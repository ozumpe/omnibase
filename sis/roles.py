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
import pathlib
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

import ray

from sis import config, contract, contract_author, gauntlet, policy, proposer
from sis import feature as feature_mod
from sis.canary import DEFAULT_MIN_CANARY_SAMPLES, CanaryMode, evaluate_canary
from sis.paths import PROJECT_ROOT, TARGET_PATH
from sis.ports import IssueStatus, IssueType, PullRequest, PullRequestNotFound
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

# Repo-relative key the contract registry is keyed by (see SelfModel).
_TARGET_REL = TARGET_PATH.relative_to(PROJECT_ROOT).as_posix()

# CEO spend-brake defaults, read off the config schema rather than restated here.
# They were previously literals in this module *and* the documented defaults in
# the README — the drift that OMNI-27 exists to end. One declaration in
# sis/config.py now feeds CEO.__init__, ceo_config_from_env, config.yml, and the
# --brakes-* flags, so a configured run and a default run cannot disagree.
DEFAULT_BUDGET_USD: float = config.key_for("brakes.budget_usd").default
DEFAULT_BREAKER_THRESHOLD: int = config.key_for("brakes.breaker_threshold").default
DEFAULT_MAX_COST_PER_ACCEPTED_USD: float = config.key_for(
    "brakes.max_cost_per_accepted_usd").default
DEFAULT_SLO_MIN_SPEND_USD: float = config.key_for("brakes.slo_min_spend_usd").default
DEFAULT_SLO_FAILURE_WEIGHT: float = config.key_for("brakes.slo_failure_weight").default

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
    slo_failure_weight: float = DEFAULT_SLO_FAILURE_WEIGHT

    def __post_init__(self) -> None:
        check_slo_failure_weight(self.slo_failure_weight)


def check_slo_failure_weight(weight: float) -> float:
    """Reject a weight outside (0, 1]. Pure.

    The config schema already refuses zero and negatives; the upper bound lives
    here because it is a property of the breaker, not of number parsing. Above
    1.0 a correct-but-slow cycle would count for *more* than a wrong one, which
    inverts the whole point of weighing them differently. Zero is refused for
    the opposite reason: it would silently switch SLO failures off.
    """
    if not 0.0 < weight <= 1.0:
        raise ValueError(
            f"brakes.slo_failure_weight must be in (0, 1], got {weight} — 1.0 counts an "
            "over-budget cycle like a wrong one; smaller values count it for less"
        )
    return weight


def failure_weight(reject_gate: str | None, *, slo_failure_weight: float) -> float:
    """How much one failed cycle adds to the consecutive-failure streak. Pure.

    Only a correct-but-over-budget rejection (``slo``) is discounted. Everything
    else — including ``slo_error``, a candidate that *raised* on the SLO
    workload, which is a wrong answer rather than a slow one — counts in full.

    ``harness`` (a broken sandbox, OMNI-37) also counts in full, deliberately.
    A broken sandbox fails *every* cycle, and each one spends on a proposal
    first, so the breaker stopping the loop after N of them is the right
    outcome. What OMNI-37 fixed is the attribution — the bug says the
    infrastructure failed, not the candidate — not whether it counts.
    """
    return slo_failure_weight if reject_gate == "slo" else 1.0


def ceo_config_from_env(env: Mapping[str, str] | None = None) -> CEOConfig:
    """Build the CEO's spend brakes from configuration (pure — unit-testable).

    Lets a run set a deliberately tiny budget without editing source
    (KNOWN_ISSUES.md M5), now through the whole precedence chain rather than the
    environment alone: ``--brakes-budget-usd`` > ``SIS_BUDGET_USD`` >
    ``config.yml``'s ``brakes.forbidden_budget_usd`` > the built-in default. An
    unparseable or negative value raises rather than falling back — a typo'd
    ``0.1O`` that quietly became $5 would defeat the whole point of the gate.

    Keeps its *env mapping* parameter because that is what makes it unit-testable
    without touching the process environment; the mapping is threaded into
    :func:`sis.config.resolve` as that layer.
    """
    cfg = config.config(env=env).brakes
    return CEOConfig(
        budget_usd=cfg.budget_usd,
        breaker_threshold=cfg.breaker_threshold,
        max_cost_per_accepted_usd=cfg.max_cost_per_accepted_usd,
        slo_min_spend_usd=cfg.slo_min_spend_usd,
        slo_failure_weight=cfg.slo_failure_weight,
    )


# Tolerance for comparing the weighted failure streak with its threshold.
_STREAK_EPSILON = 1e-9


def evaluate_brakes(
    *,
    spent: float,
    budget: float,
    consecutive_failures: float,
    threshold: int,
    accepted: int,
    max_cost_per_accepted: float,
    slo_min_spend: float,
) -> str | None:
    """Return the name of the tripped brake, or None. Pure — easy to unit-test.

    Order is intentional: the hard spend cap dominates, then the regression
    breaker, then the economics SLO (only judged once real money is spent).

    *consecutive_failures* is a weighted streak (OMNI-24): a correct-but-over-
    budget cycle adds ``brakes.slo_failure_weight`` rather than 1, so it may be
    fractional. It is compared with a small tolerance: a weight like 0.1 summed
    ten times is 0.9999999999999999 in binary floating point, and a breaker
    that never reaches its threshold because of rounding is a breaker that
    never trips.
    """
    if spent > budget:
        return "hard spend cap exceeded"
    if consecutive_failures + _STREAK_EPSILON >= threshold:
        return "consecutive failure threshold"
    cost_per_accepted = spent / accepted if accepted else float("inf")
    if spent >= slo_min_spend and cost_per_accepted > max_cost_per_accepted:
        return "cost-per-accepted-improvement SLO breached"
    return None


def brake_persistence_problem(
    store: str, proposer_backend: str, adapters_mode: str
) -> str | None:
    """Why this configuration may not run with ephemeral brakes, or None. Pure.

    OMNI-61, decided 2026-09-26: ``episodic.store = none`` is refused only when
    the brakes protect something. With ``none``, every ``python main.py``
    rehydrates nothing and starts at ``spent=0`` — repeated runs have no spend
    cap at all — and the episodic log reconciled against the provider's bill is
    discarded too. That matters when a real proposer spends money or real
    adapters touch real systems; with the stub and in-memory adapters (the
    default run and the whole test suite) nothing is spent and nothing leaves
    the process, so per-process brakes cost nothing.

    No override flag, deliberately unlike M1: ``jsonl`` writes to the gitignored
    ``runtime/``, so the safe alternative costs nothing, and an override on a
    spend guardrail would be a permanent off-switch.
    """
    if store != "none" or (proposer_backend == "stub" and adapters_mode != "real"):
        return None
    return (
        f"episodic.store=none with proposer.backend={proposer_backend!r} and "
        f"adapters.mode={adapters_mode!r}: brake state would be per-process, so "
        "every restart starts at spent=0 (no durable spend cap), and the episodic "
        "log that reconciles spend against the bill is discarded (no audit trail). "
        "Use episodic.store=jsonl (the default; it writes to the gitignored runtime/)."
    )


def ensure_brake_persistence() -> None:
    """Raise if the configured store cannot back the brakes (see above)."""
    problem = brake_persistence_problem(
        str(config.get("episodic.store")), str(config.get("proposer.backend")),
        str(config.get("adapters.mode")))
    if problem:
        raise RuntimeError(problem)


def unreadable_brake_state(detail: str) -> dict[str, Any]:
    """The state a CEO boots with when its persisted state cannot be read. Pure.

    Fails closed (OMNI-61): the breaker is open and says why. Spend is unknown,
    not zero — so no cycle runs until a human has looked, repaired or moved the
    file aside, and reset the breaker on purpose (``python -m sis.admin
    reset-breaker``). The store refuses to overwrite the unreadable file, so the
    evidence survives until then.
    """
    return {
        "tripped": True,
        "trip_reason": (
            f"brake state unreadable ({detail}); spend and failure streak are unknown. "
            "Inspect it, repair it or move it aside, then run "
            "`python -m sis.admin reset-breaker`."
        ),
    }


def _version_for(pr: PullRequest) -> str:
    """The deployed-version string for a PR's candidate. Pure.

    One definition, because ``canary()`` and ``observe_merge()`` must agree
    exactly: the promote path looks the version up by the string the deploy
    path wrote, and a mismatch would silently promote nothing while reporting
    success.
    """
    return f"{pr.branch}@{pr.id}"


def _sha(source: str) -> str:
    """The short hash a candidate is recorded under."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def pr_resolution(pr: PullRequest) -> Literal["hold", "promote", "release"]:
    """What a pending PR's state means for the canary it holds. Pure.

    ``promote`` — a human merged it. ``release`` — a human closed it without
    merging (OMNI-57): the canary goes, and the loop carries on from the
    target as it was. ``hold`` — still under review, however long that takes.
    Only a human's decision ends a hold; the agent can make neither.
    """
    if pr.merged:
        return "promote"
    return "release" if pr.closed else "hold"


class Role:
    """Base for every role actor: registers itself and exposes the shared substrate."""

    def __init__(self, name: str, role: str, parent: str | None = None) -> None:
        self.name = name
        self.role = role
        self._sm = get_self_model()
        self._ws = get_workspace()
        ray.get(self._sm.register.remote(name, role, parent))

    def _contract(self, name: str | None = None) -> contract.Contract:
        """Which target this cycle optimises, and what judges it.

        Shared by the SWE (which proposes) and QA (which re-runs the gauntlet):
        both must resolve the *same* contract, or QA re-judges the candidate
        against a different target's oracle and rejects a perfectly good diff.
        ``run_cycle`` passes the same *name* to both for exactly that reason.

        Resolution order: the explicit *name* the caller passed, then
        ``contracts.default``, then the bootstrap target (historical behaviour).

        The explicit argument is the primary mechanism, not a nicety: these are
        **detached Ray actors in their own processes**, which inherit the
        driver's environment when they are *created*. An env var exported after
        ``bootstrap()`` — including anything a test sets with
        ``monkeypatch.setenv`` — is invisible to them. ``SIS_CONTRACT`` therefore
        only works when set before launch (``SIS_CONTRACT=sort python main.py``),
        which is fine for the CLI and useless for anything programmatic. The
        ``config.yml`` layer does not have that limitation — each actor reads the
        file from disk itself — but the explicit argument still wins, because a
        per-cycle choice should not depend on a file that outlives the cycle.

        An unknown name raises rather than silently falling back — a typo'd
        contract name that quietly optimised a different target would be a
        confusing way to waste a cycle's spend.
        """
        wanted = name or config.get("contracts.default")
        if wanted:
            named: contract.Contract | None = ray.get(
                self._sm.contract_by_name.remote(wanted))
            if named is None:
                known = [c.name for c in ray.get(self._sm.contracts.remote())]
                source = "contract_name" if name else "contracts.default"
                raise ValueError(
                    f"{source}={wanted!r} is not a registered contract; known: {known}")
            return named
        registered: contract.Contract | None = ray.get(
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
        slo_failure_weight: float = DEFAULT_SLO_FAILURE_WEIGHT,
        state: dict[str, Any] | None = None,
    ) -> None:
        super().__init__("CEO", "CEO")
        self._budget = budget_usd
        self._spent = 0.0
        self._threshold = breaker_threshold
        self._max_cost_per_accepted = max_cost_per_accepted_usd
        self._slo_min_spend = slo_min_spend_usd  # don't judge the SLO on pennies
        self._slo_failure_weight = check_slo_failure_weight(slo_failure_weight)
        # Weighted (OMNI-24): an over-budget cycle adds less than a wrong one.
        self._consecutive_failures = 0.0
        self._accepted = 0
        self._tripped = False
        # Why the breaker is open, for the operator (OMNI-61) — a brake name, or
        # "brake state unreadable" when the CEO failed closed at boot.
        self._trip_reason: str | None = None
        # An operator's pause (sis.admin): refuses new cycles without tripping
        # the breaker or touching any counter. The reason, or None.
        self._paused: str | None = None
        self._charter_id: str | None = None
        # Rehydrate persisted brake/spend state (L9) — only on a *fresh* actor.
        # A detached CEO that already exists (get_if_exists) keeps its live state;
        # this path runs on first bootstrap or after a cluster/actor restart.
        if state:
            self._spent = float(state.get("spent_usd", 0.0))
            # float(): pre-OMNI-24 snapshots stored an int, which still loads.
            self._consecutive_failures = float(state.get("consecutive_failures", 0))
            self._accepted = int(state.get("accepted", 0))
            self._tripped = bool(state.get("tripped", False))
            self._trip_reason = state.get("trip_reason")
            self._paused = state.get("paused")

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

    def report_outcome(
        self, *, success: bool, cost_usd: float = 0.0, reject_gate: str | None = None,
    ) -> str | None:
        """Record real spend + outcome, then evaluate all three brakes.

        *reject_gate* is the gauntlet gate that rejected a failed cycle
        (``episodic.gate_from_reason``). It only changes how much the failure
        counts: a correct-but-over-budget ``slo`` rejection adds
        ``brakes.slo_failure_weight`` to the streak, anything else adds 1.

        Returns the brake reason **on a fresh trip** (None otherwise) so the
        caller can raise the alarm — per ACTORS.md, DevOps files the bug that
        "pages a human".
        """
        self._spent += cost_usd
        if success:
            self._consecutive_failures = 0.0
            self._accepted += 1
        else:
            self._consecutive_failures += failure_weight(
                reject_gate, slo_failure_weight=self._slo_failure_weight)
        return self._evaluate_brakes()

    def record_neutral(self, *, cost_usd: float = 0.0) -> str | None:
        """Record a neutral cycle — nothing to improve (a no-op), or a benchmark
        that saw the candidate as faster but could not prove it (OMNI-41).

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
            self._trip_reason = reason
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
            "trip_reason": self._trip_reason,
            "paused": self._paused,
        }

    def pause_reason(self) -> str | None:
        """Why an operator paused the loop, or None when it is not paused."""
        return self._paused

    def pause(self, reason: str) -> dict[str, Any]:
        """Refuse new cycles until :meth:`resume` (``sis.admin pause``).

        Not a trip: the failure streak, spend and breaker are untouched, so
        resuming puts the loop back exactly where it was. Human-only by
        convention, **not** by construction — any code that can reach this
        named actor can call it, which is why OMNI-48/49 keep candidate code
        away from the cluster rather than trusting this method to.
        """
        self._paused = reason
        ray.get(self._ws.emit.remote("loop.paused", reason=reason))
        return self.state_snapshot()

    def resume(self) -> dict[str, Any]:
        """Undo :meth:`pause`. The breaker, if open, stays open."""
        self._paused = None
        ray.get(self._ws.emit.remote("loop.resumed"))
        return self.state_snapshot()

    def reset_breaker(self) -> bool:
        """Admin reset of the circuit breaker (clears the tripped flag + failure
        streak). Spend is deliberately **not** reset — it is a financial guardrail,
        so clearing the breaker can't bypass the hard cap (see
        docs/BRAKE_STATE_AND_ORACLE.md §4.1). A spend-cap trip therefore re-trips on
        the next evaluation until the budget is raised."""
        self._tripped = False
        self._trip_reason = None
        self._consecutive_failures = 0.0
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

    def plan(self, spec_page_id: str, contract_name: str | None = None) -> dict[str, str]:
        """Plan a feature: an epic and its stories, kept until its PR opens.

        Every step of the feature works under this plan (OMNI-135). Before, each
        cycle planned afresh, and the fourth AWS run filed 22 TES issues for
        six cycles.
        """
        contract_ = self._contract(contract_name)
        spec = ray.get(self._ws.get_page.remote(spec_page_id))
        epic = ray.get(self._ws.create_issue.remote(IssueType.EPIC, f"Epic: {spec.title}", None))
        infra = ray.get(self._ws.create_issue.remote(
            IssueType.STORY, "Infra: ensure CI + sandbox runner", epic.id))
        feature = ray.get(self._ws.create_issue.remote(
            IssueType.STORY, f"Implement: {spec.title}", epic.id))
        ray.get(self._ws.transition.remote(feature.id, IssueStatus.TODO, "Planned by CTO"))
        ray.get(self._sm.record.remote("epic", epic.id, spec=spec_page_id))
        ray.get(self._sm.record.remote("story", feature.id, epic=epic.id))
        plan = {"spec_id": str(spec_page_id), "epic_id": str(epic.id),
                "infra_story_id": str(infra.id), "feature_story_id": str(feature.id)}
        ray.get(self._sm.set_plan.remote(contract_.name, plan))
        return plan

    def open_plan(self, contract_name: str | None = None) -> dict[str, Any] | None:
        """The plan the next step of *contract_name* works under, if one is open.

        One plan per feature (OMNI-135): kept from its first step until its PR
        opens. With none in memory, a feature a previous process left half
        built is found on its branch and carried on
        (:func:`sis.feature.resumable_feature`); the result then says so under
        ``resumed``. The version-control system is the source of truth, as for
        open PRs (OMNI-136): a file on the box would not survive the box being
        replaced, which every new release does (OMNI-137).

        A failure to look starts a fresh feature. That leaves the old branch
        behind, which is what happened before this check existed, and never
        carries on a branch nobody can see.
        """
        spec = self._contract(contract_name)
        plan: dict[str, Any] | None = ray.get(self._sm.plan.remote(spec.name))
        if plan is not None or ray.get(self._sm.feature.remote(spec.name)) is not None:
            return plan
        try:
            branches = ray.get(self._ws.unproposed_branches.remote(feature_mod.BRANCH_PREFIX))
        except Exception as exc:  # noqa: BLE001 - see the docstring: start fresh, say why
            detail = " ".join(str(exc).split())[:200]
            print(f"[sis] WARNING: could not look for an unfinished {spec.name} feature "
                  f"({detail}); starting a new one", file=sys.stderr)
            ray.get(self._ws.emit.remote("feature.resume_failed", contract=spec.name,
                                         error=detail))
            return None
        found = feature_mod.resumable_feature(spec.name, branches)
        if found is None:
            return None
        feature, plan = found["feature"], found["plan"]
        ray.get(self._sm.set_feature.remote(spec.name, feature))
        ray.get(self._sm.set_plan.remote(spec.name, plan))
        ray.get(self._sm.record.remote(
            "feature_resumed", feature["branch"], story=plan["feature_story_id"],
            steps=len(feature["steps"])))
        ray.get(self._ws.emit.remote(
            "feature.resumed", contract=spec.name, branch=feature["branch"],
            steps=len(feature["steps"])))
        return {**plan, "resumed": feature["branch"], "steps": len(feature["steps"])}


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

        #
        # Within a feature (OMNI-130), the next step starts from the feature
        # branch's head instead: the steps build on each other there, and the
        # base branch sees them only when a human merges the finished feature.
        max_steps = int(config.get("loop.feature_max_steps"))
        # A Class-2 contract is *built* (OMNI-147): no timings, and the feature is
        # done at its first step that passes every gate.
        building = isinstance(spec, contract.FeatureContract)
        feature: dict[str, Any] | None = ray.get(self._sm.feature.remote(spec.name))
        # The plan this step works under (CTO.plan). Its ids ride on the step's
        # commit, so a restart can rebuild the feature from the branch (OMNI-135).
        plan: dict[str, Any] = ray.get(self._sm.plan.remote(spec.name)) or {
            "feature_story_id": story_id, "spec_id": "unplanned", "epic_id": "unplanned"}
        # A feature being built can have attempts but no branch yet: its branch
        # is made by its first passing step.
        head = (ray.get(self._ws.read_file.remote(feature["branch"], spec.target_path))
                if feature and feature["steps"] else "")
        if feature and feature["steps"] and not head:
            feature = None  # the branch lost its file: start a fresh feature
        if building and feature and head:
            # A built feature ends at its first step, so a step without a PR is
            # one a stopped process committed (OMNI-135): open its PR now.
            return self._finish_feature(
                story_id, spec, feature, head, 0.0, _sha(head), "all gates passed")
        if head:
            current_source, origin = head, "feature_branch"
        else:
            merged_source = ray.get(self._ws.live_target_source.remote(spec.target_path))
            # Fall back to the *contract's* target, not a hardcoded path — otherwise
            # a cycle for any contract but the bootstrap one silently optimises
            # runtime/target.py while being judged against a different oracle. A
            # feature not built yet has neither (OMNI-147).
            local = pathlib.Path(spec.target_file)
            current_source = merged_source or (
                local.read_text(encoding="utf-8") if local.exists() else "")
            origin = ("merged_base" if merged_source
                      else "local_file" if current_source else "none")
        ray.get(self._ws.emit.remote("target.source", story_id=story_id, origin=origin))
        if building and feature is None and current_source:
            # A feature already built: judged again, since its spec may have
            # changed. Passing, there is nothing to build, which is the target
            # converging (OMNI-138); failing, its reason starts the rebuild.
            standing = gauntlet.validate(current_source, contract=spec)
            if standing.passed:
                return {"passed": False, "no_gain": True, "pr_id": None,
                        "cost_usd": 0.0, "candidate_sha": _sha(current_source),
                        "contract": spec.name,
                        "reason": f"already built: {spec.target_path} passes every gate"}
            feature = feature_mod.with_note(
                feature_mod.new_feature(feature_mod.feature_branch(story_id), story_id),
                f"the current module fails: {standing.reason}")
        # sandboxed, not in-process
        baseline = (gauntlet.measure_baseline(current_source, contract=spec)
                    if isinstance(spec, contract.OptimizationContract) else 0.0)
        try:
            candidate = proposer.propose(current_source, baseline, contract=spec,
                                         history=feature["attempts"] if feature else ())
        except proposer.ProposalCutOff as exc:
            # Nothing to judge, and not the candidate's doing: a failed cycle
            # the log names as the proposer's, its spend kept (L33, OMNI-78).
            reason = f"proposer: {exc}"
            ray.get(self._ws.transition.remote(story_id, IssueStatus.TBD, reason))
            ray.get(self._sm.record.remote("outcome", story_id, passed=False, reason=reason))
            return {"passed": False, "reason": reason, "pr_id": None,
                    "cost_usd": proposer.last_cost_usd(), "candidate_sha": None,
                    "contract": spec.name}
        candidate_sha = _sha(candidate)
        cost_usd = proposer.last_cost_usd()  # 0.0 for the stub; real $ for Claude
        # Benchmark the candidate against the source the cycle is based on (the
        # merged target), not the stale local file — see KNOWN_ISSUES.md H1.
        report = gauntlet.validate(
            candidate, baseline, baseline_source=current_source or None, contract=spec)

        if not report.passed:
            ray.get(self._ws.transition.remote(
                story_id, IssueStatus.TBD, f"Gauntlet failed: {report.reason}"))
            ray.get(self._sm.record.remote("outcome", story_id, passed=False, reason=report.reason))
            if building and feature is None:
                # The reasons a feature's attempts failed are what its next
                # prompt needs most, and it has no branch until one passes.
                feature = feature_mod.new_feature(
                    feature_mod.feature_branch(story_id), story_id)
                started = None
            else:
                started = feature
            if feature is not None:
                feature = feature_mod.with_note(feature, f"rejected: {report.reason}")
                if feature["steps"] and feature_mod.ends_feature(report.reason):
                    # The head cannot be beaten: the feature is finished, and
                    # its PR carries the head, not this rejected candidate.
                    return self._finish_feature(
                        story_id, spec, feature, current_source, cost_usd, candidate_sha,
                        f"no further gain ({report.reason})")
                ray.get(self._sm.set_feature.remote(spec.name, feature))
            return {"passed": False, "reason": report.reason, "pr_id": None,
                    "cost_usd": cost_usd, "candidate_sha": candidate_sha,
                    "contract": spec.name,
                    # Nothing beats the base: the target has converged (OMNI-138).
                    "no_gain": feature_mod.finds_no_gain(started, report.reason)}

        # Change-authorization policy: the loop may only write paths its tier
        # permits. The target is SOFT (allowed once checks pass); a mis-pointed
        # guardrail/engine path is refused here, before any branch or PR. The
        # path authorised is the one open_pr() writes — the contract's — so
        # the check cannot approve one file while the PR changes another
        # (OMNI-51: both used to name runtime/target.py for every contract).
        decision = policy.authorize_change(spec.target_path, checks_passed=report.passed)
        ray.get(self._ws.emit.remote(
            "policy.decision", story_id=story_id, path=spec.target_path,
            tier=decision.tier.value, allowed=decision.allowed))
        if not decision.allowed:
            ray.get(self._ws.transition.remote(
                story_id, IssueStatus.TBD, f"Policy blocked: {decision.reason}"))
            ray.get(self._sm.record.remote(
                "outcome", story_id, passed=False, reason=f"policy: {decision.reason}"))
            return {"passed": False, "reason": f"policy: {decision.reason}",
                    "pr_id": None, "cost_usd": cost_usd, "candidate_sha": candidate_sha,
                    "contract": spec.name}

        # A passing step is committed to the feature branch, forked (for the
        # first step) from the same base the merged target was read from, not
        # a hardcoded "main" — see KNOWN_ISSUES.md M4. No PR yet (OMNI-130).
        if feature is None or not feature["steps"]:
            feature = feature or feature_mod.new_feature(
                feature_mod.feature_branch(story_id), story_id)
            ray.get(self._ws.create_branch.remote(feature["branch"], version_control_base()))
            ray.get(self._sm.record.remote("branch", feature["branch"], story=story_id))
        step = len(feature["steps"]) + 1
        ray.get(self._ws.write_file.remote(
            feature["branch"], spec.target_path, candidate,
            feature_mod.step_message(spec.name, plan, step, baseline, report.latency_seconds,
                                     building=building)))
        feature = feature_mod.with_step(feature, story_id, baseline, report.latency_seconds)
        ray.get(self._sm.record.remote("commit", feature["branch"], story=story_id, step=step))
        if building:
            return self._finish_feature(story_id, spec, feature, candidate, cost_usd,
                                        candidate_sha, "all gates passed")
        if feature_mod.is_full(feature, max_steps):
            return self._finish_feature(story_id, spec, feature, candidate, cost_usd,
                                        candidate_sha, f"{step} of {max_steps} steps")
        ray.get(self._sm.set_feature.remote(spec.name, feature))
        # The story is the feature's, not the step's (OMNI-135): it stays in
        # progress until the feature's PR opens.
        ray.get(self._ws.transition.remote(
            story_id, IssueStatus.IN_PROGRESS, f"Step {step} committed to {feature['branch']}"))
        return {"passed": True, "feature_step": True, "step": step, "pr_id": None,
                "branch": feature["branch"], "baseline": baseline,
                "candidate_latency": report.latency_seconds, "cost_usd": cost_usd,
                "candidate_sha": candidate_sha, "contract": spec.name}

    def _finish_feature(
        self, story_id: str, spec: contract.Contract, feature: dict[str, Any],
        head: str, cost_usd: float, candidate_sha: str, finished_because: str,
    ) -> dict[str, Any]:
        """Open the feature's one PR, for its head, and hand it to review.

        The PR's head never moves after this (the next feature gets a new
        branch), so a human merges exactly the steps the PR shows.
        """
        building = isinstance(spec, contract.FeatureContract)
        pr = ray.get(self._ws.open_pr.remote(
            feature["branch"], feature_mod.pr_title(spec.name, feature, building=building),
            head, spec.target_path,
            feature_mod.pr_body(spec.name, feature, finished_because, building=building)))
        ray.get(self._ws.transition.remote(
            story_id, IssueStatus.READY_FOR_REVIEW, f"Feature PR {pr.id} ready"))
        # The canary needs this PR's contract later (oracle, entry point,
        # margin, route) and has only the PR id to go on by then.
        ray.get(self._sm.set_pr_contract.remote(pr.id, spec.name))
        # The next feature starts from a new plan, and so a new story.
        ray.get(self._sm.set_feature.remote(spec.name, None))
        ray.get(self._sm.set_plan.remote(spec.name, None))
        first, last = feature["steps"][0], feature["steps"][-1]
        ray.get(self._sm.record.remote(
            "pr", pr.id, story=story_id, steps=len(feature["steps"]),
            baseline=first["baseline_s"], candidate=last["candidate_s"]))
        return {"passed": True, "pr_id": str(pr.id), "branch": feature["branch"],
                "steps": len(feature["steps"]), "finished_because": finished_because,
                "baseline": first["baseline_s"], "candidate_latency": last["candidate_s"],
                "cost_usd": cost_usd, "candidate_sha": candidate_sha,
                "contract": spec.name}


@ray.remote
class QA(Role):
    """Verification: acts when a story is Ready for Review; augments the gauntlet."""

    def __init__(self) -> None:
        super().__init__("QA", "QA", parent="CTO")

    def review(
        self, story_id: str, pr_id: str, contract_name: str | None = None
    ) -> tuple[bool, str | None]:
        """Verify the PR against its story; returns ``(approved, gauntlet_reason)``.

        The reason is the re-run gauntlet's (``None`` if it never ran), so the
        org can tell a real rejection from a neutral one — an *inconclusive*
        benchmark (OMNI-41) is the same fact at QA as at the SWE stage and must
        not become a bug and a breaker count just because QA re-measured.
        """
        issue = ray.get(self._ws.get_issue.remote(story_id))
        # Resolved before the PR is read: which file holds the candidate is a
        # property of the contract (OMNI-51).
        spec = self._contract(contract_name)
        pr = ray.get(self._ws.get_pr.remote(pr_id, spec.target_path))
        # Deterministic gate already ran in the SWE step; QA confirms the
        # artifact exists, matches the story, and re-runs the gauntlet.
        ok = bool(pr.artifact) and issue.status == IssueStatus.READY_FOR_REVIEW
        reason: str | None = None
        if ok:
            # Re-run the gauntlet: the candidate executes ONLY inside its sandbox.
            # Benchmark against the same merged baseline the SWE used (the target
            # as merged on the base branch), not the stale local file — H1.
            # Must resolve the SAME contract the SWE used, or QA re-judges the
            # candidate against a different target's oracle and rejects a
            # perfectly good diff.
            merged = ray.get(self._ws.live_target_source.remote(spec.target_path))
            local = pathlib.Path(spec.target_file)
            # A feature not built yet has no baseline, and needs none (OMNI-147).
            baseline_source = merged or (
                local.read_text(encoding="utf-8") if local.exists() else None)
            report = gauntlet.validate(
                pr.artifact, 0.0, baseline_source=baseline_source, contract=spec)
            ok = report.passed
            reason = report.reason
        if ok:
            ray.get(self._ws.transition.remote(story_id, IssueStatus.DONE, "QA verified"))
        else:
            ray.get(self._ws.transition.remote(story_id, IssueStatus.TBD, "QA found discrepancy"))
        ray.get(self._sm.record.remote("outcome", story_id, passed=ok, by="QA"))
        return ok, reason


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
        # Structured, not just prose. `summary()` is a sentence, and a caller
        # writing the natural `if result["discriminates"]:` would take the
        # success branch for "DOES NOT REJECT A NULL IMPLEMENTATION" — a
        # non-empty string is truthy, so the one fact the field exists to convey
        # is the one a truthiness test cannot see. None means "not checked",
        # which is a third state and not the same as False.
        discrimination = staged.discrimination
        discriminates: bool | None = (
            None if discrimination is None or not discrimination.checked
            else discrimination.discriminates
        )
        verdict = discrimination.summary() if discrimination is not None else "not checked"
        ray.get(self._sm.record.remote(
            "contract_drafted", spec_id, contract=name, files=list(staged.files),
            staged_at=str(staged.directory), awaiting="human approval",
            discriminates=discriminates, discrimination_detail=verdict,
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
            "discriminates": discriminates,          # True | False | None (not checked)
            "discrimination_detail": verdict,
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

    def _cloud_for(self, spec: contract.Contract) -> Any:
        """The ``ServeCloud`` for *spec*, built and served on first use.

        Cached per contract name: a second construction would call
        ``serve_blue()`` again and, per OMNI-13's finding, needlessly cycle a
        replica the first construction already stood up correctly. Ray is
        already initialised — this runs inside a live Ray actor — so only
        Serve needs an explicit, idempotent start.
        """
        if not isinstance(spec, contract.OptimizationContract):
            raise TypeError(f"the Serve canary needs a reference and a benchmark, which "
                            f"contract {spec.name!r} (Class 2) does not have")
        if spec.name not in self._serve_clouds:
            from ray import serve

            from sis.serve_cloud import ServeCloud

            serve.start(logging_config={"log_level": "ERROR"})
            cloud = ServeCloud(_RemoteTelemetry(self._ws), spec)
            cloud.serve_blue(version="live")
            self._serve_clouds[spec.name] = cloud
        return self._serve_clouds[spec.name]

    def canary(
        self, pr_id: str, candidate_latency: float | None, canary_backend: str | None = None
    ) -> dict[str, Any]:
        # candidate_latency was measured inside the gauntlet sandbox by the SWE
        # step. On the legacy backend the candidate is NEVER executed here
        # (main process, holds creds). On "serve" it runs in a Serve replica
        # with a scrubbed runtime_env (OMNI-13) — a different, procedural
        # guarantee, and the intended shape of a canary.
        pr = ray.get(self._ws.get_pr.remote(pr_id, self._pr_target_path(pr_id)))
        version = _version_for(pr)

        # Explicit argument first, then configuration. Reading an env var
        # "fresh" inside an already-running actor is not fresh at all: the
        # actor's os.environ is a snapshot from when its OS process was
        # spawned, so a test's monkeypatch.setenv() (a different process) can
        # never reach it. Same trap as contracts.default (docs/KNOWN_ISSUES.md,
        # and Role._contract above); same fix. The config.yml layer is read from
        # disk per process and so does reach here, but the argument still wins.
        backend = canary_backend or config.get("canary.backend")
        self._pr_backend[pr_id] = "serve" if backend == "serve" else "legacy"

        if backend == "serve":
            if candidate_latency is None:  # run_cycle refuses this before any spend
                raise RuntimeError("the Serve canary compares latencies, and a built "
                                   "feature (Class 2) has none")
            return self._canary_live(pr, version, candidate_latency)

        # A built feature (Class 2) has no latency to record (OMNI-147).
        metrics = {} if candidate_latency is None else {"latency_seconds": candidate_latency}
        record = ray.get(self._ws.deploy_canary.remote(version, metrics))
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
        # invariants (only FeatureContracts declare them, and none is served
        # yet — OMNI-18 built the offline gate), so SPLIT mode would have
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
        # Status only: a poll that fires every few seconds while a human
        # reviews has no use for the file, so it does not fetch one.
        try:
            pr = ray.get(self._ws.get_pr.remote(pr_id, None))
        except PullRequestNotFound:
            # A remembered PR that no longer exists (OMNI-126): nothing is left
            # to wait for. Anything else raised here (an outage) propagates, and
            # the hold stays.
            green = ray.get(self._sm.deployment.remote())["slots"].get("green")
            return self._release(pr_id, str(green) if green else None, "no longer exists")
        version = _version_for(pr)
        resolution = pr_resolution(pr)
        if resolution == "hold":
            # The overwhelmingly common case on any given tick. Deliberately
            # silent — emitting here would bury the audit trail under one event
            # per poll while a human takes hours to review.
            return {"pr": pr_id, "version": version, "merged": False, "promoted": False}
        if resolution == "release":
            return self._release(pr_id, version, "closed without merging")

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

    def _release(self, pr_id: str, version: str | None, why: str) -> dict[str, Any]:
        """A human declined *pr_id* (or it is gone): retire its canary, free green.

        Before OMNI-57 nothing did this, so a closed PR held the canary, and
        under ``loop.serve`` the whole loop, forever.
        """
        if version is not None:
            self.retire_canary(version, pr_id)
        else:  # nothing deployed to retire, but the hold itself must go
            ray.get(self._sm.set_slot.remote("green", None))
            ray.get(self._sm.set_pending_pr.remote(None))
        ray.get(self._ws.emit.remote("pr.released", pr_id=pr_id, version=version, why=why))
        # The contract rides along so the driver's episodic record of this
        # human decision says which target it was about (OMNI-57, OMNI-121).
        spec: contract.OptimizationContract | None = ray.get(
            self._sm.contract_for_pr.remote(pr_id))
        return {"pr": pr_id, "version": version, "merged": False, "promoted": False,
                "released": True, "reason": why, "contract": spec.name if spec else None}

    def adopt_pending(
        self, pr_id: str, version: str, contract_name: str | None = None
    ) -> dict[str, Any]:
        """Put back a hold a previous process left, then resolve it (OMNI-126).

        The hold lives in this cluster's memory, and every ``main.py`` starts
        its own cluster, so a PR still awaiting review used to be forgotten
        on restart. The next run then proposed the same change again: on the
        second AWS run, ``testrun`` PRs #11 and #12, 47 s apart.

        Records in the SelfModel what ``canary()`` recorded there (green, the
        pending PR, its contract; not the Cloud's deploy record, which
        promotion does not need), then asks the PR's current state through
        :meth:`observe_merge`: merged since → promote, closed → release,
        open → the hold stands. Pure bookkeeping before the check, so when the
        check itself fails (an outage) the hold is already in place: failing
        closed, as the brake state does (OMNI-61).

        A green slot already held by a *different* PR is left alone: a
        surviving detached SelfModel knows better than a file does.
        """
        held = ray.get(self._sm.deployment.remote())
        if held["pending_pr"] not in (None, pr_id):
            return {"pr": pr_id, "version": version, "adopted": False,
                    "reason": f"green is already held for PR {held['pending_pr']}"}
        if contract_name:
            ray.get(self._sm.set_pr_contract.remote(pr_id, contract_name))
        ray.get(self._sm.set_slot.remote("green", version))
        ray.get(self._sm.set_pending_pr.remote(pr_id))
        ray.get(self._sm.record.remote("canary_restored", version, pr=pr_id))
        return {"adopted": True, **self.observe_merge(pr_id)}

    def adopt_open_pr(self) -> dict[str, Any] | None:
        """Hold for the oldest of the loop's PRs still open on the VCS (OMNI-136).

        The version-control system is the source of truth for what awaits a
        human. OMNI-126's record of the pending PR is a file on the box, and a
        rebuilt box started without it: ``testrun`` #14 opened next to #13,
        from the same base, on 2026-09-29. A feature PR that QA did not approve
        is open with no hold at all. Both end here, before the next cycle.

        None when something is already held, or nothing of the loop's is open.
        Otherwise :meth:`adopt_pending`'s outcome, plus ``open``: every such PR,
        oldest first. The next one is adopted once this one is decided. A
        failure to list raises, so the caller never reads "could not ask" as
        "nothing open".
        """
        if ray.get(self._sm.deployment.remote())["pending_pr"] is not None:
            return None
        waiting = feature_mod.awaiting_decision(ray.get(self._ws.open_prs.remote()))
        if not waiting:
            return None
        pr = waiting[0]
        known = [c.name for c in contract.REGISTERED_CONTRACTS]
        outcome = self.adopt_pending(
            pr.id, _version_for(pr), feature_mod.contract_from_title(pr.title, known))
        return {**outcome, "open": [p.id for p in waiting]}

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

    def _pr_target_path(self, pr_id: str) -> str:
        """The file a PR's candidate lives in: its recorded contract's target.

        The SWE records the contract before the PR can reach a canary; the
        default contract covers a PR opened some other way (tests, a manual
        call), which is what the old hardcoded path meant anyway.
        """
        spec: contract.Contract | None = ray.get(
            self._sm.contract_for_pr.remote(pr_id))
        return (spec or contract.default_contract()).target_path

    def _contract_for_live_pr(self, pr_id: str) -> contract.Contract:
        spec: contract.Contract | None = ray.get(
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
