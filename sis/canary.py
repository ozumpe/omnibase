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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from sis.canonical import canonical
from sis.metrics import percentile

# A promote/rollback decision needs more evidence behind it than a "should we
# even look" trigger does, so this floor is higher than loop's breach floor.
# Both are the same idea as L5's noise floor: a percentile over a handful of
# requests is not a measurement, it is one request wearing a hat.
DEFAULT_MIN_CANARY_SAMPLES = 100

# Extra allowance on the p99 comparison only, as a fraction of the p99 limit.
# p95 gets none. The asymmetry is statistical: nearest-rank p99 is decided by
# the slowest ~1% of the window, so at the live path's ~150 requests it is the
# *second-slowest request* — one GC pause or scheduler hiccup on a shared host
# moves it. CI measured exactly that: a known-good candidate rejected on p99
# 118.5 ms vs 117.4 ms, a 1% "regression" that is noise. 10% absorbs that while
# a real tail regression (tens of percent, or a stuck request) still fails.
DEFAULT_P99_NOISE_TOLERANCE = 0.10
# Upper bound on the allowance, so it can be tuned but never set to "anything
# passes": beyond this it is no longer noise tolerance, it is not checking p99.
MAX_P99_NOISE_TOLERANCE = 0.5

# The paired latency decision's non-inferiority margin (OMNI-50, M20): green's
# total latency over the paired window may be this fraction above
# ``max_latency_ratio`` times blue's. The canary only has to catch a real
# regression; the offline benchmark already showed the candidate faster by the
# contract's margin. Bounded by MAX_P99_NOISE_TOLERANCE for the same reason.
DEFAULT_LATENCY_TOLERANCE = 0.10
# Confidence of the paired-bootstrap interval. Lower than the offline gate's,
# because here the interval decides only between pass and inconclusive (a
# neutral outcome), never between pass and reject.
DEFAULT_LATENCY_CONFIDENCE = 0.90


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
    """One observed request and what the deployed version(s) answered.

    Under SHADOW the router records both sides' latencies on the sample, so
    the latency decision compares *pairs* (same request, same instant) rather
    than two unpaired arrays (OMNI-50, M20). A request where blue answered and
    green failed is recorded too, with ``candidate_error`` set: it is a
    disagreement, not a sample to drop (M21).
    """

    request: Any
    candidate_response: Any
    # SHADOW only — None under SPLIT, where the request only ever reached one
    # version. See CanaryMode.
    baseline_response: Any | None = None
    baseline_latency: float | None = None
    candidate_latency: float | None = None
    # Green's error, when it failed a request blue answered. Its response and
    # latency are then not evidence of anything but the failure.
    candidate_error: str | None = None


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
    # Neither passed nor failed: the paired latency interval could not tell
    # (OMNI-50). Neutral, like an inconclusive offline benchmark.
    inconclusive: bool = False
    baseline_errors: int = 0
    candidate_errors: int = 0
    # Paired decision only: sum(green) / sum(blue) and its interval's bounds.
    latency_ratio: float | None = None
    latency_ci_low: float | None = None
    latency_ci_high: float | None = None


def _violations(
    invariants: Sequence[BoundInvariant], samples: Sequence[LiveSample]
) -> tuple[int, str | None]:
    """Count invariant failures over the window; name the first one that broke.

    A predicate that *raises* counts as a violation rather than propagating: the
    predicate is trusted code, but the response it is handed came from an
    untrusted candidate, so "this response made the check blow up" is a
    statement about the candidate and must not take the loop down with it.

    The response is rebuilt from plain builtins first (OMNI-46, H4): a live
    response is unpickled by value (KNOWN_ISSUES L28), so it can carry a type
    whose ``__eq__`` agrees with any law that compares. One that is not plain
    raises :class:`sis.canonical.NotPlainError` — a violation, like any raise.
    """
    count = 0
    first: str | None = None
    for sample in samples:
        if sample.candidate_error is not None:
            continue  # a failure, judged by the error-rate and agreement gates
        for invariant in invariants:
            try:
                held = invariant.check(sample.request, canonical(sample.candidate_response))
            except Exception:  # noqa: BLE001 - a broken response is the candidate's fault
                held = False
            if not held:
                count += 1
                if first is None:
                    first = invariant.name
    return count, first


def _disagreements(samples: Sequence[LiveSample]) -> int:
    """Shadow only: sampled requests where blue and green answered differently.

    A request blue answered and green failed is a disagreement (M21): dropping
    it, as the router once did, judged green only on the requests it survived.
    """
    count = 0
    for sample in samples:
        if sample.candidate_error is not None:
            count += 1
            continue
        try:
            # Plain values only on the candidate's side of `!=` (OMNI-46, H4):
            # otherwise its own __ne__ decides whether it disagrees.
            differs = canonical(sample.candidate_response) != sample.baseline_response
        except Exception:  # noqa: BLE001 - an uncomparable response is a disagreement
            differs = True
        if differs:
            count += 1
    return count


def _shadow_pairs(samples: Sequence[LiveSample]) -> list[tuple[float, float]] | None:
    """``(candidate, baseline)`` latency per answered shadow sample, or ``None``
    when any sample lacks its latencies (a caller that recorded none)."""
    pairs: list[tuple[float, float]] = []
    for sample in samples:
        if sample.candidate_error is not None:
            continue  # failures return fast; they would pull green's cost down
        if sample.candidate_latency is None or sample.baseline_latency is None:
            return None
        pairs.append((sample.candidate_latency, sample.baseline_latency))
    return pairs


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
    p99_noise_tolerance: float = DEFAULT_P99_NOISE_TOLERANCE,
    baseline_errors: int = 0,
    candidate_errors: int = 0,
    pairs: Sequence[tuple[float, float]] | None = None,
    latency_tolerance: float = DEFAULT_LATENCY_TOLERANCE,
    latency_confidence: float = DEFAULT_LATENCY_CONFIDENCE,
) -> CanaryVerdict:
    """Decide whether ``version`` may be offered for promotion. Pure.

    Gates, cheapest and hardest first — every applicable one must pass:

    1. **Evidence.** Enough samples and enough latency observations to judge.
    2. **Error rate (hard, OMNI-50, M21).** Green's share of failed calls
       (``candidate_errors`` over ``candidate_latencies``, which counts every
       call, failed or not) is no higher than blue's.
    3. **Correctness (hard).** No invariant violation over the window. Same
       anti-gaming role the offline differential-correctness gate plays: the
       candidate cannot special-case inputs it never sees coming.
    4. **Agreement (hard, SHADOW only).** Blue and green answered every sampled
       request identically; a request green failed is a disagreement.
    5. **Performance.** Paired where the window is paired (OMNI-50, M20):
       ``pairs`` of ``(candidate, baseline)`` seconds, or, under SHADOW, the
       latencies recorded on every sample. Decided like the offline benchmark
       (:func:`sis.gauntlet.benchmark_decision`), on total cost with a paired
       bootstrap, against the limit ``max_latency_ratio * (1 +
       latency_tolerance)``: **pass** if the interval's upper bound is within
       it, **fail** if the point estimate is over it, otherwise
       **inconclusive** (neutral). An unpaired window (SPLIT) keeps the
       percentile rule: p95 within ``max_latency_ratio`` of baseline's, p99
       within that times ``1 + p99_noise_tolerance``.

    Two unpaired percentiles at ratio 1.0 were close to a coin flip: an
    equal-speed candidate failed ~56% of simulated windows (M20).

    Passing is *not* promotion — it only makes the candidate eligible for the
    human PR merge, which is what actually promotes (``CLAUDE.md`` hard rules).
    """
    if max_latency_ratio <= 0:
        raise ValueError(f"max_latency_ratio must be > 0, got {max_latency_ratio}")
    if not 0.0 <= p99_noise_tolerance <= MAX_P99_NOISE_TOLERANCE:
        raise ValueError(
            f"p99_noise_tolerance must be in [0, {MAX_P99_NOISE_TOLERANCE}], got "
            f"{p99_noise_tolerance} — above that the gate no longer checks the tail"
        )
    if not 0.0 <= latency_tolerance <= MAX_P99_NOISE_TOLERANCE:
        raise ValueError(
            f"latency_tolerance must be in [0, {MAX_P99_NOISE_TOLERANCE}], got "
            f"{latency_tolerance} — above that the gate no longer checks latency"
        )
    if baseline_errors < 0 or candidate_errors < 0:
        raise ValueError("error counts must be >= 0")

    b_p95, b_p99 = percentile(baseline_latencies, 95), percentile(baseline_latencies, 99)
    c_p95, c_p99 = percentile(candidate_latencies, 95), percentile(candidate_latencies, 99)
    paired = list(pairs) if pairs is not None else (
        _shadow_pairs(samples) if mode is CanaryMode.SHADOW else None)

    def verdict(passed: bool, reason: str, *, violations: int = 0, disagreements: int = 0,
                inconclusive: bool = False, ratio: tuple[float, float, float] | None = None,
                ) -> CanaryVerdict:
        return CanaryVerdict(
            version=version, samples=len(samples),
            invariant_violations=violations, response_disagreements=disagreements,
            baseline_p95=b_p95, baseline_p99=b_p99,
            candidate_p95=c_p95, candidate_p99=c_p99,
            passed=passed, reason=reason, inconclusive=inconclusive,
            baseline_errors=baseline_errors, candidate_errors=candidate_errors,
            latency_ratio=ratio[0] if ratio else None,
            latency_ci_low=ratio[1] if ratio else None,
            latency_ci_high=ratio[2] if ratio else None,
        )

    # --- Gate 1: enough evidence to decide at all ---
    observed = [len(samples), len(baseline_latencies), len(candidate_latencies)]
    if paired is not None:
        observed.append(len(paired))
    thinnest = min(observed)
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

    # --- Gate 2: error rate (hard) ---
    # Rates, not counts: the two sides need not have served as many calls
    # (SPLIT sends green a fraction). The latency arrays hold failed calls too.
    b_rate = baseline_errors / len(baseline_latencies)
    c_rate = candidate_errors / len(candidate_latencies)
    if c_rate > b_rate:
        return verdict(
            False,
            f"live error rate: candidate failed {candidate_errors} of "
            f"{len(candidate_latencies)} calls ({c_rate:.1%}), baseline "
            f"{baseline_errors} of {len(baseline_latencies)} ({b_rate:.1%})",
        )

    # --- Gate 3: correctness (hard) ---
    violations, first_broken = _violations(invariants, samples)
    if violations:
        return verdict(
            False,
            f"invariant violated: {first_broken} ({violations} of "
            f"{len(samples) * max(len(invariants), 1)} checks failed)",
            violations=violations,
        )

    # --- Gate 4: response agreement (hard, SHADOW only) ---
    disagreements = _disagreements(samples) if mode is CanaryMode.SHADOW else 0
    if disagreements:
        return verdict(
            False,
            f"response disagreement: {disagreements} of {len(samples)} sampled "
            "requests got a different answer from the candidate",
            disagreements=disagreements,
        )

    # --- Gate 5: performance ---
    if paired is not None:
        return _paired_latency(paired, max_latency_ratio * (1.0 + latency_tolerance),
                               latency_confidence, len(samples), verdict)

    limits = (
        ("p95", b_p95, c_p95, max_latency_ratio),
        ("p99", b_p99, c_p99, max_latency_ratio * (1.0 + p99_noise_tolerance)),
    )
    for label, baseline, candidate, ratio in limits:
        if candidate > baseline * ratio:
            return verdict(
                False,
                f"live {label} regression: candidate {candidate:.6f}s vs baseline "
                f"{baseline:.6f}s (need <= {baseline * ratio:.6f}s)",
            )

    return verdict(True, f"canary passed on {len(samples)} live samples")


def _paired_latency(
    pairs: Sequence[tuple[float, float]], limit: float, confidence: float, samples: int,
    verdict: Callable[..., CanaryVerdict],
) -> CanaryVerdict:
    """Gate 5 on a paired window: the offline benchmark's rule, as non-inferiority."""
    # Imported here: the gauntlet imports a great deal, and this module is
    # imported by the Serve replicas, which need none of it.
    from sis.gauntlet import BenchmarkVerdict, benchmark_decision

    d = benchmark_decision(pairs, max_ratio=limit, confidence=confidence)
    ratio = (d.ratio, d.ci_low, d.ci_high)
    evidence = (f"total latency ratio {d.ratio:.3f}, {confidence:.0%} interval "
                f"[{d.ci_low:.3f}, {d.ci_high:.3f}] over {d.samples} pairs, "
                f"limit {limit:.3f}")
    if d.verdict is BenchmarkVerdict.ACCEPT:
        return verdict(
            True, f"canary passed on {samples} live samples ({evidence})", ratio=ratio)
    if d.verdict is BenchmarkVerdict.INCONCLUSIVE:
        return verdict(
            False, f"live latency inconclusive: {evidence}", inconclusive=True, ratio=ratio)
    if d.verdict is BenchmarkVerdict.UNMEASURABLE:
        return verdict(
            False, f"insufficient evidence: {d.samples} usable latency pairs")
    return verdict(
        False, f"live latency regression: {evidence}", ratio=ratio)
