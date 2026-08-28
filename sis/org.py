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

import logging
from typing import Any

import ray

from sis import config, episodic, gauntlet, llm
from sis.contract import DEFAULT_CONTRACTS
from sis.roles import CEO, CTO, PM, QA, SWE, Designer, DevOps, ceo_config_from_env
from sis.self_model import get_self_model
from sis.settings import space_keys
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


def bootstrap() -> dict[str, Any]:
    """Start Ray, the shared substrate, and the named role actors."""
    ray.init(namespace=NAMESPACE, ignore_reinit_error=True, logging_level=logging.ERROR)

    # Shared substrate first, so role __init__ can register against it.
    workspace = get_workspace()
    self_model = get_self_model()

    # CEO spend brakes are env-configurable (M5). On a *fresh* CEO we also rehydrate
    # persisted brake/spend state from the episodic store (L9), so the spend cap and
    # breaker survive a cluster/actor restart; an already-running detached CEO keeps
    # its live state (get_if_exists ignores these args).
    ceo_cfg = ceo_config_from_env()
    ceo_state = episodic.get_episodic_store().load_state("ceo")

    handles = {
        "Workspace": workspace,
        "SelfModel": self_model,
        "CEO": _get_or_create(
            CEO_NAME, CEO, ceo_cfg.budget_usd, ceo_cfg.breaker_threshold,
            ceo_cfg.max_cost_per_accepted_usd, ceo_cfg.slo_min_spend_usd, ceo_state),
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
    for contract in DEFAULT_CONTRACTS:
        ray.get(self_model.register_contract.remote(contract))
    return handles


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

    *contract_name* selects which registered target to optimise (see
    ``sis.contract.DEFAULT_CONTRACTS``); None keeps the bootstrap target.
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

    ws = handles["Workspace"]
    sm = handles["SelfModel"]
    ceo, pm, cto, designer, swe, qa, devops = (
        handles["CEO"], handles["PM"], handles["CTO"],
        handles["Designer"], handles["SWE"], handles["QA"], handles["DevOps"],
    )

    proposer = str(config.get("proposer.backend"))
    # The model recorded in the episodic log = whichever provider/model is
    # configured (sis.llm), not a hardcoded vendor. None for the stub.
    model = llm.configured_model() if proposer != "stub" else None
    store = episodic.get_episodic_store()

    def _record(res: dict[str, Any], cost: float = 0.0) -> dict[str, Any]:
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
        except Exception:  # noqa: BLE001
            pass
        return res

    # 1. Budget & goal gate (CEO).
    if ray.get(ceo.breaker_open.remote()):
        return _record({"status": "circuit_breaker_open"})
    if not ray.get(ceo.approve_budget.remote(estimate_usd)):
        return _record({"status": "budget_denied"})

    # 2. Intake: a non-technical user drops a proposal into the proposal space.
    proposal = ray.get(ws.create_page.remote(
        space_keys()["proposal"], proposal_title, proposal_body, None, ["proposal"]))

    # 3. Spec & design (PM + Designer).
    spec_id = ray.get(pm.refine_proposal.remote(proposal.id))
    ray.get(designer.outline.remote(spec_id))

    # 4. Plan (CTO → Jira epic + stories).
    plan = ray.get(cto.plan.remote(spec_id))
    story_id = plan["feature_story_id"]

    # 5. Implement (SWE → validated change on a feature branch + PR).
    impl = ray.get(swe.implement.remote(story_id, contract_name))
    cost_usd = float(impl.get("cost_usd", 0.0))

    # A "no change" outcome — the candidate is identical to the current baseline
    # — is not a failure: the loop correctly found nothing to improve. Record
    # the spend, but don't file a bug or count it against the circuit breaker
    # (three "nothing to do" cycles must not page a human). See KNOWN_ISSUES M3.
    if not impl["passed"] and episodic.gate_from_reason(impl.get("reason")) == "noop":
        trip = ray.get(ceo.record_neutral.remote(cost_usd=cost_usd))
        breaker_bug_id = (
            ray.get(devops.file_bug.remote(
                f"CIRCUIT BREAKER OPEN — human attention required: {trip}"))
            if trip else None
        )
        return _record({"status": "no_change", "reason": impl["reason"],
                        "spec_id": spec_id, "story_id": story_id,
                        "candidate_sha": impl.get("candidate_sha"),
                        "breaker_bug_id": breaker_bug_id,
                        "economics": ray.get(ceo.economics.remote()),
                        "provenance": ray.get(sm.provenance.remote())}, cost_usd)

    if not impl["passed"]:
        trip = ray.get(ceo.report_outcome.remote(success=False, cost_usd=cost_usd))
        # Failures become artifacts (ACTORS.md: DevOps files bug/defect Jiras).
        bug_id = ray.get(devops.file_bug.remote(
            f"Cycle failed for {story_id}: {impl['reason']}"))
        breaker_bug_id = (
            ray.get(devops.file_bug.remote(
                f"CIRCUIT BREAKER OPEN — human attention required: {trip}"))
            if trip else None
        )
        return _record({"status": "rolled_back", "reason": impl["reason"],
                        "spec_id": spec_id, "story_id": story_id,
                        "candidate_sha": impl.get("candidate_sha"),
                        "bug_id": bug_id, "breaker_bug_id": breaker_bug_id,
                        "economics": ray.get(ceo.economics.remote()),
                        "provenance": ray.get(sm.provenance.remote())}, cost_usd)

    # 6. Verify (QA + deterministic gauntlet).
    approved = ray.get(qa.review.remote(story_id, impl["pr_id"], contract_name))

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
    ray.get(pm.accept.remote(spec_id, satisfied=success))
    trip = ray.get(ceo.report_outcome.remote(success=success, cost_usd=cost_usd))
    if status == "canary_rejected":
        bug_id = ray.get(devops.file_bug.remote(
            f"Live canary rejected {story_id} (PR {impl['pr_id']}): {canary_reason}"))
    elif status == "qa_rejected":
        bug_id = ray.get(devops.file_bug.remote(
            f"QA rejected {story_id} (PR {impl['pr_id']})"))
    else:
        bug_id = None
    breaker_bug_id = (
        ray.get(devops.file_bug.remote(
            f"CIRCUIT BREAKER OPEN — human attention required: {trip}"))
        if trip else None
    )

    return _record({
        "status": status,
        # Feeds episodic.event_from_cycle_result's existing reason/reject_gate
        # extraction (result.get("reason")) with zero new plumbing there —
        # CanaryVerdict.reason (evaluate_canary) is a distinct failure family
        # from the offline gauntlet's, so gate_from_reason grows matching names.
        "reason": canary_reason if status == "canary_rejected" else None,
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
