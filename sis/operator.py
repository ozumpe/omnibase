"""sis.operator — what a human may change through the operator UI, and how.

This is the decision layer behind the Panel frontend (OMNI-28,
``docs/OPERATOR_FRONTEND.md``). It is deliberately separate from the UI and
contains no Panel import, for the same reason the rest of the engine keeps
decision logic in pure functions and I/O in the actors: a rule that only exists
as a disabled widget is not a rule.

**The gate is here, not in the browser.** :func:`save_edits` refuses a
``forbidden_`` key and an unconfirmed ``strict_`` key whatever the caller sends.
The Panel layer renders the same tier information so the operator is not
surprised, but nothing depends on it doing so — a hand-crafted request to the
same function is refused identically.

Three things this module exists to get right:

- **Tier.** ``forbidden_`` is never writable here; ``strict_`` needs an
  explicit confirmation; ``soft_`` is free. Note the asymmetry with
  :mod:`sis.policy`, which answers a different question — what the *loop* may
  rewrite. The loop may not write ``config.yml`` at any tier.
- **Shadowing.** ``config.yml`` is the third of four layers (CLI > env > file >
  default), so an edit to a key currently supplied by ``SIS_*`` will save
  correctly, survive a restart, and still not take effect. That is the single
  most likely way this tool can lie to someone, so :func:`review` reports it
  per key and the UI shows it at the point of editing.
- **Validation.** Every edit goes through :func:`config.parse_value` with
  ``Source.FILE``, so the UI inherits the schema's existing checks — the
  ``choices`` list, the ``positive`` rule, boolean spellings — instead of
  growing a second, quietly different set.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sis import config
from sis.config import ConfigTier, Key, Source


class EditRefused(Exception):
    """An edit was rejected before anything was written.

    Raised rather than returned: every caller must handle a refusal, and a
    boolean return that a UI forgets to check is how a guardrail becomes
    decorative.
    """


@dataclass(frozen=True)
class Edit:
    """One requested change, as it arrives from the UI — value still raw."""

    path: str
    raw: object


@dataclass(frozen=True)
class KeyView:
    """One key as the operator sees it: value, origin, and what may be done."""

    key: Key
    value: Any
    source: Source

    @property
    def editable(self) -> bool:
        return self.key.tier is not ConfigTier.FORBIDDEN

    @property
    def needs_confirmation(self) -> bool:
        return self.key.tier is ConfigTier.STRICT

    @property
    def shadowed(self) -> bool:
        """Is a higher-precedence layer currently overriding ``config.yml``?

        When true, saving an edit changes the file and changes nothing else,
        because the environment or a CLI flag still wins. The UI says so.
        """
        return self.source in (Source.ENV, Source.CLI)

    @property
    def shadow_note(self) -> str | None:
        """Plain-language explanation of *why* an edit here would do nothing."""
        if not self.shadowed:
            return None
        if self.source is Source.CLI:
            return (
                f"{self.key.flag} was passed on the command line and outranks "
                "config.yml — saving here will not change the running value."
            )
        return (
            f"{self.key.env} is set in the environment and outranks config.yml "
            "— saving here will not change the running value until it is unset."
        )


def review(env: Mapping[str, str] | None = None) -> list[KeyView]:
    """Every key, with its value, origin, and what the operator may do to it.

    The whole model the UI renders. Built from :func:`config.effective`, so
    there is exactly one implementation of the precedence chain.
    """
    return [KeyView(r.key, r.value, r.source) for r in config.effective(env)]


def check_editable(key: Key, *, confirmed: bool) -> None:
    """Refuse *key* unless this operator is allowed to change it right now.

    Split out from :func:`save_edits` so the UI can ask the same question the
    write path will ask, and get the same answer with the same wording.
    """
    if key.tier is ConfigTier.FORBIDDEN:
        raise EditRefused(
            f"{key.path} is forbidden_ and cannot be changed through the "
            "operator UI. Its default is declared in sis/config.py and changing "
            "it is a reviewed code change; for a one-off run, use the "
            f"environment layer ({key.env})."
        )
    if key.tier is ConfigTier.STRICT and not confirmed:
        raise EditRefused(
            f"{key.path} is strict_ and needs an explicit confirmation — it "
            "removes or weakens a check rather than merely tuning one."
        )


def plan_edits(
    edits: Sequence[Edit],
    *,
    confirmed: bool = False,
    current: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate *edits* and return the full set of file values they produce.

    Pure: reads the current file layer (or *current*, for tests) and returns a
    new mapping. Writes nothing. Every edit is refused or fully applied — there
    is no partial write, so a rejected third edit cannot leave the first two on
    disk in a state nobody chose.
    """
    values = dict(config.load_file() if current is None else current)
    for edit in edits:
        key = config.key_for(edit.path)  # unknown path raises KeyError
        check_editable(key, confirmed=confirmed)
        values[key.path] = config.parse_value(key, edit.raw, Source.FILE)
    return values


def save_edits(
    edits: Sequence[Edit],
    *,
    confirmed: bool = False,
    path: Path | None = None,
) -> dict[str, Any]:
    """Apply *edits* to ``config.yml`` and return the values now on disk.

    Takes effect on **restart**, not immediately: the file layer is cached per
    process (:func:`config.file_layer`) precisely so a running cycle cannot
    change behaviour half-way through because someone hit save. The cache is
    reset here anyway, so a tool that saves and then re-reads in one process
    sees its own write rather than a stale snapshot.
    """
    target = config.CONFIG_FILE if path is None else path
    # Read the file being written, not the process-wide one: otherwise a caller
    # pointed at another path would merge this repo's values into it.
    values = plan_edits(edits, confirmed=confirmed, current=config.load_file(target))
    target.write_text(config.render_config_file(values), encoding="utf-8")
    config.reset_config_cache()
    return values


# --------------------------------------------------------------------------
# Serving the UI: the combinations that must not start
# --------------------------------------------------------------------------

# Binding to any of these means "this machine only", which is what makes
# running without authentication defensible during local development.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def is_loopback(bind: str) -> bool:
    """Does *bind* reach only this machine?

    ``0.0.0.0`` and ``::`` are explicitly not loopback: they are every
    interface, which is the case the auth rule below exists for.
    """
    return bind.strip() in _LOOPBACK


class RefusesToServe(Exception):
    """The UI will not start in the configuration it was given."""


def check_servable(frontend: config.FrontendConfig) -> None:
    """Refuse the configurations that would serve this UI unsafely.

    Same shape as the M1 rule that a real proposer requires the docker sandbox:
    reject the unsafe *combination* at startup rather than trusting whoever
    deploys it to avoid assembling one. Two rules:

    - **No authentication on a non-loopback bind.** ``auth: none`` exists so
      local development does not need a registered OAuth app. Reaching the
      network without it is never what someone meant.
    - **GitHub auth with an empty allowlist.** Failing closed is not enough on
      its own here: an allowlist nobody filled in would otherwise present a
      login screen that admits no one, which reads as a broken deployment
      rather than as a missing setting. Saying so at startup is kinder and
      equally safe.
    """
    if frontend.auth == "none" and not is_loopback(frontend.bind):
        raise RefusesToServe(
            f"frontend.auth is 'none' but frontend.bind is {frontend.bind!r}, "
            "which is reachable beyond this machine. Either bind to 127.0.0.1 "
            "or configure GitHub OAuth."
        )
    if frontend.auth == "github" and not frontend.allowed_logins:
        raise RefusesToServe(
            "frontend.auth is 'github' but frontend.allowed_logins is empty, so "
            "no one could sign in. Add the GitHub logins that may use the "
            "operator UI (SIS_FRONTEND_ALLOWED_LOGINS, or config.yml)."
        )


def is_authorized(login: str, frontend: config.FrontendConfig) -> bool:
    """Is *login* permitted to use the UI?

    Fails closed on an empty allowlist. GitHub logins are case-insensitive, so
    the comparison is too — otherwise an allowlisted operator is locked out by
    the capitalisation their profile happens to use.
    """
    if not login.strip():
        return False
    permitted = {entry.strip().lower() for entry in frontend.allowed_logins}
    return login.strip().lower() in permitted
