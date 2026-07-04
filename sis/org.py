"""sis.org — bootstrap the actor org and drive one intake→deploy cycle.

This wires the roles onto the control loop exactly as ACTORS.md maps them:

    budget/goal gate   → CEO
    spec / design      → PM (+ Designer), in Confluence
    plan (epics)       → CTO, in Jira
    propose / implement→ SWE, on a feature branch (reuses proposer+gauntlet)
    verify             → QA + the deterministic gauntlet
    canary deploy      → DevOps via the Cloud port (green slot)
    promote / rollback → human PR merge (left pending) / DevOps
    circuit breaker    → CEO authority

Everything is coordinated through durable artifacts in the shared Workspace,
and every handoff is recorded in the SelfModel provenance graph.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import ray

from sis import episodic
from sis.proposer import MODEL
from sis.roles import CEO, CTO, PM, PROPOSAL_SPACE, QA, SWE, Designer, DevOps
from sis.self_model import get_self_model
from sis.workspace import get_workspace

CHARTER_TEXT = (
    "omnibase charter: make the server itself self-improving on a trivial internal "
    "target — receive a spec, generate code, validate it through the gauntlet, deploy "
    "it safely, and roll back on regression — before it models anything external. "
    "Hard constraints: the gauntlet is the only place generated code runs; the loop "
    "never merges to main or promotes to live; spend stays under the CEO's brakes."
)


def _get_or_create(name: str, cls: Any, *args: Any) -> Any:
    try:
        return ray.get_actor(name)
    except ValueError:
        return cls.options(name=name, lifetime="detached").remote(*args)


def bootstrap() -> dict[str, Any]:
    """Start Ray, the shared substrate, and the named role actors."""
    ray.init(ignore_reinit_error=True, logging_level=logging.ERROR)

    # Shared substrate first, so role __init__ can register against it.
    workspace = get_workspace()
    self_model = get_self_model()

    handles = {
        "Workspace": workspace,
        "SelfModel": self_model,
        "CEO": _get_or_create("CEO", CEO),
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
    return handles


def run_cycle(
    handles: dict[str, Any],
    proposal_title: str,
    proposal_body: str,
    *,
    estimate_usd: float = 0.5,
) -> dict[str, Any]:
    """Run one full intake→spec→epic→story→implement→review→canary cycle."""
    ws = handles["Workspace"]
    sm = handles["SelfModel"]
    ceo, pm, cto, designer, swe, qa, devops = (
        handles["CEO"], handles["PM"], handles["CTO"],
        handles["Designer"], handles["SWE"], handles["QA"], handles["DevOps"],
    )

    proposer = os.getenv("SIS_PROPOSER", "stub")
    model = MODEL if proposer == "claude" else None
    store = episodic.get_episodic_store()

    def _record(res: dict[str, Any], cost: float = 0.0) -> dict[str, Any]:
        # Episodic logging is auxiliary — it must never break a cycle.
        try:
            store.append(episodic.event_from_cycle_result(
                res, cost_usd=cost, proposer=proposer, model=model))
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
        PROPOSAL_SPACE, proposal_title, proposal_body, None, ["proposal"]))

    # 3. Spec & design (PM + Designer).
    spec_id = ray.get(pm.refine_proposal.remote(proposal.id))
    ray.get(designer.outline.remote(spec_id))

    # 4. Plan (CTO → Jira epic + stories).
    plan = ray.get(cto.plan.remote(spec_id))
    story_id = plan["feature_story_id"]

    # 5. Implement (SWE → validated change on a feature branch + PR).
    impl = ray.get(swe.implement.remote(story_id))
    cost_usd = float(impl.get("cost_usd", 0.0))
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
    approved = ray.get(qa.review.remote(story_id, impl["pr_id"]))

    # 7. Canary deploy to the green slot (DevOps). Promotion to live is the
    #    human PR merge — intentionally NOT performed by the agent. The latency
    #    was measured inside the gauntlet sandbox; the candidate is not re-run.
    canary = (ray.get(devops.canary.remote(impl["pr_id"], impl["candidate_latency"]))
              if approved else None)

    # 8. PM acceptance + CEO records the outcome + spend (drives the brakes).
    ray.get(pm.accept.remote(spec_id, satisfied=approved))
    trip = ray.get(ceo.report_outcome.remote(success=approved, cost_usd=cost_usd))
    bug_id = (ray.get(devops.file_bug.remote(f"QA rejected {story_id} (PR {impl['pr_id']})"))
              if not approved else None)
    breaker_bug_id = (
        ray.get(devops.file_bug.remote(
            f"CIRCUIT BREAKER OPEN — human attention required: {trip}"))
        if trip else None
    )

    return _record({
        "status": "verified_awaiting_human_merge" if approved else "qa_rejected",
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
