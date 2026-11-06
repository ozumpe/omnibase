"""The operator UI's renderer (OMNI-28).

Panel is an optional dependency (``poetry install --with ui``) and CI installs
core+dev only, so anything needing it is skipped rather than failing. The parts
that carry real behaviour — what counts as an edit, what the UI does when there
is no cluster, and that it refuses to serve before binding a socket — are
either panel-free or explicitly marked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from sis import frontend, operator
from sis.config import FrontendConfig, Source


@dataclass
class _Widget:
    """The one thing `_pending_edits` needs from a Panel widget."""

    value: Any


def _views() -> list[operator.KeyView]:
    return operator.review(env={})


def _widgets(views: list[operator.KeyView], **changed: Any) -> dict[str, Any]:
    """Widgets rendered from *views*, with the named paths altered."""
    return {
        v.key.path: _Widget(changed.get(v.key.path.replace(".", "__"), v.value))
        for v in views
    }


# --- what counts as an edit ------------------------------------------------


def test_an_untouched_page_submits_nothing() -> None:
    """Saving without changing anything must not resubmit the whole schema.

    If it did, every save would restate each strict_ key and so demand a
    confirmation for changes the operator never made.
    """
    views = _views()
    assert frontend._pending_edits(views, _widgets(views)) == []


def test_only_the_changed_key_is_submitted() -> None:
    views = _views()
    widgets = _widgets(views, loop__interval_seconds=99.0)
    edits = frontend._pending_edits(views, widgets)

    assert [e.path for e in edits] == ["loop.interval_seconds"]


def test_a_forbidden_key_is_never_submitted_even_if_its_widget_changed() -> None:
    """Belt and braces: the write path refuses it anyway (test_operator.py).

    A tampered-with page should not even reach that refusal, because a disabled
    widget carrying a new value is a sign of the page being driven directly.
    """
    views = _views()
    widgets = _widgets(views, brakes__budget_usd=10_000.0)
    assert frontend._pending_edits(views, widgets) == []


def test_a_list_typed_key_does_not_look_edited_by_its_own_rendering() -> None:
    """`("a","b")` renders as `"a, b"`; parsing that back must be a no-op.

    Without this every save would report the target list as changed.
    """
    views = _views()
    targets = next(v for v in views if v.key.path == "policy.target_paths")
    rendered = ", ".join(str(item) for item in targets.value)

    assert frontend._normalise(targets, rendered) == frontend._normalise(
        targets, targets.value
    )


def test_an_unparseable_value_is_passed_through_to_be_refused() -> None:
    """`_normalise` must not swallow a bad value into looking unchanged."""
    views = _views()
    port = next(v for v in views if v.key.path == "frontend.port")
    assert frontend._normalise(port, "not-a-number") == "not-a-number"


# --- describing a key ------------------------------------------------------


def test_a_shadowed_key_says_so_in_its_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator is told at the point of editing, not after a puzzling restart."""
    monkeypatch.setenv("SIS_LOOP_INTERVAL", "17")
    view = next(
        v for v in operator.review() if v.key.path == "loop.interval_seconds"
    )
    described = frontend._describe(view)

    assert "Shadowed" in described
    assert "SIS_LOOP_INTERVAL" in described


def test_an_unshadowed_key_is_described_without_a_warning() -> None:
    view = next(v for v in _views() if v.key.path == "frontend.port")
    assert "Shadowed" not in frontend._describe(view)


def test_every_key_can_be_described_and_names_its_source() -> None:
    for view in _views():
        described = frontend._describe(view)
        assert view.key.doc in described
        assert view.source in Source


# --- no cluster ------------------------------------------------------------


def test_no_running_system_renders_as_a_status_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console is most useful when things are broken; it must not break too."""

    import ray

    def _no_cluster(**_kwargs: Any) -> None:
        raise ConnectionError("no cluster at 127.0.0.1:6379")

    monkeypatch.setattr(ray, "is_initialized", lambda: False)
    monkeypatch.setattr(ray, "init", _no_cluster)

    state = frontend.system_state()
    assert state["running"] is False
    assert "no cluster" in state["detail"]


def test_a_cluster_without_a_selfmodel_is_reported_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ray

    def _no_actor(_name: str, **_kwargs: Any) -> Any:
        raise ValueError("Failed to look up actor with name 'SelfModel'")

    monkeypatch.setattr(ray, "is_initialized", lambda: True)
    monkeypatch.setattr(ray, "get_actor", _no_actor)

    state = frontend.system_state()
    assert state["running"] is False
    assert "SelfModel" in state["detail"]


# --- brakes ----------------------------------------------------------------


def _economics(**overrides: Any) -> dict[str, Any]:
    base = {
        "economics": {
            "spent_usd": 0.25,
            "budget_usd": 5.0,
            "accepted": 2.0,
            "cost_per_accepted_usd": 0.125,
        },
        "consecutive_failures": 1,
        "breaker_open": False,
    }
    base.update(overrides)
    return base


def test_the_brake_table_reports_spend_against_the_cap() -> None:
    """The acceptance criterion: spend state, visible without running Python."""
    rendered = frontend.format_brakes(_economics())

    assert "$0.2500" in rendered
    assert "$5.00" in rendered
    assert "5.0%" in rendered


def test_no_acceptances_yet_is_not_rendered_as_a_negative_cost() -> None:
    """The CEO encodes an infinite cost-per-accepted as -1.0.

    Printing that verbatim would tell the operator each improvement earned them
    a dollar, which is the opposite of what it means.
    """
    brakes = _economics()
    brakes["economics"]["cost_per_accepted_usd"] = -1.0
    brakes["economics"]["accepted"] = 0.0
    rendered = frontend.format_brakes(brakes)

    assert "-1" not in rendered
    assert "nothing accepted yet" in rendered


def test_a_zero_budget_does_not_divide_by_zero() -> None:
    """`brakes.budget_usd` is settable to 0 for a deliberately frozen run."""
    brakes = _economics()
    brakes["economics"]["budget_usd"] = 0.0
    assert "zero" in frontend.format_brakes(brakes)


def test_the_failure_streak_is_shown() -> None:
    assert "| consecutive failures | 3 |" in frontend.format_brakes(
        _economics(consecutive_failures=3)
    )


def test_a_fractional_streak_is_shown_as_is() -> None:
    # OMNI-24: an over-budget cycle adds a fractional weight, and truncating it
    # to an int would show 0 while the breaker is already half-way to tripping.
    assert "| consecutive failures | 0.5 |" in frontend.format_brakes(
        _economics(consecutive_failures=0.5)
    )


def test_no_ceo_actor_is_reported_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cluster mid-restart must not blank the console."""
    import ray

    def _no_actor(_name: str, **_kwargs: Any) -> Any:
        raise ValueError("Failed to look up actor with name 'CEO'")

    monkeypatch.setattr(ray, "is_initialized", lambda: True)
    monkeypatch.setattr(ray, "get_actor", _no_actor)

    state = frontend.brake_state()
    assert state["running"] is False
    assert "CEO" in state["detail"]


# --- episodic history ------------------------------------------------------


def test_the_rejected_by_gate_breakdown_is_rendered() -> None:
    """The ticket's read side names this breakdown specifically."""
    rendered = frontend.format_episodic(
        {
            "total": 9,
            "accepted": 3,
            "by_outcome": {"promoted": 3, "rolled_back": 6},
            "rejected_by_gate": {"mypy": 4, "benchmark": 2},
            "total_cost_usd": 0.42,
        }
    )

    assert "**3 accepted** of 9" in rendered
    assert "`mypy` × 4" in rendered
    assert "`benchmark` × 2" in rendered
    assert "$0.4200" in rendered


def test_an_empty_history_says_so_rather_than_rendering_an_empty_table() -> None:
    assert "No cycles recorded yet." in frontend.format_episodic(
        {"total": 0, "accepted": 0}
    )


def test_no_rejections_is_stated_not_omitted() -> None:
    """"Nothing was rejected" and "we did not record why" must not look alike."""
    rendered = frontend.format_episodic(
        {"total": 2, "accepted": 2, "by_outcome": {"promoted": 2}}
    )
    assert "No rejections recorded." in rendered


def test_an_unreadable_store_degrades_to_a_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duckdb backend without duckdb installed must not break the page."""
    from sis import episodic

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ModuleNotFoundError("No module named 'duckdb'")

    monkeypatch.setattr(episodic, "get_episodic_store", _boom)

    state = frontend.episodic_state()
    assert state["available"] is False
    assert "duckdb" in state["detail"]


# --- building the page -----------------------------------------------------


def test_every_key_in_the_schema_gets_a_widget() -> None:
    """A new `Kind` must not reach a browser as a crash on page load.

    Covers the awkward ones by construction: an optional key whose value is
    None, a list-typed key, and every key carrying a `choices` list.
    """
    pytest.importorskip("panel")

    for view in _views():
        widget = frontend._widget_for(view)
        assert widget.disabled is (not view.editable), view.key.path


def test_the_whole_page_builds() -> None:
    pytest.importorskip("panel")
    assert frontend.build_app() is not None


# --- refusing to serve -----------------------------------------------------


def test_serve_refuses_before_it_binds_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unsafe combination must be caught before a socket exists."""
    pn = pytest.importorskip("panel")

    def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("pn.serve was reached despite an unsafe configuration")

    monkeypatch.setattr(pn, "serve", _explode)

    with pytest.raises(operator.RefusesToServe):
        frontend.serve(
            frontend=FrontendConfig(
                auth="none", allowed_logins=(), bind="0.0.0.0", port=8080
            )
        )


def test_github_auth_without_credentials_refuses_to_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An allowlisted login is useless without an OAuth app behind it."""
    pn = pytest.importorskip("panel")
    monkeypatch.setattr(pn, "serve", lambda *a, **k: None)

    from sis import settings

    monkeypatch.setattr(
        settings, "cached_settings", lambda: settings.Settings(frontend=None)
    )

    with pytest.raises(operator.RefusesToServe, match="OAuth credentials"):
        frontend.serve(
            frontend=FrontendConfig(
                auth="github", allowed_logins=("ozumpe",), bind="127.0.0.1", port=8080
            )
        )


# --- OMNI-53 (M13): authentication is actually installed --------------------

_GITHUB = FrontendConfig(auth="github", allowed_logins=("ozumpe",), bind="127.0.0.1",
                         port=8080)


def _with_credentials(monkeypatch: pytest.MonkeyPatch, cookie_secret: str = "") -> None:
    from sis import settings

    creds = settings.FrontendSettings(oauth_key="Iv1_test", oauth_secret="s3cret",
                                      cookie_secret=cookie_secret)
    monkeypatch.setattr(settings, "cached_settings",
                        lambda: settings.Settings(frontend=creds))


def test_no_auth_adds_no_server_options() -> None:
    local = FrontendConfig(auth="none", allowed_logins=(), bind="127.0.0.1", port=8080)
    assert frontend.server_options(local) == {}


def test_github_auth_is_passed_to_serve_as_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M13: setting pn.config.oauth_provider installed nothing; Panel builds
    # its auth provider only from the serve argument.
    _with_credentials(monkeypatch)
    options = frontend.server_options(_GITHUB)
    assert options["oauth_provider"] == "github"
    assert options["oauth_key"] == "Iv1_test" and options["oauth_secret"] == "s3cret"
    assert options["sign_sessions"] is True
    assert options["secret_key"] and options["cookie_secret"] and options["oauth_encryption_key"]
    # Generated per process, never a shared constant.
    again = frontend.server_options(_GITHUB)
    for key in ("secret_key", "cookie_secret", "oauth_encryption_key"):
        assert options[key] != again[key], key


def test_a_configured_cookie_secret_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_credentials(monkeypatch, cookie_secret="keep-me-across-restarts")
    assert frontend.server_options(_GITHUB)["cookie_secret"] == "keep-me-across-restarts"


def test_serve_hands_the_auth_options_to_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    pn = pytest.importorskip("panel")
    _with_credentials(monkeypatch)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(pn, "serve", lambda app, **kwargs: seen.update(kwargs))
    monkeypatch.setattr(frontend, "_SERVING", None)
    monkeypatch.setattr(pn.config, "authorize_callback", None)  # restored after
    frontend.serve(frontend=_GITHUB)
    assert seen["oauth_provider"] == "github" and seen["sign_sessions"] is True


def _built_server(monkeypatch: pytest.MonkeyPatch) -> Any:
    pytest.importorskip("panel")
    from panel.io.server import get_server

    _with_credentials(monkeypatch)
    server = get_server(frontend.build_app, port=0, address="127.0.0.1", start=False,
                        **frontend.server_options(_GITHUB))
    return server


def test_the_built_server_does_not_run_with_null_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _built_server(monkeypatch)  # skips without the ui group
    from bokeh.server.auth_provider import NullAuth

    try:
        tornado = server._tornado
        assert not isinstance(tornado.auth_provider, NullAuth)
        assert tornado.sign_sessions is True
        assert tornado.secret_key
    finally:
        server.stop()


def test_an_unsigned_session_token_is_refused_by_the_websocket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bokeh's websocket handler opens a session only for a token that passes
    # check_token_signature; with signed sessions, only the page handler (after
    # login and the authorize callback) can mint one.
    server = _built_server(monkeypatch)  # skips without the ui group
    from bokeh.util.token import check_token_signature, generate_jwt_token, generate_session_id

    try:
        tornado = server._tornado
        forged = generate_jwt_token(generate_session_id(signed=False), signed=False)
        assert not check_token_signature(forged, signed=tornado.sign_sessions,
                                         secret_key=tornado.secret_key)
        minted = generate_jwt_token(
            generate_session_id(secret_key=tornado.secret_key, signed=True),
            secret_key=tornado.secret_key, signed=True)
        assert check_token_signature(minted, signed=tornado.sign_sessions,
                                     secret_key=tornado.secret_key)
    finally:
        server.stop()


def test_authorize_refuses_a_request_with_no_user_info() -> None:
    # Was an AttributeError on None.get (M13), which Panel turned into a 403
    # for everyone.
    assert frontend.authorize_login(None, _GITHUB) is False
    assert frontend.authorize_login({}, _GITHUB) is False
    assert frontend.authorize_login({"login": "OZUMPE"}, _GITHUB) is True
    assert frontend.authorize_login({"login": "mallory"}, _GITHUB) is False


def test_a_session_without_an_authorized_user_is_refused() -> None:
    assert frontend.session_refusal(_GITHUB, user="") is not None
    assert "mallory" in str(frontend.session_refusal(_GITHUB, user="mallory"))
    assert frontend.session_refusal(_GITHUB, user="ozumpe") is None
    local = FrontendConfig(auth="none", allowed_logins=(), bind="127.0.0.1", port=8080)
    assert frontend.session_refusal(local, user="") is None


def test_an_unauthorized_session_gets_no_console(monkeypatch: pytest.MonkeyPatch) -> None:
    pn = pytest.importorskip("panel")
    monkeypatch.setattr(frontend, "_SERVING", _GITHUB)
    monkeypatch.setattr(frontend, "_current_user", lambda: None)
    page = frontend.build_app()
    assert isinstance(page, pn.pane.Alert) and "Not authorized" in page.object


def test_an_unauthorized_session_cannot_save(monkeypatch: pytest.MonkeyPatch) -> None:
    # Acceptance criterion: a session that is not an authorized operator's
    # cannot reach save_edits, even if it holds a built config panel.
    pn = pytest.importorskip("panel")
    monkeypatch.setattr(frontend, "_SERVING", _GITHUB)
    monkeypatch.setattr(frontend, "_current_user", lambda: "ozumpe")
    panel = frontend.build_config_panel()  # built while authorized

    def _reached(*_a: Any, **_k: Any) -> None:
        raise AssertionError("save_edits was reached by an unauthorized session")

    monkeypatch.setattr(operator, "save_edits", _reached)
    monkeypatch.setattr(frontend, "_current_user", lambda: None)

    buttons = [w for w in panel.select(pn.widgets.Button)]
    alerts = [w for w in panel.select(pn.pane.Alert)]
    assert len(buttons) == 1 and len(alerts) == 1
    buttons[0].clicks += 1
    assert alerts[0].visible and "Refused" in alerts[0].object
    assert "no signed-in GitHub user" in alerts[0].object


def test_a_served_app_is_guarded_too(monkeypatch: pytest.MonkeyPatch) -> None:
    # The twin viewer is served through frontend.serve(app=...) under the same
    # allowlist, so its page is guarded the same way.
    pytest.importorskip("panel")
    monkeypatch.setattr(frontend, "_SERVING", _GITHUB)
    monkeypatch.setattr(frontend, "_current_user", lambda: "mallory")
    guarded = frontend._guarded(lambda: "the viewer")
    assert guarded() != "the viewer"
    monkeypatch.setattr(frontend, "_current_user", lambda: "ozumpe")
    assert guarded() == "the viewer"
