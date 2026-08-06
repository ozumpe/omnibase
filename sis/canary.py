"""sis.canary — the online half of the validation contract.

The gauntlet (:mod:`sis.gauntlet`) checks a candidate offline against generated
inputs. This checks the *same predicates* against **live** request/response
pairs, which is the only way to answer "is it correct, and is it better?" using
production traffic — traffic that has no stored expected output. Design and
rationale: ``docs/SERVE_CANARY.md``.

:func:`evaluate_canary` is pure — no Ray, no Serve, no network, no clock — per
the project convention that decision logic is unit-testable on its own
(``evaluate_brakes``, ``loop.decide``, ``loop.serve_breach``). The Ray/Serve
shell that deploys, splits traffic, collects a window and acts on the verdict is
``DevOps.canary()`` (sequencing step 10).

**Everything here fails closed.** A predicate that raises, a window too thin to
judge, a shadow sample missing its baseline — none of them promote. A canary's
whole job is to be the last thing between a bad candidate and live traffic, so
"couldn't tell" must never read as "fine".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from sis.metrics import percentile

# A promote/rollback decision needs more evidence behind it than a "should we
# even look" trigger does, so this floor is higher than loop's breach floor.
# Both are the same idea as L5's noise floor: a percentile over a handful of
# requests is not a measurement, it is one request wearing a hat.
DEFAULT_MIN_CANARY_SAMPLES = 100


class CanaryMode(str, Enum):
    """How the canary sees traffic — a per-target choice, not a global one.

    ``SHADOW`` dispatches each sampled request to **both** versions and returns
    only the live one's answer. It buys a paired latency comparison (same
    request, same instant, so the traffic-mix confounder disappears) and a
    direct response-agreement check, at the cost of double compute on sampled
    requests. It is only sound where a request has *one* correct answer.

    ``SPLIT`` is a plain weighted split: each request reaches exactly one
    version, so no baseline response exists to compare against and the agreement
    gate is skipped. The right mode for targets with several valid answers.
    """

    SHADOW = "shadow"
    SPLIT = "split"


@runtime_checkable
class BoundInvariant(Protocol):
    """A contract invariant with its predicate already resolved to a callable.

    ``docs/CLASS2_CONTRACT.md``'s ``Invariant`` carries ``strategy``/``check`` as
    *strings* — names resolved inside the sandbox, because the offline gate
    executes untrusted candidate code there. The canary is the opposite
    situation: it judges responses that have already crossed the network, in the
    main process, using predicates from the POLICY-FORBIDDEN contract module
    (trusted). So it wants the bound callable, and this Protocol is the shape
    that resolution has to produce. L5 Layer 1 owns the resolution step; naming
    it ``BoundInvariant`` keeps it from colliding with the contract's data class
    when that lands.
    """

    @property
    def name(self) -> str: ...

    def check(self, request: Any, response: Any) -> bool: ...


@dataclass(frozen=True)
class LiveSample:
    """One observed request and what the deployed version(s) answered."""

    request: Any
    candidate_response: Any
    # SHADOW only — None under SPLIT, where the request only ever reached one
    # version. See CanaryMode.
    baseline_response: Any | None = None


@dataclass(frozen=True)
class CanaryVerdict:
    """The canary's answer, shaped like ``gauntlet.Result`` so it slots into the
    existing episodic/provenance plumbing."""

    version: str
    samples: int
    invariant_violations: int
    response_disagreements: int  # SHADOW only; always 0 under SPLIT
    baseline_p95: float
    baseline_p99: float
    candidate_p95: float
    candidate_p99: float
    passed: bool
    reason: str


def _violations(
    invariants: Sequence[BoundInvariant], samples: Sequence[LiveSample]
) -> tuple[int, str | None]:
    """Count invariant failures over the window; name the first one that broke.

    A predicate that *raises* counts as a violation rather than propagating: the
    predicate is trusted code, but the response it is handed came from an
    untrusted candidate, so "this response made the check blow up" is a
    statement about the candidate and must not take the loop down with it.
    """
    count = 0
    first: str | None = None
    for sample in samples:
        for invariant in invariants:
            try:
                held = invariant.check(sample.request, sample.candidate_response)
            except Exception:  # noqa: BLE001 - a broken response is the candidate's fault
                held = False
            if not held:
                count += 1
                if first is None:
                    first = invariant.name
    return count, first


def _disagreements(samples: Sequence[LiveSample]) -> int:
    """Shadow only: sampled requests where blue and green answered differently."""
    count = 0
    for sample in samples:
        try:
            differs = sample.candidate_response != sample.baseline_response
        except Exception:  # noqa: BLE001 - an uncomparable response is a disagreement
            differs = True
        if differs:
            count += 1
    return count


def evaluate_canary(
    invariants: Sequence[BoundInvariant],
    samples: Sequence[LiveSample],
    baseline_latencies: Sequence[float],
    candidate_latencies: Sequence[float],
    *,
    version: str,
    mode: CanaryMode = CanaryMode.SHADOW,
    min_samples: int = DEFAULT_MIN_CANARY_SAMPLES,
    max_latency_ratio: float = 1.0,
) -> CanaryVerdict:
    """Decide whether ``version`` may be offered for promotion. Pure.

    Gates, cheapest and hardest first — every applicable one must pass:

    1. **Evidence.** Enough samples and enough latency observations to judge.
    2. **Correctness (hard).** No invariant violation over the window. Same
       anti-gaming role the offline differential-correctness gate plays: the
       candidate cannot special-case inputs it never sees coming.
    3. **Agreement (hard, SHADOW only).** Blue and green answered every sampled
       request identically.
    4. **Performance (graded).** Candidate p95 *and* p99 within
       ``max_latency_ratio`` of baseline's. Default 1.0 = "not worse"; pass e.g.
       0.9 to demand the offline gate's 10% margin.

    Passing is *not* promotion — it only makes the candidate eligible for the
    human PR merge, which is what actually promotes (``CLAUDE.md`` hard rules).
    """
    if max_latency_ratio <= 0:
        raise ValueError(f"max_latency_ratio must be > 0, got {max_latency_ratio}")

    b_p95, b_p99 = percentile(baseline_latencies, 95), percentile(baseline_latencies, 99)
    c_p95, c_p99 = percentile(candidate_latencies, 95), percentile(candidate_latencies, 99)

    def verdict(passed: bool, reason: str, *, violations: int = 0, disagreements: int = 0
                ) -> CanaryVerdict:
        return CanaryVerdict(
            version=version, samples=len(samples),
            invariant_violations=violations, response_disagreements=disagreements,
            baseline_p95=b_p95, baseline_p99=b_p99,
            candidate_p95=c_p95, candidate_p99=c_p99,
            passed=passed, reason=reason,
        )

    # --- Gate 1: enough evidence to decide at all ---
    thinnest = min(len(samples), len(baseline_latencies), len(candidate_latencies))
    if thinnest < min_samples:
        return verdict(
            False,
            f"insufficient evidence: {thinnest} observations, need {min_samples} "
            "(a percentile over a handful of requests is not a measurement)",
        )

    # A shadow window that lost its baselines is a harness fault, not a verdict on
    # the candidate — say so, the way the gauntlet's missing-test-suite reason
    # does, so the analytics don't record it as the candidate disagreeing.
    if mode is CanaryMode.SHADOW and any(s.baseline_response is None for s in samples):
        return verdict(
            False,
            "harness: SHADOW mode sample is missing its baseline_response — "
            "the shadow dispatcher recorded only one side",
        )

    # --- Gate 2: correctness (hard) ---
    violations, first_broken = _violations(invariants, samples)
    if violations:
        return verdict(
            False,
            f"invariant violated: {first_broken} ({violations} of "
            f"{len(samples) * max(len(invariants), 1)} checks failed)",
            violations=violations,
        )

    # --- Gate 3: response agreement (hard, SHADOW only) ---
    disagreements = _disagreements(samples) if mode is CanaryMode.SHADOW else 0
    if disagreements:
        return verdict(
            False,
            f"response disagreement: {disagreements} of {len(samples)} sampled "
            "requests got a different answer from the candidate",
            disagreements=disagreements,
        )

    # --- Gate 4: performance (graded) ---
    for label, baseline, candidate in (("p95", b_p95, c_p95), ("p99", b_p99, c_p99)):
        if candidate > baseline * max_latency_ratio:
            return verdict(
                False,
                f"live {label} regression: candidate {candidate:.6f}s vs baseline "
                f"{baseline:.6f}s (need <= {baseline * max_latency_ratio:.6f}s)",
            )

    return verdict(True, f"canary passed on {len(samples)} live samples")
