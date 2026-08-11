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
import pathlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

import ray

from sis import contract, contract_author, gauntlet, policy, proposer
from sis.canary import DEFAULT_MIN_CANARY_SAMPLES, CanaryMode, evaluate_canary
from sis.paths import PROJECT_ROOT, TARGET_PATH
from sis.ports import IssueStatus, IssueType, PullRequest
from sis.self_model import get_self_model
from sis.settings import space_keys, version_control_base
from sis.workspace import get_workspace

# How many synthetic requests DevOps drives through a live canary to fill its
# window (OMNI-14). Nothing external calls the served target yet
# (docs/SERVE_CANARY.md's bootstrap problem), so the window has to be filled
# the same way manual testing already does (sis.loadgen) rather than waiting
# on organic traffic that will never arrive. Comfortably above
# evaluate_canary's own evidence floor, with headroom for a candidate that
# fails some fraction of requests (a failed dispatch is not a paired sample).
LIVE_CANARY_REQUESTS = 150
LIVE_CANARY_CONCURRENCY = 8

# CEO spend-brake defaults — the single source of truth, shared by CEO.__init__
# and ceo_config_from_env so an env-configured run and a default run agree.
# Repo-relative key the contract registry is keyed by (see SelfModel).
_TARGET_REL = TARGET_PATH.relative_to(PROJECT_ROOT).as_posix()

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


def _version_for(pr: PullRequest) -> str:
    """The deployed-version string for a PR's candidate. Pure.

    One definition, because ``canary()`` and ``observe_merge()`` must agree
    exactly: the promote path looks the version up by the string the deploy
    path wrote, and a mismatch would silently promote nothing while reporting
    success.
    """
    return f"{pr.branch}@{pr.id}"


class Role:
    """Base for every role actor: registers itself and exposes the shared substrate."""

    def __init__(self, name: str, role: str, parent: str | None = None) -> None:
        self.name = name
        self.role = role
        self._sm = get_self_model()
        self._ws = get_workspace()
        ray.get(self._sm.register.remote(name, role, parent))

    def _contract(self, name: str | None = None) -> contract.OptimizationContract:
        """Which target this cycle optimises, and what judges it.

        Shared by the SWE (which proposes) and QA (which re-runs the gauntlet):
        both must resolve the *same* contract, or QA re-judges the candidate
        against a different target's oracle and rejects a perfectly good diff.
        ``run_cycle`` passes the same *name* to both for exactly that reason.

        Resolution order: the explicit *name* the caller passed, then
        ``SIS_CONTRACT``, then the bootstrap target (the historical behaviour).

        The explicit argument is the primary mechanism, not a nicety: these are
        **detached Ray actors in their own processes**, which inherit the
        driver's environment when they are *created*. An env var exported after
        ``bootstrap()`` — including anything a test sets with
        ``monkeypatch.setenv`` — is invisible to them. ``SIS_CONTRACT`` therefore
        only works when set before launch (``SIS_CONTRACT=sort python main.py``),
        which is fine for the CLI and useless for anything programmatic.

        An unknown name raises rather than silently falling back — a typo'd
        contract name that quietly optimised a different target would be a
        confusing way to waste a cycle's spend.
        """
        wanted = name or os.getenv("SIS_CONTRACT")
        if wanted:
            named: contract.OptimizationContract | None = ray.get(
                self._sm.contract_by_name.remote(wanted))
            if named is None:
                known = [c.name for c in ray.get(self._sm.contracts.remote())]
                source = "contract_name" if name else "SIS_CONTRACT"
                raise ValueError(
                    f"{source}={wanted!r} is not a registered contract; known: {known}")
            return named
        registered: contract.OptimizationContract | None = ray.get(
            self._sm.contract_for.remote(_TARGET_REL))
        return registered or contract.default_contract()


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

    def implement(self, story_id: str, contract_name: str | None = None) -> dict[str, Any]:
        # What this target is judged by — reference, inputs, margin. Resolved
        # FIRST, before any artifact is touched: a bad contract name is a
        # configuration error, and failing after moving the story to
        # In Progress would leave it stranded there with nothing working on it.
        spec = self._contract(contract_name)

        ray.get(self._ws.transition.remote(story_id, IssueStatus.IN_PROGRESS, "SWE picked up"))

        # Start from the target as merged on the base branch, so a cycle that
        # follows a merged optimisation builds on it instead of re-proposing
        # against the stale local file. Falls back to the local file when
        # version control has no merged source (the in-memory path, or a target
        # not yet committed to the base).

        merged_source = ray.get(self._ws.live_target_source.remote())
        origin = "merged_base" if merged_source else "local_file"
        # Fall back to the *contract's* target, not a hardcoded path — otherwise
        # a cycle for any contract but the bootstrap one silently optimises
        # runtime/target.py while being judged against a different oracle.
        current_source = merged_source or pathlib.Path(
            spec.target_file).read_text(encoding="utf-8")
        ray.get(self._ws.emit.remote("target.source", story_id=story_id, origin=origin))
        # sandboxed, not in-process
        baseline = gauntlet.measure_baseline(current_source, contract=spec)
        candidate = proposer.propose(current_source, baseline, contract=spec)
        candidate_sha = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12]
        cost_usd = proposer.last_cost_usd()  # 0.0 for the stub; real $ for Claude
        # Benchmark the candidate against the source the cycle is based on (the
        # merged target), not the stale local file — see KNOWN_ISSUES.md H1.
        report = gauntlet.validate(
            candidate, baseline, baseline_source=current_source, contract=spec)

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
        # The canary needs this PR's contract later (oracle, entry point,
        # margin, route) and has only the PR id to go on by then.
        ray.get(self._sm.set_pr_contract.remote(pr.id, spec.name))
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

    def review(self, story_id: str, pr_id: str, contract_name: str | None = None) -> bool:
        issue = ray.get(self._ws.get_issue.remote(story_id))
        pr = ray.get(self._ws.get_pr.remote(pr_id))
        # Deterministic gate already ran in the SWE step; QA confirms the
        # artifact exists, matches the story, and re-runs the gauntlet.
        ok = bool(pr.artifact) and issue.status == IssueStatus.READY_FOR_REVIEW
        if ok:
            # Re-run the gauntlet: the candidate executes ONLY inside its sandbox.
            # Benchmark against the same merged baseline the SWE used (the target
            # as merged on the base branch), not the stale local file — H1.
            # Must resolve the SAME contract the SWE used, or QA re-judges the
            # candidate against a different target's oracle and rejects a
            # perfectly good diff.
            spec = self._contract(contract_name)
            merged = ray.get(self._ws.live_target_source.remote())
            baseline_source = merged or pathlib.Path(
                spec.target_file).read_text(encoding="utf-8")
            report = gauntlet.validate(
                pr.artifact, 0.0, baseline_source=baseline_source, contract=spec)
            ok = report.passed
        if ok:
            ray.get(self._ws.transition.remote(story_id, IssueStatus.DONE, "QA verified"))
        else:
            ray.get(self._ws.transition.remote(story_id, IssueStatus.TBD, "QA found discrepancy"))
        ray.get(self._sm.record.remote("outcome", story_id, passed=ok, by="QA"))
        return ok


@ray.remote
class ContractAuthor(Role):
    """Turns a spec into a drafted contract. Trusted; its output is human-reviewed.

    **The one role that is allowed to write the exam**, which is exactly why it
    is a different actor from the SWE that has to pass it. Separation of author
    and implementer is not a workflow nicety — it *is* the anti-gaming property,
    and making it structural is the point of this step existing at all.

    Deliberately thin: the drafting logic is pure and lives in
    :mod:`sis.contract_author`, so it is unit-testable without standing up Ray,
    and the approval gate lives there too — in guardrail code, not in a method on
    an actor the loop could otherwise reason its way around.
    """

    def __init__(self) -> None:
        super().__init__("ContractAuthor", "ContractAuthor", parent="CTO")

    def draft(
        self,
        spec_id: str,
        *,
        name: str,
        entry: str,
        public_api: tuple[str, ...],
    ) -> dict[str, Any]:
        """Draft a contract skeleton from a spec page and stage it for review.

        Returns a summary rather than the draft itself: the artifacts are on
        disk under ``runtime/contract_staging/``, and what a caller needs back is
        *what to go and look at*.

        Never promotes. ``contract_author.promote`` requires human approval and
        this actor does not call it — the agent surfaces the decision, a human
        makes it, which is the same shape as ``DevOps.observe_merge`` applying a
        human's merge rather than performing one.
        """
        page = ray.get(self._ws.get_page.remote(spec_id))
        draft = contract_author.skeleton_from_spec(
            name=name,
            spec_ref=spec_id,
            body=page.body,
            entry=entry,
            public_api=public_api,
        )
        staged = contract_author.stage(draft, public_api=public_api)
        verdict = (staged.discrimination.summary()
                   if staged.discrimination is not None else "not checked")
        ray.get(self._sm.record.remote(
            "contract_drafted", spec_id, contract=name, files=list(staged.files),
            staged_at=str(staged.directory), awaiting="human approval",
            discriminates=verdict,
        ))
        return {
            "contract": staged.name,
            "spec_ref": staged.spec_ref,
            "staged_at": str(staged.directory),
            "files": list(staged.files),
            "promoted": False,
            # Surfaced in the return value, not only in the file: a caller that
            # never opens the directory should still see that the drafted exam
            # asserts nothing, because that is the failure a reviewer skimming
            # plausible-looking test code is least likely to notice.
            "discriminates": verdict,
            "next": "a human reviews the draft, then approves promotion into specs/",
        }


@dataclass(frozen=True)
class _RemoteTelemetry:
    """Forwards ``emit`` through ``Workspace.emit.remote(...)``.

    Satisfies :class:`sis.serve_cloud.SupportsEmit` for a role actor, which
    holds a Ray *handle* to Workspace rather than the raw ``InMemoryTelemetry``
    instance living inside it. Without this, a live ``ServeCloud``'s events
    would land in a second, invisible audit trail instead of the one
    everything else writes to.
    """

    workspace: Any

    def emit(self, event: str, **fields: object) -> None:
        ray.get(self.workspace.emit.remote(event, **fields))


@ray.remote
class DevOps(Role):
    """Infra & ops: canary deploy to the green slot; files bugs; feeds SelfModel.

    Two canary backends, chosen per call (OMNI-14):

    - **legacy** (default) — records a deploy against ``Workspace.cloud``
      (``InMemoryCloud``/``RealCloud``); no traffic, matches the engine's
      behaviour before this story.
    - **serve** (``SIS_CANARY=serve`` or ``canary_backend="serve"``) — a real
      Ray Serve deployment via :class:`~sis.serve_cloud.ServeCloud`, judged by
      :func:`~sis.canary.evaluate_canary` against live traffic this class
      synthesises itself (see :meth:`_canary_live`).
    """

    def __init__(self) -> None:
        super().__init__("DevOps", "DevOps", parent="CTO")
        # One ServeCloud per contract, built on first use (each construction
        # starts a real Serve application). Keyed by contract name because the
        # engine is multi-target and Workspace.cloud is a single, contract-
        # agnostic adapter slot that a per-contract deployment cannot share.
        self._serve_clouds: dict[str, Any] = {}
        # pr_id -> "serve" | "legacy", set by canary() and read by
        # observe_merge()/retire_canary() so a later call on the same PR routes
        # to the same backend it was deployed through. In-memory only, same
        # durability bar as SelfModel's own slot state — not persisted across a
        # cluster restart.
        self._pr_backend: dict[str, str] = {}

    def _cloud_for(self, spec: contract.OptimizationContract) -> Any:
        """The ``ServeCloud`` for *spec*, built and served on first use.

        Cached per contract name: a second construction would call
        ``serve_blue()`` again and, per OMNI-13's finding, needlessly cycle a
        replica the first construction already stood up correctly. Ray is
        already initialised — this runs inside a live Ray actor — so only
        Serve needs an explicit, idempotent start.
        """
        if spec.name not in self._serve_clouds:
            from ray import serve

            from sis.serve_cloud import ServeCloud

            serve.start(logging_config={"log_level": "ERROR"})
            cloud = ServeCloud(_RemoteTelemetry(self._ws), spec)
            cloud.serve_blue(version="live")
            self._serve_clouds[spec.name] = cloud
        return self._serve_clouds[spec.name]

    def canary(
        self, pr_id: str, candidate_latency: float, canary_backend: str | None = None
    ) -> dict[str, Any]:
        # candidate_latency was measured inside the gauntlet sandbox by the SWE
        # step. On the legacy backend the candidate is NEVER executed here
        # (main process, holds creds). On "serve" it runs in a Serve replica
        # with a scrubbed runtime_env (OMNI-13) — a different, procedural
        # guarantee, and the intended shape of a canary.
        pr = ray.get(self._ws.get_pr.remote(pr_id))
        version = _version_for(pr)

        # Explicit argument first, then the env var. Reading the env var
        # "fresh" inside an already-running actor is not fresh at all: the
        # actor's os.environ is a snapshot from when its OS process was
        # spawned, so a test's monkeypatch.setenv() (a different process) can
        # never reach it. Same trap as SIS_CONTRACT (docs/KNOWN_ISSUES.md, and
        # Role._contract above); same fix.
        backend = canary_backend or os.getenv("SIS_CANARY")
        self._pr_backend[pr_id] = "serve" if backend == "serve" else "legacy"

        if backend == "serve":
            return self._canary_live(pr, version, candidate_latency)

        record = ray.get(self._ws.deploy_canary.remote(
            version, {"latency_seconds": candidate_latency}))
        ray.get(self._sm.set_slot.remote("green", version))
        # Remember which PR would release this canary, so the merge watcher has
        # an exact id rather than one parsed back out of the version string.
        ray.get(self._sm.set_pending_pr.remote(pr_id))
        ray.get(self._sm.record.remote(
            "canary", version, pr=pr_id, latency=candidate_latency))
        return {"version": version, "slot": record.slot,
                "latency_seconds": candidate_latency, "live": record.live,
                "canary_passed": True}

    def _canary_live(
        self, pr: PullRequest, version: str, candidate_latency: float
    ) -> dict[str, Any]:
        """The real flow: deploy behind Ray Serve, fill the window, decide.

        A live signal the sandboxed benchmark structurally cannot see — real
        concurrency, real queueing (OMNI-12's field measurement: a ~5x offline
        speedup was only ~30% faster under 8-way load). That gap is the reason
        this exists, not a formality to satisfy before ``verified_awaiting_
        human_merge`` unchanged.
        """
        spec: contract.OptimizationContract | None = ray.get(
            self._sm.contract_for_pr.remote(pr.id))
        if spec is None:
            raise RuntimeError(
                f"no contract recorded for PR {pr.id!r} — SWE.implement() must "
                "resolve and record one before a live canary can judge the candidate"
            )
        cloud = self._cloud_for(spec)

        # Forced, not configured: an OptimizationContract carries no
        # invariants (Class 2 / OMNI-18, unbuilt), so SPLIT mode would have
        # ZERO live correctness signal — only a speed comparison — and could
        # silently promote a fast, wrong candidate. Response agreement under
        # SHADOW is the only live correctness check available today.
        cloud.set_mode(CanaryMode.SHADOW)

        record = cloud.deploy_canary(
            version, metrics={"latency_seconds": candidate_latency}, source=pr.artifact)
        ray.get(self._sm.set_slot.remote("green", version))
        ray.get(self._sm.set_pending_pr.remote(pr.id))
        ray.get(self._sm.record.remote(
            "canary", version, pr=pr.id, latency=candidate_latency, backend="serve"))

        # Bootstrap traffic (see LIVE_CANARY_REQUESTS): nothing external calls
        # the target yet, so the window is filled synthetically rather than
        # waiting on organic traffic that will never arrive.
        cloud.warm_up(LIVE_CANARY_REQUESTS, concurrency=LIVE_CANARY_CONCURRENCY)

        blue_latencies, _ = cloud.live_window(str(cloud.live_version()))
        green_latencies, _ = cloud.live_window(version)
        verdict = evaluate_canary(
            [], cloud.live_samples(), blue_latencies, green_latencies,
            version=version, mode=CanaryMode.SHADOW,
            min_samples=min(DEFAULT_MIN_CANARY_SAMPLES, LIVE_CANARY_REQUESTS))

        if not verdict.passed:
            self.retire_canary(version, pr.id)
            bug_id = self.file_bug(
                f"Live canary rejected PR {pr.id} ({spec.name}): {verdict.reason}")
            ray.get(self._sm.record.remote(
                "canary_rejected", version, pr=pr.id, reason=verdict.reason))
            return {"version": version, "slot": "green", "live": False,
                    "latency_seconds": candidate_latency, "canary_passed": False,
                    "reason": verdict.reason, "bug_id": bug_id, "verdict": asdict(verdict)}

        return {"version": version, "slot": record.slot, "live": record.live,
                "latency_seconds": candidate_latency, "canary_passed": True,
                "verdict": asdict(verdict)}

    def observe_merge(self, pr_id: str) -> dict[str, Any]:
        """Notice that a human merged ``pr_id``; promote and release green.

        **This never merges and never decides to promote.** It reads the PR
        back from the version-control port and does nothing at all unless
        ``merged`` is already true. Since ``merge_pr()`` raises
        ``RequiresHumanApproval`` in every adapter, the agent cannot make that
        true — so the only thing this does is *apply* a decision a human
        already made, which is what closes the loop the design always described
        (docs/SERVE_CANARY.md, "nothing calls promote() today").

        Idempotent: promoting an already-live version is a no-op, so a poll that
        fires twice on the same merge does not double-promote or double-record.
        Routes to the same backend the PR was canaried through, so a live
        promotion actually redeploys blue rather than silently updating a
        bookkeeping record nobody is looking at (see ``canary()``).
        """
        pr = ray.get(self._ws.get_pr.remote(pr_id))
        version = _version_for(pr)
        if not pr.merged:
            # The overwhelmingly common case on any given tick. Deliberately
            # silent — emitting here would bury the audit trail under one event
            # per poll while a human takes hours to review.
            return {"pr": pr_id, "version": version, "merged": False, "promoted": False}

        if self._pr_backend.get(pr_id) == "serve":
            spec = self._contract_for_live_pr(pr_id)
            cloud = self._cloud_for(spec)
            if cloud.live_version() == version:
                return {"pr": pr_id, "version": version, "merged": True,
                        "promoted": False, "reason": "already live"}
            record = cloud.promote(version)
        else:
            if ray.get(self._ws.live_version.remote()) == version:
                return {"pr": pr_id, "version": version, "merged": True,
                        "promoted": False, "reason": "already live"}
            record = ray.get(self._ws.promote.remote(version))

        ray.get(self._sm.set_live_version.remote(version))
        # Release the gate: green is free, so loop.serve may start a new cycle —
        # and it will now baseline from the merged target rather than
        # re-proposing the change that was sitting in this PR.
        ray.get(self._sm.set_slot.remote("green", None))
        ray.get(self._sm.set_pending_pr.remote(None))
        ray.get(self._sm.record.remote("promote", version, pr=pr_id))
        ray.get(self._ws.emit.remote("merge.observed", pr_id=pr_id, version=version))
        return {"pr": pr_id, "version": version, "merged": True, "promoted": True,
                "slot": record.slot, "live": record.live}

    def retire_canary(self, version: str, pr_id: str | None = None) -> dict[str, Any]:
        """Take the canary out of the green slot and stop its traffic.

        The release half of ``canary()``. Without it the one-canary-in-flight
        gate (``loop.serve``) has no exit: green is set when a canary deploys
        and nothing else ever clears it, so the loop would idle forever after
        its first successful cycle. Called on rollback (including from
        ``_canary_live`` on a failed live verdict), and by ``observe_merge`` on
        promotion.

        ``pr_id`` is optional: given, it routes the rollback to the same
        backend the canary was deployed through. Omitted — the manual
        "an operator releases the gate by hand" path — it always goes through
        the legacy adapter, the historical behaviour.
        """
        if pr_id is not None and self._pr_backend.get(pr_id) == "serve":
            self._cloud_for(self._contract_for_live_pr(pr_id)).rollback(version)
        else:
            ray.get(self._ws.rollback.remote(version))
        ray.get(self._sm.set_slot.remote("green", None))
        ray.get(self._sm.set_pending_pr.remote(None))
        ray.get(self._sm.record.remote("canary_retired", version))
        return {"version": version, "slot": "green", "released": True}

    def _contract_for_live_pr(self, pr_id: str) -> contract.OptimizationContract:
        spec: contract.OptimizationContract | None = ray.get(
            self._sm.contract_for_pr.remote(pr_id))
        if spec is None:  # pragma: no cover - canary() would not set backend="serve" otherwise
            raise RuntimeError(
                f"PR {pr_id!r} was canaried on the live backend but has no "
                "recorded contract — this should be unreachable"
            )
        return spec

    def file_bug(self, summary: str) -> str:
        issue = ray.get(self._ws.create_issue.remote(IssueType.BUG, summary, None))
        ray.get(self._sm.record.remote("bug", issue.id, summary=summary))
        return str(issue.id)
