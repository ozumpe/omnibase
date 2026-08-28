"""sis.operator — what a human may change through the operator UI, and how.

This is the decision layer behind the Panel frontend (OMNI-28,
``docs/OPERATOR_FRONTEND.md``). It is deliberately separate from the UI and
contains no Panel import, for the same reason the rest of the engine keeps
decision logic in pure functions and I/O in the actors: a rule that only exists
as a disabled widget is not a rule.

**The gate is here, not in the browser.** :func:`save_edits` refuses a
``forbidden_`` key and an unjustified ``strict_`` key whatever the caller sends.
The Panel layer renders the same tier information so the operator is not
surprised, but nothing depends on it doing so — a hand-crafted request to the
same function is refused identically.

Four things this module exists to get right:

- **Tier.** ``forbidden_`` is never writable here; ``strict_`` needs a
  confirmed :class:`Approval` carrying a written justification; ``soft_`` is
  free. Note the asymmetry with :mod:`sis.policy`, which answers a different
  question — what the *loop* may rewrite. The loop may not write ``config.yml``
  at any tier.
- **Shadowing.** ``config.yml`` is the third of four layers (CLI > env > file >
  default), so an edit to a key currently supplied by ``SIS_*`` will save
  correctly, survive a restart, and still not take effect. That is the single
  most likely way this tool can lie to someone, so :func:`review` reports it
  per key and the UI shows it at the point of editing.
- **Validation.** Every edit goes through :func:`config.parse_value` with
  ``Source.FILE``, so the UI inherits the schema's existing checks — the
  ``choices`` list, the ``positive`` rule, boolean spellings — instead of
  growing a second, quietly different set.
- **Audit.** Every successful write appends to ``runtime/operator_audit.jsonl``
  (:func:`append_audit`): when, which key, from what to what, at which tier,
  and the justification given. Written by :func:`save_edits` itself rather than
  by the UI, so the record is a property of the write path and not of the
  caller remembering to log.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sis import config
from sis.config import ConfigTier, Key, Source
from sis.policy import Justification


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


# A justification of "." is not a justification. The threshold is deliberately
# low and deliberately not zero: it exists to stop a single keystroke standing
# in for a reason, not to judge whether the reason is a good one — no rule can
# do that, and one that pretended to would only teach operators to pad.
MIN_JUSTIFICATION_CHARS = 12


@dataclass(frozen=True)
class Approval:
    """Who authorised an edit, and the reason they gave for it.

    Reuses :class:`sis.policy.Justification` rather than growing a second
    approval vocabulary. The loop's "a human asked for this" and the console's
    are the same claim about the same system, and letting one of them be an
    enum while the other is a bare boolean is how two subtly different meanings
    of "approved" come to exist.

    The boolean this replaces could say only *that* someone clicked. A
    ``strict_`` key is by definition one whose edit removes or weakens a check,
    so the interesting part was always *why* — and a checkbox cannot carry it.
    """

    justification: Justification = Justification.NONE
    note: str = ""

    @classmethod
    def human(cls, note: str) -> Approval:
        """An operator confirming at the console, with their stated reason."""
        return cls(Justification.HUMAN_REQUEST, note)

    @property
    def reason(self) -> str:
        """The reason, whitespace-stripped — a note of spaces is not a reason."""
        return self.note.strip()

    @property
    def is_human_request(self) -> bool:
        return self.justification is Justification.HUMAN_REQUEST


UNJUSTIFIED = Approval()
"""No approval offered. The default, and sufficient for ``soft_`` keys only."""


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


def check_editable(key: Key, *, approval: Approval = UNJUSTIFIED) -> None:
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
    if key.tier is not ConfigTier.STRICT:
        return
    if not approval.is_human_request:
        raise EditRefused(
            f"{key.path} is strict_ and needs an explicit human confirmation — "
            "it removes or weakens a check rather than merely tuning one."
        )
    if len(approval.reason) < MIN_JUSTIFICATION_CHARS:
        raise EditRefused(
            f"{key.path} is strict_, so the confirmation must say why the check "
            f"is being weakened (at least {MIN_JUSTIFICATION_CHARS} characters). "
            "The reason is written to the operator audit log next to the change."
        )


def plan_edits(
    edits: Sequence[Edit],
    *,
    approval: Approval = UNJUSTIFIED,
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
        check_editable(key, approval=approval)
        values[key.path] = config.parse_value(key, edit.raw, Source.FILE)
    return values


# --------------------------------------------------------------------------
# The audit log: what was changed, and why
# --------------------------------------------------------------------------

OPERATOR_AUDIT_JSONL = Path("runtime/operator_audit.jsonl")


@dataclass(frozen=True)
class AuditEntry:
    """One committed config change, as it is recorded on disk.

    ``before``/``after`` are the **file** layer's values, which is what the save
    actually altered. The effective value can still differ from both if a
    ``SIS_*`` variable shadows the key — recording the file layer keeps the log
    a true statement about the write rather than a guess about the process that
    will next read it.
    """

    at: str
    path: str
    tier: str
    before: Any
    after: Any
    justification: str
    note: str


def _audit_now() -> str:
    """Wall clock, deliberately — an audit trail records when a *human* acted.

    Same reasoning as :func:`sis.self_model._now`: :mod:`sis.clock` exists to
    make *event* time injectable for replay, and a record of who changed the
    spend cap and when is worth less if it can be repositioned.
    """
    return datetime.datetime.now(datetime.UTC).isoformat()


def audit_path_for(target: Path, override: Path | None = None) -> Path:
    """Where the audit log lives for a save that wrote *target*.

    A save aimed at a temporary config file keeps its audit next to it, so a
    test never appends to the real log — the same isolation trick
    :class:`sis.episodic.JsonlEpisodicStore` uses for its state file.
    """
    if override is not None:
        return override
    if target == config.CONFIG_FILE:
        return OPERATOR_AUDIT_JSONL
    return target.with_name(f"{target.stem}_audit.jsonl")


def append_audit(entries: Sequence[AuditEntry], path: Path) -> None:
    """Append *entries* to the audit log. Append-only, one JSON object per line."""
    if not entries:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(asdict(entry)) + "\n")


def read_audit(path: Path | None = None) -> list[AuditEntry]:
    """Read the audit log back. Skips corrupt lines rather than raising.

    A truncated final line — a crash mid-append — must not make the whole
    history unreadable, for the same reason the episodic store tolerates a
    corrupt state file: the log is most needed when something went wrong.
    """
    target = OPERATOR_AUDIT_JSONL if path is None else path
    if not target.exists():
        return []
    entries: list[AuditEntry] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(AuditEntry(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            continue
    return entries


def save_edits(
    edits: Sequence[Edit],
    *,
    approval: Approval = UNJUSTIFIED,
    path: Path | None = None,
    audit_path: Path | None = None,
) -> dict[str, Any]:
    """Apply *edits* to ``config.yml`` and return the values now on disk.

    Takes effect on **restart**, not immediately: the file layer is cached per
    process (:func:`config.file_layer`) precisely so a running cycle cannot
    change behaviour half-way through because someone hit save. The cache is
    reset here anyway, so a tool that saves and then re-reads in one process
    sees its own write rather than a stale snapshot.

    The audit log is appended after the config file is written, so a failed
    write leaves no record of a change that did not happen.
    """
    target = config.CONFIG_FILE if path is None else path
    # Read the file being written, not the process-wide one: otherwise a caller
    # pointed at another path would merge this repo's values into it.
    before = config.load_file(target)
    values = plan_edits(edits, approval=approval, current=before)
    target.write_text(config.render_config_file(values), encoding="utf-8")
    config.reset_config_cache()

    at = _audit_now()
    append_audit(
        [
            AuditEntry(
                at=at,
                path=edit.path,
                tier=config.key_for(edit.path).tier.value,
                before=before.get(edit.path),
                after=values[edit.path],
                justification=approval.justification.value,
                note=approval.reason,
            )
            for edit in edits
        ],
        audit_path_for(target, audit_path),
    )
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
