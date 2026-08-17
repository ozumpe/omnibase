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
