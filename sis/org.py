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
from typing import Any

import ray

from sis.roles import CEO, CTO, PM, QA, SWE, DevOps, Designer, PROPOSAL_SPACE
from sis.self_model import get_self_model
from sis.workspace import get_workspace


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

    # 1. Budget & goal gate (CEO).
    if ray.get(ceo.breaker_open.remote()):
        return {"status": "circuit_breaker_open"}
    if not ray.get(ceo.approve_budget.remote(estimate_usd)):
        return {"status": "budget_denied"}

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
        ray.get(ceo.report_outcome.remote(success=False, cost_usd=cost_usd))
        return {"status": "rolled_back", "reason": impl["reason"],
                "spec_id": spec_id, "story_id": story_id,
                "economics": ray.get(ceo.economics.remote()),
                "provenance": ray.get(sm.provenance.remote())}

    # 6. Verify (QA + deterministic gauntlet).
    approved = ray.get(qa.review.remote(story_id, impl["pr_id"]))

    # 7. Canary deploy to the green slot (DevOps). Promotion to live is the
    #    human PR merge — intentionally NOT performed by the agent.
    canary = ray.get(devops.canary.remote(impl["pr_id"])) if approved else None

    # 8. PM acceptance + CEO records the outcome + spend (drives the brakes).
    ray.get(pm.accept.remote(spec_id, satisfied=approved))
    ray.get(ceo.report_outcome.remote(success=approved, cost_usd=cost_usd))

    return {
        "status": "verified_awaiting_human_merge" if approved else "qa_rejected",
        "spec_id": spec_id,
        "epic_id": plan["epic_id"],
        "story_id": story_id,
        "pr_id": impl["pr_id"],
        "baseline_latency": impl["baseline"],
        "candidate_latency": impl["candidate_latency"],
        "canary": canary,
        "economics": ray.get(ceo.economics.remote()),
        "provenance": ray.get(sm.provenance.remote()),
    }
