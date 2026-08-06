"""sis.roles — the actor org (CEO, PM, CTO, SWE, QA, DevOps, Designer).

Each role is a Ray actor whose mandate, relationships, and real-world
connections follow ACTORS.md. Roles do not chat; they coordinate by reading
and writing artifacts through the shared :class:`~sis.workspace.Workspace`
and they record provenance in the :class:`~sis.self_model.SelfModel`.

The SWE reuses the existing self-improvement machinery (:mod:`sis.proposer`
+ :mod:`sis.gauntlet`): the validated change rides in a PR and a green
canary; it is never written to the live target or merged to main by the
agent (the human PR is mandatory — gauntlet step 6).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import ray

from sis import gauntlet, policy, proposer
from sis.paths import TARGET_PATH
from sis.ports import IssueStatus, IssueType
from sis.self_model import get_self_model
from sis.settings import space_keys, version_control_base
from sis.workspace import get_workspace

# CEO spend-brake defaults — the single source of truth, shared by CEO.__init__
# and ceo_config_from_env so an env-configured run and a default run agree.
DEFAULT_BUDGET_USD = 5.0
DEFAULT_BREAKER_THRESHOLD = 3
DEFAULT_MAX_COST_PER_ACCEPTED_USD = 2.0
DEFAULT_SLO_MIN_SPEND_USD = 0.50

# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CEOConfig:
    """The CEO's spend brakes — the hard cap and the two SLO thresholds."""

    budget_usd: float = DEFAULT_BUDGET_USD
    breaker_threshold: int = DEFAULT_BREAKER_THRESHOLD
    max_cost_per_accepted_usd: float = DEFAULT_MAX_COST_PER_ACCEPTED_USD
    slo_min_spend_usd: float = DEFAULT_SLO_MIN_SPEND_USD


def _env_number(env: Mapping[str, str], name: str, default: float, *, cast: Any) -> Any:
    """Parse an env var as a number, or fail loudly. A bad spend cap must never
    silently fall back to the permissive default (a typo'd '0.1O' that becomes
    $5 defeats the whole point of the budget gate) — so raise, don't shrug."""
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a valid number") from None
    if value < 0:
        raise ValueError(f"{name}={raw!r} must not be negative")
    return value


def ceo_config_from_env(env: Mapping[str, str] | None = None) -> CEOConfig:
    """Build the CEO's spend brakes from the environment (pure — unit-testable).

    Lets a run set a deliberately tiny budget without editing source
    (KNOWN_ISSUES.md M5): ``SIS_BUDGET_USD``, ``SIS_BREAKER_THRESHOLD``,
    ``SIS_MAX_COST_PER_ACCEPTED_USD``, ``SIS_SLO_MIN_SPEND_USD``. Each falls back
    to its ``DEFAULT_*`` when unset; an unparseable/negative value raises.
    """
    e = os.environ if env is None else env
    return CEOConfig(
        budget_usd=_env_number(e, "SIS_BUDGET_USD", DEFAULT_BUDGET_USD, cast=float),
        breaker_threshold=_env_number(
            e, "SIS_BREAKER_THRESHOLD", DEFAULT_BREAKER_THRESHOLD, cast=int),
        max_cost_per_accepted_usd=_env_number(
            e, "SIS_MAX_COST_PER_ACCEPTED_USD", DEFAULT_MAX_COST_PER_ACCEPTED_USD, cast=float),
        slo_min_spend_usd=_env_number(
            e, "SIS_SLO_MIN_SPEND_USD", DEFAULT_SLO_MIN_SPEND_USD, cast=float),
    )


def evaluate_brakes(
    *,
    spent: float,
    budget: float,
    consecutive_failures: int,
    threshold: int,
    accepted: int,
    max_cost_per_accepted: float,
    slo_min_spend: float,
) -> str | None:
    """Return the name of the tripped brake, or None. Pure — easy to unit-test.

    Order is intentional: the hard spend cap dominates, then the regression
    breaker, then the economics SLO (only judged once real money is spent).
    """
    if spent > budget:
        return "hard spend cap exceeded"
    if consecutive_failures >= threshold:
        return "consecutive failure threshold"
    cost_per_accepted = spent / accepted if accepted else float("inf")
    if spent >= slo_min_spend and cost_per_accepted > max_cost_per_accepted:
        return "cost-per-accepted-improvement SLO breached"
    return None


class Role:
    """Base for every role actor: registers itself and exposes the shared substrate."""

    def __init__(self, name: str, role: str, parent: str | None = None) -> None:
        self.name = name
        self.role = role
        self._sm = get_self_model()
        self._ws = get_workspace()
        ray.get(self._sm.register.remote(name, role, parent))


# --------------------------------------------------------------------------
# Leadership (named, detached)
# --------------------------------------------------------------------------


@ray.remote
class CEO(Role):
    """Goals & cost: budget gate + circuit-breaker authority.

    Three independent brakes (any one trips the breaker):
      1. Hard spend cap — total LLM $ may never exceed ``budget_usd``.
      2. Consecutive-failure breaker — N regressed/rolled-back cycles.
      3. Cost-per-accepted-improvement SLO — once real money has been spent,
         the $ per *accepted* improvement must stay under the ceiling. This
         catches the "many low-value cycles that each cost money" failure the
         regression breaker alone misses.
    """

    def __init__(
        self,
        budget_usd: float = DEFAULT_BUDGET_USD,
        breaker_threshold: int = DEFAULT_BREAKER_THRESHOLD,
        max_cost_per_accepted_usd: float = DEFAULT_MAX_COST_PER_ACCEPTED_USD,
        slo_min_spend_usd: float = DEFAULT_SLO_MIN_SPEND_USD,
        state: dict[str, Any] | None = None,
    ) -> None:
        super().__init__("CEO", "CEO")
        self._budget = budget_usd
        self._spent = 0.0
        self._threshold = breaker_threshold
        self._max_cost_per_accepted = max_cost_per_accepted_usd
        self._slo_min_spend = slo_min_spend_usd  # don't judge the SLO on pennies
        self._consecutive_failures = 0
        self._accepted = 0
        self._tripped = False
        self._charter_id: str | None = None
        # Rehydrate persisted brake/spend state (L9) — only on a *fresh* actor.
        # A detached CEO that already exists (get_if_exists) keeps its live state;
        # this path runs on first bootstrap or after a cluster/actor restart.
        if state:
            self._spent = float(state.get("spent_usd", 0.0))
            self._consecutive_failures = int(state.get("consecutive_failures", 0))
            self._accepted = int(state.get("accepted", 0))
            self._tripped = bool(state.get("tripped", False))

    def approve_budget(self, estimate_usd: float) -> bool:
        """Goal/cost gate: refuse if this attempt would breach the hard cap."""
        if self._tripped:
            return False
        if self._spent + estimate_usd > self._budget:
            ray.get(self._ws.emit.remote("budget.denied", estimate=estimate_usd,
                                         spent=self._spent, budget=self._budget))
            return False
        ray.get(self._ws.emit.remote("budget.approved", estimate=estimate_usd,
                                     spent=self._spent, budget=self._budget))
        return True

    def report_outcome(self, *, success: bool, cost_usd: float = 0.0) -> str | None:
        """Record real spend + outcome, then evaluate all three brakes.

        Returns the brake reason **on a fresh trip** (None otherwise) so the
        caller can raise the alarm — per ACTORS.md, DevOps files the bug that
        "pages a human".
        """
        self._spent += cost_usd
        if success:
            self._consecutive_failures = 0
            self._accepted += 1
        else:
            self._consecutive_failures += 1
        return self._evaluate_brakes()

    def record_neutral(self, *, cost_usd: float = 0.0) -> str | None:
        """Record a neutral cycle — the loop found nothing to improve (a no-op).

        Not a failure (no regression) and not an acceptance (nothing shipped),
        so the failure and accept counters are left untouched — a no-op must
        never trip the consecutive-failure breaker. Spend is still recorded, so
        the hard spend cap and the cost-per-accepted SLO still apply: many
        paid-for no-op cycles that accept nothing are exactly what the SLO
        catches. Returns the brake reason on a fresh trip, else None.
        """
        self._spent += cost_usd
        return self._evaluate_brakes()

    def _evaluate_brakes(self) -> str | None:
        """Evaluate all three brakes against current state; trip once."""
        reason = evaluate_brakes(
            spent=self._spent,
            budget=self._budget,
            consecutive_failures=self._consecutive_failures,
            threshold=self._threshold,
            accepted=self._accepted,
            max_cost_per_accepted=self._max_cost_per_accepted,
            slo_min_spend=self._slo_min_spend,
        )
        if reason and not self._tripped:
            self._tripped = True
            ray.get(self._ws.emit.remote("breaker.tripped", reason=reason,
                                         **self.economics()))
            return reason
        return None

    def _cost_per_accepted(self) -> float:
        # No acceptances yet but money spent → treat as infinite (worst case).
        return self._spent / self._accepted if self._accepted else float("inf")

    def breaker_open(self) -> bool:
        return self._tripped

    def economics(self) -> dict[str, float]:
        cpa = self._cost_per_accepted()
        return {
            "spent_usd": round(self._spent, 6),
            "budget_usd": self._budget,
            "accepted": float(self._accepted),
            "cost_per_accepted_usd": cpa if cpa != float("inf") else -1.0,
        }

    def state_snapshot(self) -> dict[str, Any]:
        """The persistable brake/spend state (L9) — what the driver writes to the
        episodic store after each cycle and rehydrates on a fresh bootstrap."""
        return {
            "spent_usd": self._spent,
            "consecutive_failures": self._consecutive_failures,
            "accepted": self._accepted,
            "tripped": self._tripped,
        }

    def reset_breaker(self) -> bool:
        """Admin reset of the circuit breaker (clears the tripped flag + failure
        streak). Spend is deliberately **not** reset — it is a financial guardrail,
        so clearing the breaker can't bypass the hard cap (see
        docs/BRAKE_STATE_AND_ORACLE.md §4.1). A spend-cap trip therefore re-trips on
        the next evaluation until the budget is raised."""
        self._tripped = False
        self._consecutive_failures = 0
        ray.get(self._ws.emit.remote("breaker.reset", **self.economics()))
        return True

    def set_charter(self, text: str) -> str:
        """Write the top-level charter page (once — idempotent per CEO lifetime).

        Per ACTORS.md the CEO "writes rarely — sets the high-level charter";
        the provenance graph roots at this page: charter → spec → epic → story.
        """
        if self._charter_id is not None:
            return self._charter_id
        page = ray.get(self._ws.create_page.remote(
            space_keys()["charter"], "Project Charter", text, None, ["charter"]))
        ray.get(self._sm.record.remote("charter", page.id))
        self._charter_id = str(page.id)
        return self._charter_id


@ray.remote
class Designer(Role):
    """UI / higher-level outline; writes in Confluence. Spawned by the PM."""

    def __init__(self) -> None:
        super().__init__("Designer", "Designer", parent="PM")

    def outline(self, spec_page_id: str) -> str:
        spec = ray.get(self._ws.get_page.remote(spec_page_id))
        page = ray.get(self._ws.create_page.remote(
            space_keys()["spec"], f"Outline — {spec.title}",
            "High-level outline and UX notes derived from the spec.",
            spec_page_id, ["outline"]))
        return str(page.id)


@ray.remote
class PM(Role):
    """User experience & specs: refines intake proposals into specs in Confluence."""

    def __init__(self) -> None:
        super().__init__("PM", "PM")

    def refine_proposal(self, proposal_page_id: str) -> str:
        """Turn a raw proposal into a structured spec page (from a fixed template)."""
        proposal = ray.get(self._ws.get_page.remote(proposal_page_id))
        body = (
            f"# Spec: {proposal.title}\n\n"
            f"## Problem\n{proposal.body}\n\n"
            "## Acceptance criteria\n"
            "- Behaviour matches this spec\n"
            "- Deterministic gauntlet passes (types, tests, benchmark)\n"
            "- Change beats the current baseline\n"
        )
        spec = ray.get(self._ws.create_page.remote(
            space_keys()["spec"], f"Spec — {proposal.title}", body, proposal_page_id, ["spec"]))
        ray.get(self._sm.record.remote("spec", spec.id, source_proposal=proposal_page_id))
        ray.get(self._ws.emit.remote("spec.authored", spec_id=spec.id))
        return str(spec.id)

    def accept(self, spec_page_id: str, *, satisfied: bool) -> bool:
        """Final acceptance of behaviour vs spec (PM reviews QA outcome)."""
        ray.get(self._ws.emit.remote("spec.acceptance", spec_id=spec_page_id, satisfied=satisfied))
        return satisfied


@ray.remote
class CTO(Role):
    """Technical execution: Confluence spec → Jira epic + initial stories."""

    def __init__(self) -> None:
        super().__init__("CTO", "CTO")

    def plan(self, spec_page_id: str) -> dict[str, str]:
        spec = ray.get(self._ws.get_page.remote(spec_page_id))
        epic = ray.get(self._ws.create_issue.remote(IssueType.EPIC, f"Epic: {spec.title}", None))
        infra = ray.get(self._ws.create_issue.remote(
            IssueType.STORY, "Infra: ensure CI + sandbox runner", epic.id))
        feature = ray.get(self._ws.create_issue.remote(
            IssueType.STORY, f"Implement: {spec.title}", epic.id))
        ray.get(self._ws.transition.remote(feature.id, IssueStatus.TODO, "Planned by CTO"))
        ray.get(self._sm.record.remote("epic", epic.id, spec=spec_page_id))
        ray.get(self._sm.record.remote("story", feature.id, epic=epic.id))
        return {"epic_id": str(epic.id), "infra_story_id": str(infra.id),
                "feature_story_id": str(feature.id)}


# --------------------------------------------------------------------------
# Engineering (spawned/owned by the CTO)
# --------------------------------------------------------------------------


@ray.remote
class SWE(Role):
    """Implementation: proposes a validated change on a feature branch + PR."""

    def __init__(self) -> None:
        super().__init__("SWE", "SWE", parent="CTO")

    def implement(self, story_id: str) -> dict[str, Any]:
        ray.get(self._ws.transition.remote(story_id, IssueStatus.IN_PROGRESS, "SWE picked up"))

        # Start from the target as merged on the base branch, so a cycle that
        # follows a merged optimisation builds on it instead of re-proposing
        # against the stale local file. Falls back to the local file when
        # version control has no merged source (the in-memory path, or a target
        # not yet committed to the base).
        merged_source = ray.get(self._ws.live_target_source.remote())
        origin = "merged_base" if merged_source else "local_file"
        current_source = merged_source or TARGET_PATH.read_text(encoding="utf-8")
        ray.get(self._ws.emit.remote("target.source", story_id=story_id, origin=origin))
        baseline = gauntlet.measure_baseline(current_source)  # sandboxed, not in-process
        candidate = proposer.propose(current_source, baseline)
        candidate_sha = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12]
        cost_usd = proposer.last_cost_usd()  # 0.0 for the stub; real $ for Claude
        # Benchmark the candidate against the source the cycle is based on (the
        # merged target), not the stale local file — see KNOWN_ISSUES.md H1.
        report = gauntlet.validate(candidate, baseline, baseline_source=current_source)

        if not report.passed:
            ray.get(self._ws.transition.remote(
                story_id, IssueStatus.TBD, f"Gauntlet failed: {report.reason}"))
            ray.get(self._sm.record.remote("outcome", story_id, passed=False, reason=report.reason))
            return {"passed": False, "reason": report.reason, "pr_id": None,
                    "cost_usd": cost_usd, "candidate_sha": candidate_sha}

        # Change-authorization policy: the loop may only write paths its tier
        # permits. The target is SOFT (allowed once checks pass); a mis-pointed
        # guardrail/engine path is refused here, before any branch or PR.
        decision = policy.authorize_change(TARGET_PATH, checks_passed=report.passed)
        if not decision.allowed:
            ray.get(self._ws.transition.remote(
                story_id, IssueStatus.TBD, f"Policy blocked: {decision.reason}"))
            ray.get(self._sm.record.remote(
                "outcome", story_id, passed=False, reason=f"policy: {decision.reason}"))
            return {"passed": False, "reason": f"policy: {decision.reason}",
                    "pr_id": None, "cost_usd": cost_usd, "candidate_sha": candidate_sha}

        # Fork from the same base the merged target was read from, not a
        # hardcoded "main" — see KNOWN_ISSUES.md M4.
        branch = f"feature/{story_id.lower()}"
        ray.get(self._ws.create_branch.remote(branch, version_control_base()))
        ray.get(self._ws.commit.remote(branch, f"Optimise target for {story_id}"))
        pr = ray.get(self._ws.open_pr.remote(branch, f"Optimise target ({story_id})", candidate))
        ray.get(self._ws.transition.remote(
            story_id, IssueStatus.READY_FOR_REVIEW, f"PR {pr.id} ready"))
        ray.get(self._sm.record.remote("branch", branch, story=story_id))
        ray.get(self._sm.record.remote(
            "pr", pr.id, story=story_id,
            baseline=baseline, candidate=report.latency_seconds))
        return {"passed": True, "pr_id": str(pr.id), "branch": branch,
                "baseline": baseline, "candidate_latency": report.latency_seconds,
                "cost_usd": cost_usd, "candidate_sha": candidate_sha}


@ray.remote
class QA(Role):
    """Verification: acts when a story is Ready for Review; augments the gauntlet."""

    def __init__(self) -> None:
        super().__init__("QA", "QA", parent="CTO")

    def review(self, story_id: str, pr_id: str) -> bool:
        issue = ray.get(self._ws.get_issue.remote(story_id))
        pr = ray.get(self._ws.get_pr.remote(pr_id))
        # Deterministic gate already ran in the SWE step; QA confirms the
        # artifact exists, matches the story, and re-runs the gauntlet.
        ok = bool(pr.artifact) and issue.status == IssueStatus.READY_FOR_REVIEW
        if ok:
            # Re-run the gauntlet: the candidate executes ONLY inside its sandbox.
            # Benchmark against the same merged baseline the SWE used (the target
            # as merged on the base branch), not the stale local file — H1.
            merged = ray.get(self._ws.live_target_source.remote())
            baseline_source = merged or TARGET_PATH.read_text(encoding="utf-8")
            report = gauntlet.validate(pr.artifact, 0.0, baseline_source=baseline_source)
            ok = report.passed
        if ok:
            ray.get(self._ws.transition.remote(story_id, IssueStatus.DONE, "QA verified"))
        else:
            ray.get(self._ws.transition.remote(story_id, IssueStatus.TBD, "QA found discrepancy"))
        ray.get(self._sm.record.remote("outcome", story_id, passed=ok, by="QA"))
        return ok


@ray.remote
class DevOps(Role):
    """Infra & ops: canary deploy to the green slot; files bugs; feeds SelfModel."""

    def __init__(self) -> None:
        super().__init__("DevOps", "DevOps", parent="CTO")

    def canary(self, pr_id: str, candidate_latency: float) -> dict[str, Any]:
        # candidate_latency was measured inside the gauntlet sandbox by the SWE
        # step. The candidate is NEVER executed here (main process, holds creds).
        pr = ray.get(self._ws.get_pr.remote(pr_id))
        version = f"{pr.branch}@{pr.id}"
        record = ray.get(self._ws.deploy_canary.remote(
            version, {"latency_seconds": candidate_latency}))
        ray.get(self._sm.set_slot.remote("green", version))
        ray.get(self._sm.record.remote(
            "canary", version, pr=pr_id, latency=candidate_latency))
        return {"version": version, "slot": record.slot,
                "latency_seconds": candidate_latency, "live": record.live}

    def file_bug(self, summary: str) -> str:
        issue = ray.get(self._ws.create_issue.remote(IssueType.BUG, summary, None))
        ray.get(self._sm.record.remote("bug", issue.id, summary=summary))
        return str(issue.id)
