"""sis.admin — an operator's hands on the running CEO (OMNI-61).

    python -m sis.admin status
    python -m sis.admin pause         --reason "looking into a spend spike"
    python -m sis.admin resume        --reason "spike explained: one retried call"
    python -m sis.admin reset-breaker --reason "state file repaired; spend checked"

Before this, ``CEO.reset_breaker()`` had no caller outside the tests (KNOWN_ISSUES
L23): clearing a tripped breaker meant editing state by hand or restarting —
and a restart was exactly the path on which corrupt state used to read as
"nothing spent". Each action here needs a written reason, like a ``strict_``
edit in the operator console, and lands in the same audit log
(``runtime/operator_audit.jsonl``): who paused the loop, or reset a safety trip,
and why, is the question that log exists to answer.

Human-only **by convention, not by construction**. Anything that can reach the
named CEO actor can call the same methods — which is why candidate code is kept
off the cluster (OMNI-48/49) rather than this module being treated as a
security boundary.

What each action means is decided by :func:`plan`, which is pure; everything
else here is the I/O around it.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sis.operator import MIN_JUSTIFICATION_CHARS, OPERATOR_AUDIT_JSONL, AuditEntry, append_audit

ACTIONS = ("status", "pause", "resume", "reset-breaker")


@dataclass(frozen=True)
class Plan:
    """What an action will do: the CEO method to call (None for none), or why not."""

    method: str | None
    message: str
    refused: bool = False


def plan(action: str, snapshot: Mapping[str, Any], reason: str) -> Plan:
    """Decide what *action* does to a CEO in state *snapshot*. Pure.

    Every state-changing action needs a reason of at least
    :data:`sis.operator.MIN_JUSTIFICATION_CHARS` — the same bar a ``strict_``
    config edit clears, for the same reason: it is the part of the audit entry
    that answers "why". An action that would change nothing says so and calls
    nothing, so the log records decisions, not no-ops.
    """
    if action not in ACTIONS:
        return Plan(None, f"unknown action {action!r}; one of {', '.join(ACTIONS)}",
                    refused=True)
    if action == "status":
        return Plan(None, describe(snapshot))
    if len(reason.strip()) < MIN_JUSTIFICATION_CHARS:
        return Plan(None, f"{action} needs --reason of at least {MIN_JUSTIFICATION_CHARS} "
                          "characters: it is what the audit log will say about why",
                    refused=True)
    paused = snapshot.get("paused")
    if action == "pause":
        verb = "re-paused" if paused else "paused"
        return Plan("pause", f"{verb}: new cycles are refused until `sis.admin resume` "
                             "(breaker, spend and failure streak are untouched)")
    if action == "resume":
        if not paused:
            return Plan(None, "not paused; nothing to resume")
        note = " The breaker is still open." if snapshot.get("tripped") else ""
        return Plan("resume", f"resumed: new cycles may start.{note}")
    # reset-breaker
    if not snapshot.get("tripped"):
        return Plan(None, "the breaker is not open; nothing to reset")
    message = ("breaker reset: the failure streak is cleared; spend is not "
               "(a spend-cap trip re-trips until the budget is raised)")
    if str(snapshot.get("trip_reason") or "").startswith("brake state unreadable"):
        message += (". The CEO booted from unreadable state, so spend restarts from what "
                    "it has counted since — and the store still refuses to overwrite the "
                    "unreadable file: repair it or move it aside, or nothing is persisted.")
    return Plan("reset_breaker", message)


def describe(snapshot: Mapping[str, Any]) -> str:
    """One human-readable status line for a CEO snapshot. Pure."""
    breaker = (f"OPEN ({snapshot.get('trip_reason') or 'reason not recorded'})"
               if snapshot.get("tripped") else "closed")
    paused = snapshot.get("paused")
    return (f"breaker {breaker}; {'paused (' + str(paused) + ')' if paused else 'running'}; "
            f"spent ${float(snapshot.get('spent_usd', 0.0)):.4f}; "
            f"failure streak {float(snapshot.get('consecutive_failures', 0.0)):g}; "
            f"accepted {int(snapshot.get('accepted', 0))}")


def _audit(action: str, before: Mapping[str, Any], after: Mapping[str, Any],
           reason: str) -> None:
    append_audit([AuditEntry(
        at=datetime.datetime.now(datetime.UTC).isoformat(),
        path=f"ceo.{action}", tier="admin",
        before=dict(before), after=dict(after),
        justification=reason, note="sis.admin",
    )], OPERATOR_AUDIT_JSONL)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sis.admin", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--reason", default="", help="why — recorded in the audit log")
    args = parser.parse_args(argv)

    import ray

    from sis import episodic
    from sis.org import CEO_NAME, NAMESPACE

    try:
        ray.init(address="auto", namespace=NAMESPACE, logging_level=logging.ERROR,
                 ignore_reinit_error=True)
        # Any, like every role handle in sis.org: Ray's stubs type actor
        # methods too loosely to be worth fighting here.
        ceo: Any = ray.get_actor(CEO_NAME, namespace=NAMESPACE)
    except (ConnectionError, ValueError) as exc:
        print(f"no running CEO to talk to ({exc}). Start the loop first; this acts on "
              "a live cluster, not on the state file.", file=sys.stderr)
        return 2

    before: dict[str, Any] = ray.get(ceo.state_snapshot.remote())
    decided = plan(args.action, before, args.reason)
    if decided.method is None:
        print(decided.message, file=sys.stderr if decided.refused else sys.stdout)
        return 1 if decided.refused else 0

    if decided.method == "pause":
        ray.get(ceo.pause.remote(args.reason))
    else:
        ray.get(getattr(ceo, decided.method).remote())
    after: dict[str, Any] = ray.get(ceo.state_snapshot.remote())
    _audit(args.action, before, after, args.reason)
    print(decided.message)
    print(describe(after))

    # Persisted now, not at the next cycle: a paused loop runs no cycles, and a
    # pause that a restart forgot would be no pause at all.
    try:
        episodic.get_episodic_store().save_state("ceo", after)
    except Exception as exc:  # noqa: BLE001 - live state is changed; say what is not
        print(f"WARNING: the running CEO is updated, but its state was not persisted "
              f"({exc}); a restart before the next cycle would not see this change.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
