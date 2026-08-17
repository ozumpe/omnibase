"""sis.frontend — the operator UI (OMNI-28). Panel; design in docs/OPERATOR_FRONTEND.md.

Run it::

    poetry install --with ui
    poetry run python -m sis.frontend

This module is the **renderer**. Every rule about what an operator may change
lives in :mod:`sis.operator` and is enforced there, so a disabled widget is a
courtesy to the person using the UI rather than the thing standing between them
and the spend cap. That split is why this file imports Panel and that one does
not: the rules stay unit-testable without a browser, and they hold for a request
that never went through a browser at all.

What it shows:

- **System state** — the :class:`sis.self_model.SelfModel` snapshot when a loop
  is running, and an honest "not running" when it is not. The UI never starts
  Ray itself; a console that silently boots the thing it is meant to observe
  would make "is it up?" unanswerable.
- **Configuration** — every key with its value, its tier, and *where the value
  came from*, with `forbidden_` read-only, `strict_` behind a confirmation, and
  anything currently shadowed by an environment variable or CLI flag marked as
  such at the point of editing.

Panel is imported lazily throughout, because it is an optional dependency
(``--with ui``) and the engine must import cleanly without it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sis import config, operator, settings
from sis.config import ConfigTier, Source
from sis.operator import Edit, EditRefused, KeyView

if TYPE_CHECKING:  # pragma: no cover - typing only
    import panel as pn


TITLE = "omnibase — operator"

_TIER_BADGE = {
    ConfigTier.FORBIDDEN: "🔒 forbidden",
    ConfigTier.STRICT: "⚠️ strict",
    ConfigTier.SOFT: "✏️ soft",
}


# --------------------------------------------------------------------------
# System state
# --------------------------------------------------------------------------


def system_state() -> dict[str, Any]:
    """The SelfModel snapshot, or an explanation of why there isn't one.

    Deliberately tolerant: the operator console is most useful exactly when
    something is wrong, so a cluster that is down, unreachable, or has no
    SelfModel yet must render as a readable status rather than a traceback.
    """
    try:
        import ray
    except ModuleNotFoundError:  # pragma: no cover - ray is a core dep
        return {"running": False, "detail": "ray is not installed"}

    from sis.org import NAMESPACE
    from sis.self_model import SELF_MODEL_NAME

    if not ray.is_initialized():
        try:
            # `address="auto"` attaches to an existing cluster and fails if there
            # is none, which is what we want: never start one from the console.
            ray.init(
                address="auto",
                namespace=NAMESPACE,
                ignore_reinit_error=True,
                log_to_driver=False,
            )
        except Exception as exc:  # noqa: BLE001 - any failure means "no cluster"
            return {"running": False, "detail": f"no running Ray cluster ({exc})"}

    try:
        model = ray.get_actor(SELF_MODEL_NAME, namespace=NAMESPACE)
        snapshot = cast("dict[str, Any]", ray.get(model.snapshot.remote()))
    except Exception as exc:  # noqa: BLE001 - actor absent or cluster mid-restart
        return {"running": False, "detail": f"no SelfModel actor ({exc})"}

    return {"running": True, "snapshot": snapshot}


# --------------------------------------------------------------------------
# Configuration view
# --------------------------------------------------------------------------


def _widget_for(view: KeyView) -> pn.widgets.Widget:
    """A widget matching the key's type — or a disabled one when forbidden.

    The disabled widget is presentation. :func:`sis.operator.save_edits` refuses
    the same key regardless, which is what makes this safe to be merely visual.
    """
    import panel as pn

    from sis.config import Kind

    key, value = view.key, view.value
    common: dict[str, Any] = {"label": key.yaml_key, "disabled": not view.editable}

    if key.choices:
        return pn.widgets.Select(options=list(key.choices), value=value, **common)
    if key.kind is Kind.BOOL:
        return pn.widgets.Checkbox(value=bool(value), **common)
    if key.kind in (Kind.INT, Kind.OPT_INT):
        return pn.widgets.IntInput(value=value, **common)
    if key.kind is Kind.FLOAT:
        return pn.widgets.FloatInput(value=value, **common)
    if key.kind is Kind.STR_LIST:
        return pn.widgets.TextAreaInput(
            value=", ".join(str(item) for item in value), height=70, **common
        )
    return pn.widgets.TextInput(value="" if value is None else str(value), **common)


def _describe(view: KeyView) -> str:
    """The one-line explanation under a key: what it is, and where it came from."""
    origin = {
        Source.CLI: "set by a command-line flag",
        Source.ENV: f"set by {view.key.env}",
        Source.FILE: "set in config.yml",
        Source.DEFAULT: "built-in default",
    }[view.source]
    line = f"{_TIER_BADGE[view.key.tier]} · {origin} · `{view.key.env}`"
    if view.shadowed:
        line += f"\n\n**Shadowed — {view.shadow_note}**"
    return f"{view.key.doc}\n\n{line}"


def build_config_panel() -> pn.viewable.Viewable:
    """The configuration editor: one section per schema section."""
    import panel as pn

    views = operator.review()
    by_section: dict[str, list[KeyView]] = {}
    for view in views:
        by_section.setdefault(view.key.section, []).append(view)

    widgets: dict[str, Any] = {}
    status = pn.pane.Alert("", alert_type="light", visible=False)
    confirm = pn.widgets.Checkbox(
        label="I confirm the strict_ changes below — each one weakens a check",
        value=False,
    )

    def save(_event: Any) -> None:
        edits = _pending_edits(views, widgets)
        if not edits:
            _flash(status, "Nothing changed.", "light")
            return
        try:
            operator.save_edits(edits, confirmed=confirm.value)
        except (EditRefused, ValueError, KeyError) as exc:
            _flash(status, f"Refused: {exc}", "danger")
            return
        changed = ", ".join(edit.path for edit in edits)
        _flash(
            status,
            f"Saved {changed} to config.yml. **Restart to apply** — a running "
            "loop keeps the configuration it started with.",
            "success",
        )

    save_button = pn.widgets.Button(label="Save to config.yml", color="primary")
    save_button.on_click(save)

    sections = []
    for section, section_views in by_section.items():
        rows = []
        for view in section_views:
            widget = _widget_for(view)
            widgets[view.key.path] = widget
            rows.append(pn.Column(widget, pn.pane.Markdown(_describe(view))))
        sections.append((section, pn.Column(*rows)))

    return pn.Column(
        pn.pane.Markdown(
            "## Configuration\n"
            "Precedence is **CLI flag > environment variable > config.yml > "
            "built-in default**, so a key marked *shadowed* will save here and "
            "still not take effect. Edits apply on restart."
        ),
        pn.Tabs(*sections),
        confirm,
        pn.Row(save_button),
        status,
    )


def _pending_edits(views: list[KeyView], widgets: dict[str, Any]) -> list[Edit]:
    """Which widgets actually differ from the value they were rendered with.

    Only changed keys are submitted, so saving a soft_ change does not require
    confirming an untouched strict_ one sitting elsewhere on the page.
    """
    edits: list[Edit] = []
    for view in views:
        if not view.editable:
            continue
        raw = widgets[view.key.path].value
        if _normalise(view, raw) != _normalise(view, view.value):
            edits.append(Edit(view.key.path, raw))
    return edits


def _normalise(view: KeyView, raw: Any) -> Any:
    """Compare a widget value against a config value on equal terms.

    A text box holding ``"a, b"`` and a tuple ``("a", "b")`` are the same
    setting; without this every list-valued key would look edited on every save.
    """
    try:
        return config.parse_value(view.key, raw, Source.FILE)
    except ValueError:
        return raw


def _flash(alert: Any, message: str, kind: str) -> None:
    alert.alert_type = kind
    alert.object = message
    alert.visible = True


# --------------------------------------------------------------------------
# State view
# --------------------------------------------------------------------------


def build_state_panel() -> pn.viewable.Viewable:
    import panel as pn

    state = system_state()
    if not state["running"]:
        return pn.Column(
            pn.pane.Markdown("## System state"),
            pn.pane.Alert(
                f"Not connected to a running system — {state['detail']}.\n\n"
                "Start one with `poetry run python main.py --loop`. This console "
                "deliberately does not start the engine itself.",
                alert_type="warning",
            ),
        )
    return pn.Column(
        pn.pane.Markdown("## System state"),
        pn.pane.JSON(state["snapshot"], depth=3, name="SelfModel"),
    )


def build_app() -> pn.viewable.Viewable:
    """The whole page. Called per session, so each visitor gets fresh state."""
    import panel as pn

    return pn.template.FastListTemplate(
        title=TITLE,
        main=[build_state_panel(), build_config_panel()],
    )


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------


def _install_oauth(frontend: config.FrontendConfig) -> dict[str, Any]:
    """Configure Panel's OAuth and the allowlist. Returns extra serve kwargs.

    The allowlist is applied through Panel's ``authorize_callback``, which runs
    after a successful login and before any page is served — so an authenticated
    GitHub user who is not on the list gets nothing, rather than a page they
    merely cannot edit.
    """
    import panel as pn

    if frontend.auth == "none":
        return {}

    creds = settings.cached_settings().frontend
    if creds is None or not creds.oauth_key or not creds.oauth_secret:
        raise operator.RefusesToServe(
            "frontend.auth is 'github' but no OAuth credentials are configured. "
            "Add frontend.oauth_key and frontend.oauth_secret to "
            "secrets.local.yml (see secrets.example.yml)."
        )

    pn.config.oauth_provider = "github"
    pn.config.oauth_key = creds.oauth_key
    pn.config.oauth_secret = creds.oauth_secret
    if creds.cookie_secret:
        pn.config.cookie_secret = creds.cookie_secret

    def authorize(user_info: dict[str, Any]) -> bool:
        login = str(user_info.get("login") or user_info.get("username") or "")
        return operator.is_authorized(login, frontend)

    pn.config.authorize_callback = authorize
    return {}


def serve(*, frontend: config.FrontendConfig | None = None, show: bool = False) -> None:
    """Start the operator UI, or refuse to.

    The refusal cases are in :func:`sis.operator.check_servable` and are checked
    *before* anything binds a socket: no authentication on a non-loopback bind,
    or GitHub auth with an empty allowlist.
    """
    import panel as pn

    resolved = config.config().frontend if frontend is None else frontend
    operator.check_servable(resolved)
    extra = _install_oauth(resolved)

    pn.serve(
        build_app,
        port=resolved.port,
        address=resolved.bind,
        title=TITLE,
        show=show,
        **extra,
    )


def _main() -> None:  # pragma: no cover - entry point
    import sys

    cli = config.parse_cli(sys.argv[1:])
    config.apply_cli_overrides(cli)
    serve(show="--show" in sys.argv)


if __name__ == "__main__":  # pragma: no cover
    _main()
