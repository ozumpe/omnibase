"""sis.org — bootstrap the actor org and drive one intake→deploy cycle.

This wires the roles onto the control loop exactly as ACTORS.md maps them:

    budget/goal gate   → CEO
    spec / design      → PM (+ Designer), in Confluence
    plan (epics)       → CTO, in Jira
    propose / implement→ SWE, on a feature branch (reuses proposer+gauntlet)
    verify             → QA + the deterministic gauntlet
    canary deploy      → DevOps via the Cloud port (green slot)
    promote / rollback → human PR merge, observed & applied by DevOps / DevOps
    circuit breaker    → CEO authority

Everything is coordinated through durable artifacts in the shared Workspace,
and every handoff is recorded in the SelfModel provenance graph.
"""

from __future__ import annotations

import datetime
import logging
import sys
from collections.abc import Mapping
from typing import Any

import ray

from sis import config, episodic, gauntlet, llm
from sis.contract import REGISTERED_CONTRACTS, FeatureContract
from sis.ports import Severity
from sis.roles import (
    CEO,
    CTO,
    PM,
    QA,
    SWE,
    Designer,
    DevOps,
    ceo_config_from_env,
    ensure_brake_persistence,
    unreadable_brake_state,
)
from sis.self_model import get_self_model
from sis.settings import cached_settings, space_keys
from sis.version import code_version
from sis.workspace import get_workspace

CHARTER_TEXT = (
    "omnibase charter: make the server itself self-improving on a trivial internal "
    "target — receive a spec, generate code, validate it through the gauntlet, deploy "
    "it safely, and roll back on regression — before it models anything external. "
    "Hard constraints: the gauntlet is the only place generated code runs; the loop "
    "never merges to main or promotes to live; spend stays under the CEO's brakes."
)


# All detached actors share one namespace so a persistent/AWS cluster finds them
# across runs instead of duplicating them into fresh anonymous namespaces (M2).
NAMESPACE = "sis"

# The CEO is looked up by name from outside a cycle — the operator console reads
# its brakes (OMNI-28) — so the registered name is a constant rather than a
# literal repeated at each lookup site, which is how the two come to disagree.
CEO_NAME = "CEO"


def _get_or_create(name: str, cls: Any, *args: Any) -> Any:
    # get_if_exists is atomic: it returns the existing detached actor or creates
    # it, closing the race where two concurrent bootstraps both create (M2). On an
    # existing actor the constructor *args are ignored — it keeps its live state.
    return cls.options(
        name=name, namespace=NAMESPACE, lifetime="detached", get_if_exists=True
    ).remote(*args)


def page(
    workspace: Any, store: episodic.EpisodicStore, severity: Severity, title: str, body: str
) -> dict[str, str]:
    """Page a human through the Workspace's notifier (OMNI-62). Never raises.

    The notifier already reports its own failures; this adds the durable half —
    a page that could not be sent is recorded in the episodic store, so the
    run's own record says when nobody was told.
    """
    outcome: dict[str, str] = ray.get(workspace.notify.remote(severity, title, body))
    record_page_outcome(store, severity, title, outcome)
    if "delivered" in outcome:
        # On the console too (OMNI-122): on a supervised run it is where the
        # operator is looking, and the first AWS run's page left no trace there.
        print(f"[sis] paged ({severity.value}): {title}", file=sys.stderr)
    return outcome


def record_page_outcome(
    store: episodic.EpisodicStore, severity: Severity, title: str, outcome: dict[str, str]
) -> None:
    """Keep a failed page in the episodic store's state. Never raises."""
    if "error" not in outcome:
        return
    try:
        store.save_state("notifier", {"last_failure": {
            "title": title, "severity": severity.value, "error": outcome["error"]}})
    except Exception:  # noqa: BLE001 - notify() has already printed and emitted it
        pass


# The episodic-state key for a verified PR still awaiting a human (OMNI-126).
PENDING_PR_KEY = "pending_pr"


def durable_vcs() -> str | None:
    """``owner/repo`` when a PR outlives this process, else None.

    Only the real GitHub adapter qualifies. An in-memory PR dies with its
    adapter, and ids restart at PR-1 in the next process, so remembering one
    would only ever restore a hold on nothing.
    """
    if str(config.get("adapters.mode")) != "real":
        return None
    try:
        github = cached_settings().github
    except Exception:  # noqa: BLE001 - no settings means nothing durable to find
        return None
    return f"{github.owner}/{github.repo}" if github and github.owner and github.repo else None


def pending_pr_record(
    result: Mapping[str, Any], *, repo: str | None, now: str
) -> dict[str, Any] | None:
    """What a finished cycle leaves for the next process to wait on, or None. Pure.

    Only a verified candidate leaves a PR for a human, and only a durable
    version-control system (*repo* set) can still show it to the next process.
    The version is kept so the hold can be restored before the PR is checked:
    if that check fails, the hold is already in place (OMNI-126).
    """
    if repo is None or result.get("status") != "verified_awaiting_human_merge":
        return None
    pr_id, version = result.get("pr_id"), (result.get("canary") or {}).get("version")
    if not pr_id or not version:
        return None
    return {"pr_id": str(pr_id), "version": str(version),
            "contract": result.get("contract"), "repo": repo, "since": now}


def remember_pending_pr(
    store: episodic.EpisodicStore, result: Mapping[str, Any], *, repo: str | None
) -> None:
    """Persist a verified cycle's PR so a restart still waits for it. Never raises."""
    now = datetime.datetime.now(datetime.UTC).isoformat()
    if (record := pending_pr_record(result, repo=repo, now=now)) is None:
        return
    try:
        store.save_state(PENDING_PR_KEY, record)
    except Exception as exc:  # noqa: BLE001 - like the brake state: loud, never fatal
        print(f"[sis] WARNING: pending PR {record['pr_id']} not persisted; a restart "
              f"will not wait for it: {exc}", file=sys.stderr)


def forget_pending_pr(store: episodic.EpisodicStore, pr_id: str) -> None:
    """Drop the remembered PR once a human has decided it. Never raises.

    Only if it is *pr_id*: a stale caller must not clear a newer PR's hold.
    """
    try:
        record = store.load_state(PENDING_PR_KEY)
        if record and str(record.get("pr_id")) == str(pr_id):
            store.save_state(PENDING_PR_KEY, {})
    except Exception as exc:  # noqa: BLE001
        print(f"[sis] WARNING: resolved PR {pr_id} not forgotten; the next start "
              f"will check it again: {exc}", file=sys.stderr)


# How a hold ended by a human's decision is recorded (OMNI-57): an outcome of
# its own, never a gauntlet rejection and never a breaker count, because
# declining a change is a judgement on the change, not a failure of the loop.
RELEASE_OUTCOMES = {"closed without merging": "human_declined",
                    "no longer exists": "pr_vanished"}


def release_result(seen: Mapping[str, Any]) -> dict[str, Any] | None:
    """The episodic record of a released hold, as a cycle-result dict, or None. Pure.

    No ``reason`` key on purpose: the episodic log reads ``reason`` as a
    gauntlet rejection, and a human closing a PR is not one.
    """
    if not seen.get("released"):
        return None
    return {"status": RELEASE_OUTCOMES.get(str(seen.get("reason")), "released"),
            "pr_id": seen.get("pr"), "contract": seen.get("contract")}


def record_release(store: episodic.EpisodicStore, seen: Mapping[str, Any]) -> None:
    """Append a released hold to the episodic log, at no cost. Never raises."""
    if (result := release_result(seen)) is None:
        return
    try:
        store.append(episodic.event_from_cycle_result(result, proposer="human"))
    except Exception as exc:  # noqa: BLE001 - the log is auxiliary, as in run_cycle
        print(f"[sis] WARNING: {result['status']} for PR {result['pr_id']} not "
              f"recorded: {exc}", file=sys.stderr)


def restore_pending_pr(
    handles: Mapping[str, Any], store: episodic.EpisodicStore, *, repo: str | None
) -> str | None:
    """Put back the hold the last process left, resolved against the PR as it is now.

    Returns the console line saying what happened, or None if there was
    nothing to restore. Merged since → promoted; closed or gone → released;
    still open → held, so no cycle proposes the same change again (OMNI-126).
    """
    if repo is None:
        return None
    try:
        record = store.load_state(PENDING_PR_KEY)
    except episodic.StateUnreadable:
        return None  # the CEO has already booted with its breaker open over this file
    if not record or not record.get("pr_id"):
        return None
    pr_id, contract = str(record["pr_id"]), record.get("contract")
    label = f"PR {pr_id}" + (f" ({contract})" if contract else "")
    if record.get("repo") != repo:
        forget_pending_pr(store, pr_id)
        return (f"[sis] forgot pending {label}: it is in {record.get('repo')}, "
                f"this run uses {repo}")
    try:
        outcome = ray.get(handles["DevOps"].adopt_pending.remote(
            pr_id, str(record["version"]), contract))
    except Exception as exc:  # noqa: BLE001 - an outage keeps the hold; it never releases it
        return (f"[sis] HOLDING for {label} from the last run: could not check it on "
                f"{repo} ({exc}). No cycle starts until it can be checked.")
    if not outcome.get("adopted", False):
        return f"[sis] {label} from the last run not restored: {outcome.get('reason')}"
    if outcome.get("promoted") or outcome.get("merged"):
        forget_pending_pr(store, pr_id)
        return f"[sis] {label} was merged since the last run: promoted; cycles build on it"
    if outcome.get("released"):
        record_release(store, outcome)
        forget_pending_pr(store, pr_id)
        return f"[sis] {label} from the last run was {outcome.get('reason')}: hold released"
    return (f"[sis] HOLDING: {label} from the last run still awaits a human merge or "
            f"close on {repo}. No cycle starts until then.")


def hold_for_open_prs(
    handles: Mapping[str, Any], store: episodic.EpisodicStore
) -> tuple[bool, str | None]:
    """Whether a cycle must wait for a PR the VCS says is open: ``(hold, line)``.

    Asked before every cycle start (OMNI-136), because only the version-control
    system knows every PR still awaiting a human, whichever process or box
    opened it. :func:`restore_pending_pr` reads a file on this box; the third
    AWS run's box was rebuilt, and a second PR opened beside the first.

    Fails closed: a listing that fails holds, since "could not ask" is not
    "nothing open" (as OMNI-61's unreadable brake state is not "no spend"). A
    PR decided between the listing and the check releases as usual, but the
    hold stands while any other of the loop's PRs is still open; the next
    check adopts it.
    """
    try:
        outcome = ray.get(handles["DevOps"].adopt_open_pr.remote())
    except Exception as exc:  # noqa: BLE001 - an outage holds; it never lets a cycle through
        detail = " ".join(str(exc).split())[:200]
        return True, (f"[sis] HOLDING: could not list open PRs ({detail}). "
                      "No cycle starts until they can be checked.")
    if outcome is None:
        return False, None
    pr_id = str(outcome.get("pr"))
    others = [str(p) for p in outcome.get("open", []) if str(p) != pr_id]
    also = f" (also open: {', '.join(f'PR {p}' for p in others)})" if others else ""
    if not outcome.get("adopted", False):
        return True, (f"[sis] HOLDING: PR {pr_id} is open but not adopted: "
                      f"{outcome.get('reason')}{also}")
    if outcome.get("released"):
        record_release(store, outcome)
        forget_pending_pr(store, pr_id)
        return bool(others), f"[sis] PR {pr_id} was {outcome.get('reason')}: not held{also}"
    if outcome.get("promoted") or outcome.get("merged"):
        forget_pending_pr(store, pr_id)
        return bool(others), f"[sis] PR {pr_id} was merged: promoted; cycles build on it{also}"
    return True, (f"[sis] HOLDING: PR {pr_id} is open and awaits a human merge or close"
                  f"{also}. No cycle starts until then.")


def bootstrap() -> dict[str, Any]:
    """Start Ray, the shared substrate, and the named role actors."""
    # Before anything starts: brakes that reset on every restart are refused
    # whenever they protect real money or real systems (OMNI-61).
    ensure_brake_persistence()
    if str(config.get("episodic.store")) == "none":
        print("[sis] note: episodic.store=none — brake state is per-process and "
              "resets on restart (fine for the stub + in-memory adapters)", file=sys.stderr)

    ray.init(namespace=NAMESPACE, ignore_reinit_error=True, logging_level=logging.ERROR)

    # Shared substrate first, so role __init__ can register against it.
    workspace = get_workspace()
    self_model = get_self_model()

    # CEO spend brakes are env-configurable (M5). On a *fresh* CEO we also rehydrate
    # persisted brake/spend state from the episodic store (L9), so the spend cap and
    # breaker survive a cluster/actor restart; an already-running detached CEO keeps
    # its live state (get_if_exists ignores these args).
    ceo_cfg = ceo_config_from_env()
    store = episodic.get_episodic_store()
    held_open: str | None = None
    try:
        ceo_state = store.load_state("ceo")
    except episodic.StateUnreadable as exc:
        # Fail closed (OMNI-61): unreadable is not "no state". The CEO boots
        # with the breaker open and says why, instead of at spent=0.
        ceo_state = unreadable_brake_state(str(exc))
        held_open = str(ceo_state["trip_reason"])
        print(f"[sis] BRAKES HELD OPEN: {held_open}", file=sys.stderr)

    handles = {
        "Workspace": workspace,
        "SelfModel": self_model,
        "CEO": _get_or_create(
            CEO_NAME, CEO, ceo_cfg.budget_usd, ceo_cfg.breaker_threshold,
            ceo_cfg.max_cost_per_accepted_usd, ceo_cfg.slo_min_spend_usd,
            ceo_cfg.slo_failure_weight, ceo_state),
        "PM": _get_or_create("PM", PM),
        "CTO": _get_or_create("CTO", CTO),
        "Designer": _get_or_create("Designer", Designer),
        "SWE": _get_or_create("SWE", SWE),
        "QA": _get_or_create("QA", QA),
        "DevOps": _get_or_create("DevOps", DevOps),
    }

    # The CEO sets the top-level charter once (idempotent) — the goal the
    # provenance graph roots at: charter → spec → epic → story → outcome.
    ray.get(handles["CEO"].set_charter.remote(CHARTER_TEXT))

    # Register what each target is judged by. Idempotent, keyed by target path,
    # so a detached SelfModel surviving a restart just re-learns the same map.
    for contract in REGISTERED_CONTRACTS:
        ray.get(self_model.register_contract.remote(contract))

    # Name the code this run executes (OMNI-63): the provenance graph and the
    # episodic store both carry it, so a log synced off the box says which
    # commit made its decisions — and whether the tree was dirty.
    version = code_version()
    ray.get(self_model.record.remote("code", version["sha"], describe=version["describe"]))
    try:
        store.save_state("code_version", version)
    except Exception as exc:  # noqa: BLE001 - provenance must not stop a run
        print(f"[sis] WARNING: code version not persisted: {exc}", file=sys.stderr)
    print(f"[sis] running {version['describe']} ({version['sha']})", file=sys.stderr)

    # A PR the last run left for a human is still pending until a human says
    # otherwise (OMNI-126): restored before any cycle can propose it again.
    if (line := restore_pending_pr(handles, store, repo=durable_vcs())) is not None:
        print(line, file=sys.stderr)

    if held_open:
        page(workspace, store, Severity.CRITICAL, "brakes held open at startup",
             f"The CEO booted with its circuit breaker open: {held_open}")
    return handles


def neutral_cycle_status(impl: Mapping[str, Any]) -> str | None:
    """The neutral status an implementation outcome is recorded under, or None. Pure.

    Neutral means spend recorded, no bug filed, no breaker count: a verdict
    that says nothing against the loop. The gate decides for "no change" and
    "inconclusive" (``episodic.neutral_status``); the SWE decides for "no
    gain", because the same "no improvement" reason is neutral only when no
    feature is in progress (OMNI-138).
    """
    if impl.get("passed"):
        return None
    return episodic.neutral_status(impl.get("reason")) or (
        episodic.NO_GAIN if impl.get("no_gain") else None)


def cycle_summary(result: Mapping[str, Any]) -> str:
    """One console line: what a cycle did, why, and what it cost. Pure (OMNI-123).

    The first AWS run printed ``cycle status: rolled_back`` and nothing else;
    the reason ("no improvement: … ratio 1.3384, 95% interval [1.2271,
    1.4743]") was only in the episodic log synced off the box afterwards. On a
    supervised run the console is what the operator reads, so the gate and
    the reason go there, as the log records them.
    """
    status = str(result.get("status", "unknown"))
    if status == "paused":
        detail = f"paused by an operator: {result.get('pause_reason')}"
    elif status == "circuit_breaker_open":
        detail = "circuit breaker open, no cycle ran (python -m sis.admin status)"
    elif status == "budget_denied":
        detail = "spend cap reached, no cycle ran"
    elif status in ("verified_awaiting_human_merge", "feature_step"):
        base, cand = result.get("baseline_latency"), result.get("candidate_latency")
        timing = (f" (baseline {base:.6f}s -> candidate {cand:.6f}s)"
                  if isinstance(base, int | float) and isinstance(cand, int | float) else "")
        detail = (f"step {result.get('step')} committed to {result.get('branch')}{timing}"
                  if status == "feature_step"
                  else f"PR {result.get('pr_id')} awaits a human merge{timing}")
    else:
        reason = result.get("reason")
        if isinstance(reason, str) and reason:
            gate = episodic.gate_from_reason(reason) or "gate unknown"
            detail = f"{gate}: {' '.join(reason.split())}"
        else:
            detail = "no reason recorded"
    money = []
    cost, econ = result.get("cost_usd"), result.get("economics")
    if isinstance(cost, int | float):
        money.append(f"cost ${cost:.4f}")
    if isinstance(econ, Mapping):
        money.append(f"spent ${float(econ['spent_usd']):.4f} of "
                     f"${float(econ['budget_usd']):.2f}")
    return f"[cycle] {status}: {detail}" + (f" ({'; '.join(money)})" if money else "")


def rejection_bug(
    story_id: str, reason: str | None, *, pr_id: str | None = None
) -> tuple[str | None, str]:
    """The reject gate of a rejected cycle, and the summary its bug is filed under. Pure.

    One rule for the SWE's rejection and QA's (*pr_id* given), so neither
    loses what the other keeps (OMNI-56, M17): the gauntlet's reason, the gate
    the CEO weighs, and a harness fault filed as what it is (OMNI-37): the
    sandbox broke, the candidate was never judged.
    """
    gate = episodic.gate_from_reason(reason)
    where = f"QA of {story_id} (PR {pr_id})" if pr_id else story_id
    if gate == "harness":
        headline = f"Infrastructure fault (sandbox) during {where} — the candidate was not judged"
    elif pr_id:
        headline = f"QA rejected {story_id} (PR {pr_id})"
    else:
        headline = f"Cycle failed for {story_id}"
    return gate, f"{headline}: {reason}" if reason else headline


def cycle_outcome(approved: bool, canary: dict[str, Any] | None) -> tuple[str, bool, str | None]:
    """Fold QA's verdict and (if one ran) the canary's into one cycle outcome.

    Pure — no Ray, no I/O — per the project convention that decision logic is
    unit-testable without standing up a cluster (``evaluate_brakes``,
    ``gate_from_reason``, ``loop.decide``). Returns ``(status, success,
    canary_reason)``.

    Before OMNI-14, QA approval alone decided success; a live canary
    (OMNI-14, ``canary_backend="serve"``) can now reject a candidate QA
    already approved — exactly the failure mode it exists to catch, since it
    sees real concurrency and queueing the offline gauntlet cannot. That
    rejection has to be able to change the outcome, not just ride along in
    the returned dict unread. ``canary`` is None when QA rejected (no canary
    runs) or the legacy backend ran (no ``canary_passed`` key at all, so it
    defaults True and this reduces to the pre-OMNI-14 behaviour exactly).
    """
    canary_passed = True if canary is None else bool(canary.get("canary_passed", True))
    canary_reason = None if canary is None else canary.get("reason")
    success = approved and canary_passed
    status = ("verified_awaiting_human_merge" if success
              else "canary_rejected" if approved else "qa_rejected")
    return status, success, canary_reason


def run_cycle(
    handles: dict[str, Any],
    proposal_title: str,
    proposal_body: str,
    *,
    estimate_usd: float = 0.5,
    contract_name: str | None = None,
    canary_backend: str | None = None,
) -> dict[str, Any]:
    """Run one full intake→spec→epic→story→implement→review→canary cycle.

    *contract_name* selects which registered target to optimise or build (see
    ``sis.contract.REGISTERED_CONTRACTS``); None keeps the bootstrap target.
    Passed to BOTH the SWE and QA so they judge the candidate against the
    same oracle — and passed explicitly rather than via ``SIS_CONTRACT``
    because the role actors are separate processes that cannot see an env
    var exported after bootstrap().

    *canary_backend* selects ``DevOps.canary()``'s backend ("serve" for a real
    Ray Serve deployment judged against live traffic; anything else keeps the
    legacy in-memory/real ``Cloud`` recording). Same reasoning as
    *contract_name*: an explicit argument, not ``SIS_CANARY`` alone, because
    DevOps is an already-running actor by the time this runs."""
    # Fail fast, before any spend or artifacts: an untrusted (non-stub) proposer
    # requires the kernel-enforced docker sandbox so its code can't read host
    # credentials (KNOWN_ISSUES.md M1). validate() re-checks as a backstop.
    gauntlet.ensure_sandbox_allows_proposer()
    # Backstop for bootstrap()'s check: configuration can change between the
    # two (a CLI overlay, an edited config.yml), and this is before any spend.
    ensure_brake_persistence()
    # Same moment, same reason: the Serve canary runs candidate code as an
    # ordinary Ray worker, so it refuses anything but the stub's (OMNI-49).
    gauntlet.ensure_canary_allows_proposer(canary_backend)

    ws = handles["Workspace"]
    sm = handles["SelfModel"]
    # The Serve canary judges a candidate's latency against a reference, and a
    # Class-2 feature has neither (OMNI-147): refused before any spend.
    if (canary_backend or config.get("canary.backend")) == "serve":
        named = contract_name or config.get("contracts.default")
        if isinstance(ray.get(sm.contract_by_name.remote(named)) if named else None,
                      FeatureContract):
            raise RuntimeError(
                f"canary.backend='serve' cannot judge contract {named!r}: it is a feature "
                "(Class 2), with no reference or benchmark to compare. Leave canary.backend "
                "unset (the in-memory canary) for it.")
    ceo, pm, cto, designer, swe, qa, devops = (
        handles["CEO"], handles["PM"], handles["CTO"],
        handles["Designer"], handles["SWE"], handles["QA"], handles["DevOps"],
    )

    proposer = str(config.get("proposer.backend"))
    # The model recorded in the episodic log = whichever provider/model is
    # configured (sis.llm), not a hardcoded vendor. None for the stub.
    model = llm.configured_model() if proposer != "stub" else None
    store = episodic.get_episodic_store()

    # The contract this cycle runs against, as far as it is known yet: the
    # caller's choice until the SWE has resolved it (OMNI-121). _record stamps
    # it on every result, so the episodic log says which target a cycle was
    # about — the first AWS run's log could not.
    known_contract: dict[str, str | None] = {"name": contract_name}

    def _record(res: dict[str, Any], cost: float = 0.0) -> dict[str, Any]:
        res.setdefault("contract", known_contract["name"])
        # What the cycle cost and where spend stands, on every result — the
        # console line (cycle_summary, OMNI-123) reports both for every exit.
        res.setdefault("cost_usd", cost)
        res.setdefault("economics", ray.get(ceo.economics.remote()))
        # Episodic logging + CEO-state persistence are auxiliary — they must never
        # break a cycle. The driver is the single writer (keeps DuckDB happy).
        try:
            store.append(episodic.event_from_cycle_result(
                res, cost_usd=cost, proposer=proposer, model=model))
        except Exception:  # noqa: BLE001
            pass
        try:
            # Persist the CEO's brake/spend state so it survives a restart (L9).
            store.save_state("ceo", ray.get(ceo.state_snapshot.remote()))
        except Exception as exc:  # noqa: BLE001
            # Still never breaks the cycle — but never silent either. A refused
            # save is usually the store declining to overwrite state it could
            # not read, which the next bootstrap turns into an open breaker.
            print(f"[sis] WARNING: brake state not persisted: {exc}", file=sys.stderr)
            ws.emit.remote("brake_state.save_failed", error=str(exc))
        return res

    def _breaker_alarm(trip: str | None) -> str | None:
        """File the breaker bug and page a human — on a fresh trip only.

        The bug is the audit trail; the page is what reaches a person (OMNI-62).
        One helper for every place the CEO can report a trip, so no path files
        the bug and forgets the page.
        """
        if not trip:
            return None
        bug_id = str(ray.get(devops.file_bug.remote(
            f"CIRCUIT BREAKER OPEN — human attention required: {trip}")))
        econ = ray.get(ceo.economics.remote())
        page(ws, store, Severity.CRITICAL, f"circuit breaker open: {trip}",
             f"The loop has stopped starting cycles: {trip}.\n"
             f"Spent ${econ['spent_usd']:.4f} of ${econ['budget_usd']:.2f}; "
             f"accepted {int(econ['accepted'])}. Filed as {bug_id}.\n"
             "Inspect with `python -m sis.admin status`; clear it deliberately with "
             "`python -m sis.admin reset-breaker --reason \"...\"` (spend is not reset).")
        return bug_id

    # 1. Budget & goal gate (CEO). A pause is checked first and recorded as
    # its own status: an operator's decision, not a brake that tripped.
    if (paused := ray.get(ceo.pause_reason.remote())) is not None:
        # "pause_reason", not "reason": the episodic log reads "reason" as a
        # gauntlet rejection, and an operator's note is not one.
        return _record({"status": "paused", "pause_reason": paused})
    if ray.get(ceo.breaker_open.remote()):
        return _record({"status": "circuit_breaker_open"})
    if not ray.get(ceo.approve_budget.remote(estimate_usd)):
        econ = ray.get(ceo.economics.remote())
        page(ws, store, Severity.CRITICAL, "spend cap reached: cycle refused",
             f"A cycle estimated at ${estimate_usd:.2f} would exceed the budget: "
             f"spent ${econ['spent_usd']:.4f} of ${econ['budget_usd']:.2f}. No cycle "
             "runs until the budget (brakes.budget_usd) is raised.")
        return _record({"status": "budget_denied"})

    # 2–4 happen once per feature (OMNI-135): a step of a feature in progress
    # works under the plan its first step made, so a feature files one spec,
    # one epic and one story rather than one of each per cycle.
    plan = ray.get(cto.open_plan.remote(contract_name))
    if plan is None:
        # 2. Intake: a non-technical user drops a proposal into the proposal space.
        proposal = ray.get(ws.create_page.remote(
            space_keys()["proposal"], proposal_title, proposal_body, None, ["proposal"]))

        # 3. Spec & design (PM + Designer).
        spec_id = ray.get(pm.refine_proposal.remote(proposal.id))
        ray.get(designer.outline.remote(spec_id))

        # 4. Plan (CTO → Jira epic + stories).
        plan = ray.get(cto.plan.remote(spec_id, contract_name))
    elif plan.get("resumed"):
        print(f"[sis] carrying on {plan['resumed']} ({plan['steps']} step(s) committed), "
              "left unfinished by an earlier run", file=sys.stderr)
    spec_id = str(plan["spec_id"])
    story_id = str(plan["feature_story_id"])

    # 5. Implement (SWE → validated change on a feature branch + PR).
    impl = ray.get(swe.implement.remote(story_id, contract_name))
    cost_usd = float(impl.get("cost_usd", 0.0))
    known_contract["name"] = impl.get("contract", contract_name)

    # A "no change" outcome — the candidate is identical to the current baseline
    # — is not a failure: the loop correctly found nothing to improve. Record
    # the spend, but don't file a bug or count it against the circuit breaker
    # (three "nothing to do" cycles must not page a human). See KNOWN_ISSUES M3.
    # An inconclusive benchmark (OMNI-41) is benign the same way: the gate could
    # not tell the candidate from the margin, which says nothing against the
    # candidate. It keeps its own status so the log never calls it "no change".
    # So is a new feature that finds no further gain (OMNI-138): the target has
    # converged. loop.serve stops after loop.converged_after of these in a row.
    neutral_status = neutral_cycle_status(impl)
    if neutral_status:
        trip = ray.get(ceo.record_neutral.remote(cost_usd=cost_usd))
        breaker_bug_id = _breaker_alarm(trip)
        return _record({"status": neutral_status, "reason": impl["reason"],
                        "spec_id": spec_id, "story_id": story_id,
                        "candidate_sha": impl.get("candidate_sha"),
                        "breaker_bug_id": breaker_bug_id,
                        "economics": ray.get(ceo.economics.remote()),
                        "provenance": ray.get(sm.provenance.remote())}, cost_usd)

    if not impl["passed"]:
        gate, summary = rejection_bug(story_id, impl.get("reason"))
        # The gate is passed so the CEO can weigh a correct-but-over-budget
        # rejection (``slo``, OMNI-24) below a wrong one.
        trip = ray.get(ceo.report_outcome.remote(
            success=False, cost_usd=cost_usd, reject_gate=gate))
        # Failures become artifacts (ACTORS.md: DevOps files bug/defect Jiras).
        bug_id = ray.get(devops.file_bug.remote(summary))
        if gate == "harness":
            # A broken sandbox fails every cycle after this one too, and nothing
            # a proposer does will fix it — worth a person now, not after the
            # breaker has counted three of them (OMNI-62).
            page(ws, store, Severity.WARNING, f"sandbox broken during {story_id}",
                 f"The candidate was not judged: {impl['reason']}\nFiled as {bug_id}.")
        breaker_bug_id = _breaker_alarm(trip)
        return _record({"status": "rolled_back", "reason": impl["reason"],
                        "spec_id": spec_id, "story_id": story_id,
                        "candidate_sha": impl.get("candidate_sha"),
                        "bug_id": bug_id, "breaker_bug_id": breaker_bug_id,
                        "economics": ray.get(ceo.economics.remote()),
                        "provenance": ray.get(sm.provenance.remote())}, cost_usd)

    # A step committed to a feature branch (OMNI-130): accepted by the gauntlet,
    # but no PR until the feature is finished, so nothing for QA or a canary
    # yet. The feature keeps growing on the next cycle.
    if impl.get("feature_step"):
        trip = ray.get(ceo.report_outcome.remote(success=True, cost_usd=cost_usd))
        return _record({"status": "feature_step", "spec_id": spec_id, "story_id": story_id,
                        "branch": impl.get("branch"), "step": impl.get("step"),
                        "candidate_sha": impl.get("candidate_sha"),
                        "baseline_latency": impl.get("baseline"),
                        "candidate_latency": impl.get("candidate_latency"),
                        "breaker_bug_id": _breaker_alarm(trip),
                        "economics": ray.get(ceo.economics.remote()),
                        "provenance": ray.get(sm.provenance.remote())}, cost_usd)

    # 6. Verify (QA + deterministic gauntlet).
    approved, qa_reason = ray.get(qa.review.remote(story_id, impl["pr_id"], contract_name))

    # QA re-measures, so it can land on a neutral verdict the SWE's run did not
    # (OMNI-41: an inconclusive benchmark). Same fact, same handling — spend
    # recorded, no bug, no breaker increment — or a noisy re-measurement would
    # file a bug against a candidate the SWE's run had just accepted.
    qa_neutral = None if approved else episodic.neutral_status(qa_reason)
    if qa_neutral:
        trip = ray.get(ceo.record_neutral.remote(cost_usd=cost_usd))
        breaker_bug_id = _breaker_alarm(trip)
        return _record({"status": qa_neutral, "reason": qa_reason,
                        "spec_id": spec_id, "story_id": story_id,
                        "pr_id": impl["pr_id"],
                        "candidate_sha": impl.get("candidate_sha"),
                        "breaker_bug_id": breaker_bug_id,
                        "economics": ray.get(ceo.economics.remote()),
                        "provenance": ray.get(sm.provenance.remote())}, cost_usd)

    # 7. Canary deploy to the green slot (DevOps). Promotion to live is the
    #    human PR merge — intentionally NOT performed by the agent. On the
    #    legacy backend the offline latency is recorded as-is (the candidate
    #    is not re-run); on the "serve" backend this is a real deployment
    #    judged against live traffic (OMNI-14) and can itself reject a
    #    candidate QA already approved — the failure mode a canary exists to
    #    catch (real concurrency/queueing the offline gauntlet cannot see).
    canary = (ray.get(devops.canary.remote(
                  impl["pr_id"], impl["candidate_latency"], canary_backend))
              if approved else None)

    status, success, canary_reason = cycle_outcome(approved, canary)

    # 8. PM acceptance + CEO records the outcome + spend (drives the brakes).
    # A rejection keeps its reason and gate, as at the SWE stage (OMNI-56).
    reason = (canary_reason if status == "canary_rejected"
              else qa_reason if status == "qa_rejected" else None)
    ray.get(pm.accept.remote(spec_id, satisfied=success))
    trip = ray.get(ceo.report_outcome.remote(
        success=success, cost_usd=cost_usd, reject_gate=episodic.gate_from_reason(reason)))
    if status == "canary_rejected":
        bug_id = ray.get(devops.file_bug.remote(
            f"Live canary rejected {story_id} (PR {impl['pr_id']}): {canary_reason}"))
    elif status == "qa_rejected":
        qa_gate, summary = rejection_bug(story_id, qa_reason, pr_id=impl["pr_id"])
        bug_id = ray.get(devops.file_bug.remote(summary))
        if qa_gate == "harness":
            page(ws, store, Severity.WARNING, f"sandbox broken during QA of {story_id}",
                 f"The candidate was not judged: {qa_reason}\nFiled as {bug_id}.")
    else:
        bug_id = None
    breaker_bug_id = _breaker_alarm(trip)

    result = _record({
        "status": status,
        # Feeds episodic.event_from_cycle_result's existing reason/reject_gate
        # extraction (result.get("reason")) with zero new plumbing there —
        # CanaryVerdict.reason (evaluate_canary) is a distinct failure family
        # from the offline gauntlet's, so gate_from_reason grows matching names.
        "reason": reason,
        "bug_id": bug_id,
        "breaker_bug_id": breaker_bug_id,
        "spec_id": spec_id,
        "epic_id": plan["epic_id"],
        "story_id": story_id,
        "pr_id": impl["pr_id"],
        "candidate_sha": impl.get("candidate_sha"),
        "baseline_latency": impl["baseline"],
        "candidate_latency": impl["candidate_latency"],
        "canary": canary,
        "economics": ray.get(ceo.economics.remote()),
        "provenance": ray.get(sm.provenance.remote()),
    }, cost_usd)
    # A verified candidate waits for a human, possibly longer than this
    # process lives: remembered so the next start still waits (OMNI-126).
    remember_pending_pr(store, result, repo=durable_vcs())
    return result
