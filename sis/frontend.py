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
- **Brakes** — the CEO's spend against its cap, the consecutive-failure count,
  and cost-per-accepted. Read from the live actor, so what is shown is what is
  actually braking the loop rather than what this console's own configuration
  says it should be.
- **Episodic history** — accepted/rejected counts and the ``rejected_by_gate``
  breakdown. Read from the store on disk, so it renders with no cluster at
  all: the history of what happened outlives the process it happened in.
- **Configuration** — every key with its value, its tier, and *where the value
  came from*, with `forbidden_` read-only, `strict_` behind a justified
  confirmation, and anything currently shadowed by an environment variable or
  CLI flag marked as such at the point of editing.

Each of those four degrades on its own. A cluster with no CEO still renders its
episodic history; an unreadable episodic store still renders the brakes. The
console is most useful when something is broken, which is exactly when a single
shared failure path would blank the whole page.

Panel is imported lazily throughout, because it is an optional dependency
(``--with ui``) and the engine must import cleanly without it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from sis import config, episodic, operator, settings
from sis.config import ConfigTier, Source
from sis.operator import Approval, Edit, EditRefused, KeyView

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


def _attach() -> str | None:
    """Attach to a already-running cluster. ``None`` on success, else why not.

    Shared by every read that needs a live actor, so "is there a cluster?" is
    answered once and identically. Deliberately tolerant: the operator console
    is most useful exactly when something is wrong, so a cluster that is down or
    unreachable must render as a readable status rather than a traceback.
    """
    try:
        import ray
    except ModuleNotFoundError:  # pragma: no cover - ray is a core dep
        return "ray is not installed"

    from sis.org import NAMESPACE

    if ray.is_initialized():
        return None
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
        return f"no running Ray cluster ({exc})"
    return None


def system_state() -> dict[str, Any]:
    """The SelfModel snapshot, or an explanation of why there isn't one."""
    detail = _attach()
    if detail is not None:
        return {"running": False, "detail": detail}

    import ray

    from sis.org import NAMESPACE
    from sis.self_model import SELF_MODEL_NAME

    try:
        model = ray.get_actor(SELF_MODEL_NAME, namespace=NAMESPACE)
        snapshot = cast("dict[str, Any]", ray.get(model.snapshot.remote()))
    except Exception as exc:  # noqa: BLE001 - actor absent or cluster mid-restart
        return {"running": False, "detail": f"no SelfModel actor ({exc})"}

    return {"running": True, "snapshot": snapshot}


# --------------------------------------------------------------------------
# Brakes (CEO) — spend against the cap, the breaker, cost-per-accepted
# --------------------------------------------------------------------------


def brake_state() -> dict[str, Any]:
    """What is currently braking the loop, read from the live CEO actor.

    Read from the actor rather than reassembled from this console's own
    configuration, because they can legitimately differ: the CEO snapshots its
    thresholds when it is created, so a console started later with a different
    ``config.yml`` would otherwise display a budget nothing is enforcing.
    """
    detail = _attach()
    if detail is not None:
        return {"running": False, "detail": detail}

    import ray

    from sis.org import CEO_NAME, NAMESPACE

    try:
        ceo = ray.get_actor(CEO_NAME, namespace=NAMESPACE)
        # economics() carries the money; state_snapshot() carries the breaker's
        # failure streak, which economics() has never exposed.
        economics, snapshot = cast(
            "list[dict[str, Any]]",
            ray.get([ceo.economics.remote(), ceo.state_snapshot.remote()]),
        )
    except Exception as exc:  # noqa: BLE001 - actor absent or cluster mid-restart
        return {"running": False, "detail": f"no CEO actor ({exc})"}

    return {
        "running": True,
        "economics": dict(economics),
        "consecutive_failures": int(snapshot.get("consecutive_failures", 0)),
        "breaker_open": bool(snapshot.get("tripped", False)),
    }


def format_brakes(brakes: Mapping[str, Any]) -> str:
    """Markdown for the brake table. Pure, so the wording is unit-testable."""
    economics = brakes["economics"]
    spent = float(economics["spent_usd"])
    budget = float(economics["budget_usd"])
    accepted = int(economics["accepted"])
    cost_per_accepted = float(economics["cost_per_accepted_usd"])
    # The CEO encodes "infinite" as -1.0, since a JSON `inf` is not portable.
    # Rendering that as a number would report a negative cost per improvement.
    per_accepted = (
        "— (nothing accepted yet)"
        if cost_per_accepted < 0
        else f"${cost_per_accepted:,.4f}"
    )
    share = f"{spent / budget * 100:.1f}% of" if budget else "against a zero"
    return (
        "| brake | value |\n"
        "| --- | --- |\n"
        f"| spend | **${spent:,.4f}** — {share} the ${budget:,.2f} cap |\n"
        f"| consecutive failures | {brakes['consecutive_failures']} |\n"
        f"| accepted improvements | {accepted} |\n"
        f"| cost per accepted | {per_accepted} |\n"
    )


# --------------------------------------------------------------------------
# Episodic history — what the loop has actually done
# --------------------------------------------------------------------------


def episodic_state() -> dict[str, Any]:
    """The episodic store's rollup. Needs no cluster — it reads the store.

    Tolerant for a reason beyond symmetry: the configured backend may be one
    whose optional dependency is not installed (``duckdb`` without
    ``--with analytics``), and a console that dies on that is a console that
    dies on exactly the machine where someone is trying to work out what broke.
    """
    try:
        summary = episodic.get_episodic_store().summary()
    except Exception as exc:  # noqa: BLE001 - missing backend, unreadable file
        return {"available": False, "detail": f"episodic store unreadable ({exc})"}
    return {"available": True, "summary": summary}


def format_episodic(summary: Mapping[str, Any]) -> str:
    """Markdown for the episodic rollup. Pure, like :func:`format_brakes`."""
    total = int(summary.get("total", 0))
    if not total:
        return "No cycles recorded yet."

    accepted = int(summary.get("accepted", 0))
    lines = [f"**{accepted} accepted** of {total} recorded cycles."]

    by_outcome = summary.get("by_outcome") or {}
    if by_outcome:
        lines.append(
            "Outcomes: "
            + ", ".join(f"`{name}` × {n}" for name, n in sorted(by_outcome.items()))
        )

    # The interesting half: which gate is doing the rejecting. An empty
    # breakdown is stated rather than omitted — "no rejections" and "we did not
    # record why" look identical if the line simply disappears.
    rejected = summary.get("rejected_by_gate") or {}
    lines.append(
        "Rejected by gate: "
        + ", ".join(f"`{gate}` × {n}" for gate, n in sorted(rejected.items()))
        if rejected
        else "No rejections recorded."
    )

    cost = summary.get("total_cost_usd")
    if cost:
        lines.append(f"Total spend across recorded cycles: ${float(cost):,.4f}.")
    return "\n\n".join(lines)


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
    justification = pn.widgets.TextAreaInput(
        label="Why (required for strict_ changes)",
        placeholder="e.g. canary is flaky against the new target; disabling for "
        "this week's runs while OMNI-31 is investigated",
        height=70,
    )

    def save(_event: Any) -> None:
        edits = _pending_edits(views, widgets)
        if not edits:
            _flash(status, "Nothing changed.", "light")
            return
        # An unticked box is not a human request, so a strict_ key is refused
        # even with prose in the note — the confirmation and the reason are two
        # different claims and the write path wants both.
        approval = (
            Approval.human(justification.value) if confirm.value else operator.UNJUSTIFIED
        )
        try:
            operator.save_edits(edits, approval=approval)
        except (EditRefused, ValueError, KeyError) as exc:
            _flash(status, f"Refused: {exc}", "danger")
            return
        changed = ", ".join(edit.path for edit in edits)
        _flash(
            status,
            f"Saved {changed} to config.yml, and recorded in "
            f"`{operator.OPERATOR_AUDIT_JSONL}`. **Restart to apply** — a "
            "running loop keeps the configuration it started with.",
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
        justification,
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
    """System state, brakes and history — each rendered independently.

    Three separate reads against three separate sources, so one being
    unavailable degrades to a note in its own section instead of blanking the
    other two.
    """
    import panel as pn

    items: list[Any] = [pn.pane.Markdown("## System state")]

    state = system_state()
    if not state["running"]:
        items.append(
            pn.pane.Alert(
                f"Not connected to a running system — {state['detail']}.\n\n"
                "Start one with `poetry run python main.py --loop`. This console "
                "deliberately does not start the engine itself.",
                alert_type="warning",
            )
        )
    else:
        items.append(pn.pane.JSON(state["snapshot"], depth=3, name="SelfModel"))

    items.append(pn.pane.Markdown("## Brakes"))
    brakes = brake_state()
    if not brakes["running"]:
        items.append(
            pn.pane.Alert(f"No brake status — {brakes['detail']}.", alert_type="warning")
        )
    else:
        if brakes["breaker_open"]:
            items.append(
                pn.pane.Alert(
                    "**The circuit breaker is open.** The loop is frozen and "
                    "will not spend again until a human resets it. Clearing the "
                    "breaker deliberately does not reset spend, so a cap trip "
                    "re-trips until the budget itself is raised.",
                    alert_type="danger",
                )
            )
        items.append(pn.pane.Markdown(format_brakes(brakes)))

    items.append(pn.pane.Markdown("## Episodic history"))
    history = episodic_state()
    if not history["available"]:
        items.append(
            pn.pane.Alert(f"No history — {history['detail']}.", alert_type="warning")
        )
    else:
        items.append(pn.pane.Markdown(format_episodic(history["summary"])))

    return pn.Column(*items)


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
