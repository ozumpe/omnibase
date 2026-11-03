"""Tests for the online canary verdict — pure, no Ray/Serve/network/clock."""

import math
from dataclasses import dataclass
from typing import Any

import pytest

from sis.canary import (
    BoundInvariant,
    CanaryMode,
    LiveSample,
    evaluate_canary,
)


@dataclass(frozen=True)
class _Invariant:
    """A bound invariant built from a plain predicate (the resolved form)."""

    name: str
    predicate: Any

    def check(self, request: Any, response: Any) -> bool:
        return bool(self.predicate(request, response))


SORTED_PERMUTATION = _Invariant(
    "sorted_permutation",
    lambda req, resp: list(resp) == sorted(req) and sorted(resp) == sorted(req),
)


def _samples(n: int, *, shadow: bool = True, bad_at: int | None = None) -> list[LiveSample]:
    out = []
    for i in range(n):
        request = [3, 1, 2, i]
        correct = sorted(request)
        candidate = [9, 9, 9, 9] if i == bad_at else correct
        out.append(LiveSample(
            request=request,
            candidate_response=candidate,
            baseline_response=correct if shadow else None,
        ))
    return out


def _latencies(n: int, value: float) -> list[float]:
    return [value] * n


def test_a_healthy_canary_passes() -> None:
    verdict = evaluate_canary(
        [SORTED_PERMUTATION], _samples(100),
        _latencies(100, 0.010), _latencies(100, 0.008),
        version="v1", min_samples=100,
    )
    assert verdict.passed, verdict.reason
    assert verdict.invariant_violations == 0
    assert verdict.response_disagreements == 0
    assert verdict.samples == 100


def test_the_bound_invariant_protocol_is_satisfied_by_a_plain_object() -> None:
    # The canary wants the *resolved* callable form, not CLASS2_CONTRACT's
    # string-valued data class. Anything with name + check(request, response) fits.
    assert isinstance(SORTED_PERMUTATION, BoundInvariant)


def test_an_invariant_violation_fails_hard() -> None:
    verdict = evaluate_canary(
        [SORTED_PERMUTATION], _samples(100, bad_at=7),
        _latencies(100, 0.010), _latencies(100, 0.001),  # 10x faster: must not save it
        version="v1", min_samples=100,
    )
    assert not verdict.passed
    assert verdict.invariant_violations == 1
    assert "sorted_permutation" in verdict.reason


def test_a_predicate_that_raises_counts_as_a_violation() -> None:
    # The predicate is trusted, but the response it is handed came from an
    # untrusted candidate. "This response blew the check up" is a statement about
    # the candidate, and must not propagate out and take the loop down.
    exploding = _Invariant("explodes", lambda req, resp: 1 / 0)
    verdict = evaluate_canary(
        [exploding], _samples(100),
        _latencies(100, 0.010), _latencies(100, 0.008),
        version="v1", min_samples=100,
    )
    assert not verdict.passed
    assert verdict.invariant_violations == 100


def test_shadow_disagreement_fails_even_when_invariants_hold() -> None:
    # Both answers can be *valid* permutations and still differ; under SHADOW
    # that is a regression, which is why the mode is per-target.
    samples = [LiveSample(request=[1, 2], candidate_response=[1, 2],
                          baseline_response=[2, 1]) for _ in range(100)]
    verdict = evaluate_canary(
        [], samples, _latencies(100, 0.010), _latencies(100, 0.008),
        version="v1", mode=CanaryMode.SHADOW, min_samples=100,
    )
    assert not verdict.passed
    assert verdict.response_disagreements == 100
    assert "disagreement" in verdict.reason


def test_split_mode_skips_the_agreement_gate() -> None:
    # Under a weighted split each request reaches exactly one version, so there
    # is no baseline response to compare — the gate must not fire on its absence.
    verdict = evaluate_canary(
        [SORTED_PERMUTATION], _samples(100, shadow=False),
        _latencies(100, 0.010), _latencies(100, 0.008),
        version="v1", mode=CanaryMode.SPLIT, min_samples=100,
    )
    assert verdict.passed, verdict.reason
    assert verdict.response_disagreements == 0


def test_shadow_missing_a_baseline_blames_the_harness() -> None:
    # A shadow window that lost its baselines is a dispatcher fault. Recording it
    # as "the candidate disagreed" would blame the wrong side in the analytics,
    # exactly like the gauntlet's missing-test-suite case.
    verdict = evaluate_canary(
        [SORTED_PERMUTATION], _samples(100, shadow=False),  # SHADOW, but no baselines
        _latencies(100, 0.010), _latencies(100, 0.008),
        version="v1", mode=CanaryMode.SHADOW, min_samples=100,
    )
    assert not verdict.passed
    assert verdict.reason.startswith("harness:")
    assert verdict.response_disagreements == 0  # not attributed to the candidate


def test_a_thin_window_cannot_promote() -> None:
    # Fail closed: "couldn't tell" must never read as "fine".
    verdict = evaluate_canary(
        [SORTED_PERMUTATION], _samples(5),
        _latencies(5, 0.010), _latencies(5, 0.001),
        version="v1", min_samples=100,
    )
    assert not verdict.passed
    assert "insufficient evidence" in verdict.reason


def test_thin_latency_arrays_also_block_promotion() -> None:
    # Plenty of correctness samples, but almost no timing behind the percentiles.
    verdict = evaluate_canary(
        [SORTED_PERMUTATION], _samples(100),
        _latencies(3, 0.010), _latencies(3, 0.008),
        version="v1", min_samples=100,
    )
    assert not verdict.passed
    assert "insufficient evidence" in verdict.reason


def test_a_latency_regression_fails() -> None:
    verdict = evaluate_canary(
        [SORTED_PERMUTATION], _samples(100),
        _latencies(100, 0.010), _latencies(100, 0.020),
        version="v1", min_samples=100,
    )
    assert not verdict.passed
    assert "regression" in verdict.reason


def test_equal_latency_passes_at_the_default_ratio() -> None:
    # Default 1.0 means "not worse", not "must be faster" — a Class-2 feature
    # canary is about not regressing, unlike the offline optimisation margin.
    verdict = evaluate_canary(
        [SORTED_PERMUTATION], _samples(100),
        _latencies(100, 0.010), _latencies(100, 0.010),
        version="v1", min_samples=100,
    )
    assert verdict.passed, verdict.reason


def test_max_latency_ratio_can_demand_a_margin() -> None:
    # 0.9 = the offline gate's "at least 10% faster", applied to live traffic.
    faster = evaluate_canary(
        [SORTED_PERMUTATION], _samples(100),
        _latencies(100, 0.010), _latencies(100, 0.008),
        version="v1", min_samples=100, max_latency_ratio=0.9,
    )
    assert faster.passed, faster.reason

    not_enough = evaluate_canary(
        [SORTED_PERMUTATION], _samples(100),
        _latencies(100, 0.010), _latencies(100, 0.0095),
        version="v1", min_samples=100, max_latency_ratio=0.9,
    )
    assert not not_enough.passed
    assert "regression" in not_enough.reason


def test_p99_regression_is_caught_even_when_p95_is_fine() -> None:
    # Tail latency is the SLO that matters (DESIGN.md §7 calls out p99); a
    # candidate that is fine at p95 and terrible at p99 must not pass.
    # Two slow requests, not one: with 100 samples the nearest-rank p99 is the
    # 99th value, so a single outlier is a p100 event and legitimately invisible
    # at p99 — it takes >1% of the window to move the tail.
    baseline = _latencies(100, 0.010)
    candidate = _latencies(98, 0.009) + [0.5, 0.5]
    verdict = evaluate_canary(
        [SORTED_PERMUTATION], _samples(100), baseline, candidate,
        version="v1", min_samples=100,
    )
    assert not verdict.passed
    assert "p99" in verdict.reason


def test_verdict_reports_both_percentile_pairs() -> None:
    verdict = evaluate_canary(
        [SORTED_PERMUTATION], _samples(100),
        _latencies(100, 0.010), _latencies(100, 0.008),
        version="v7", min_samples=100,
    )
    assert verdict.version == "v7"
    assert verdict.baseline_p95 == 0.010 and verdict.baseline_p99 == 0.010
    assert verdict.candidate_p95 == 0.008 and verdict.candidate_p99 == 0.008


def test_max_latency_ratio_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_latency_ratio"):
        evaluate_canary([], [], [], [], version="v1", max_latency_ratio=0.0)


# --- p99 noise tolerance ---------------------------------------------------


def _judge(baseline: float, candidate: float, n: int = 150, **kw: Any) -> Any:
    return evaluate_canary(
        [SORTED_PERMUTATION], _samples(n),
        _latencies(n, baseline), _latencies(n, candidate),
        version="v1", min_samples=100, **kw,
    )


def _judge_tail(base_p99: float, cand_p99: float, n: int = 150, *,
                cand_body: float = 0.010, **kw: Any) -> Any:
    """Only the tail differs, and by exactly enough requests to *be* the p99.

    Nearest-rank p99 over n samples is the ceil(0.99n)-th value, so the tail
    must hold n - ceil(0.99n) + 1 requests (2 at n=150) — one slow request is
    a p100 event and would leave p99 untouched, testing nothing.
    """
    tail = n - math.ceil(0.99 * n) + 1
    baseline = _latencies(n - tail, 0.010) + _latencies(tail, base_p99)
    candidate = _latencies(n - tail, cand_body) + _latencies(tail, cand_p99)
    return evaluate_canary(
        [SORTED_PERMUTATION], _samples(n), baseline, candidate,
        version="v1", min_samples=100, **kw,
    )


def test_the_ci_flake_a_one_percent_p99_wobble_passes() -> None:
    # The exact numbers that failed CI on PR #106: a known-good candidate
    # rejected on p99 118.507 ms vs 117.423 ms. That is noise at ~150 requests.
    verdict = _judge_tail(0.117423, 0.118507)
    assert verdict.passed, verdict.reason


def test_a_thirty_percent_p99_regression_still_fails() -> None:
    verdict = _judge_tail(0.100, 0.130)
    assert not verdict.passed
    assert "live p99 regression" in verdict.reason


def test_p95_gets_no_allowance() -> None:
    # The tolerance is p99-only: p95 over the same window is several requests
    # deep, so a 1% p95 regression still fails at the default ratio.
    verdict = _judge(0.100, 0.101)
    assert not verdict.passed
    assert "live p95 regression" in verdict.reason


def test_the_allowance_composes_with_a_demanded_margin() -> None:
    # ratio 0.9 → p95 must be <= 0.90x, p99 <= 0.99x (0.9 * 1.10). The
    # candidate body is 20% faster, so p95 clears the margin and only p99 decides.
    assert _judge_tail(0.100, 0.098, max_latency_ratio=0.9, cand_body=0.008).passed
    rejected = _judge_tail(0.100, 0.100, max_latency_ratio=0.9, cand_body=0.008)
    assert not rejected.passed
    assert "live p99 regression" in rejected.reason


def test_zero_tolerance_restores_the_strict_p99_check() -> None:
    verdict = _judge_tail(0.117423, 0.118507, p99_noise_tolerance=0.0)
    assert not verdict.passed
    assert "live p99 regression" in verdict.reason


@pytest.mark.parametrize("tolerance", [-0.01, 0.51, 1.0])
def test_the_tolerance_cannot_be_set_to_anything_passes(tolerance: float) -> None:
    with pytest.raises(ValueError, match="p99_noise_tolerance"):
        _judge(0.1, 0.1, p99_noise_tolerance=tolerance)


# --- H4 online: a response type that defines its own equality (OMNI-46) ------


class _LiarList(list[int]):
    """What an unpickled-by-value live response can be (KNOWN_ISSUES L28)."""

    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False


def _liar_samples(n: int) -> list[LiveSample]:
    return [LiveSample(request=[3, 1, 2], candidate_response=_LiarList([9, 9, 9]),
                       baseline_response=[1, 2, 3]) for _ in range(n)]


def test_a_liar_cannot_agree_with_blue_by_answering_the_comparison_itself() -> None:
    verdict = evaluate_canary(
        [], _liar_samples(100), _latencies(100, 0.010), _latencies(100, 0.008),
        version="v1", mode=CanaryMode.SHADOW, min_samples=100,
    )
    assert not verdict.passed
    assert verdict.response_disagreements == 100


def test_a_liar_cannot_satisfy_a_law_that_compares() -> None:
    compares = _Invariant("equals_sorted", lambda req, resp: resp == sorted(req))
    verdict = evaluate_canary(
        [compares], _liar_samples(100), _latencies(100, 0.010), _latencies(100, 0.008),
        version="v1", mode=CanaryMode.SPLIT, min_samples=100,
    )
    assert not verdict.passed
    assert verdict.invariant_violations == 100


# --- OMNI-50: paired latency (M20) and error rate (M21) ---------------------


def _paired_window(rng: Any, slow: float, n: int = 150, sigma: float = 0.35
                   ) -> tuple[list[LiveSample], list[float], list[float]]:
    """M20's simulation: independent lognormal latencies, recorded per sample."""
    samples, blue, green = [], [], []
    for i in range(n):
        b = 0.02 * rng.lognormvariate(0, sigma)
        g = 0.02 * slow * rng.lognormvariate(0, sigma)
        blue.append(b)
        green.append(g)
        samples.append(LiveSample(request=[i], candidate_response=i, baseline_response=i,
                                  baseline_latency=b, candidate_latency=g))
    return samples, blue, green


def _outcomes(slow: float, trials: int = 200) -> dict[str, int]:
    import collections
    import random

    rng = random.Random(1)
    counts: collections.Counter[str] = collections.Counter()
    for _ in range(trials):
        samples, blue, green = _paired_window(rng, slow)
        v = evaluate_canary([], samples, blue, green, version="v")
        counts["pass" if v.passed else "inconclusive" if v.inconclusive else "reject"] += 1
    return dict(counts)


def test_an_equal_speed_candidate_is_rarely_rejected() -> None:
    # M20: two unpaired percentiles at ratio 1.0 rejected an equal-speed
    # candidate ~56% of the time, each rejection a bug and a breaker count. On
    # the same windows, paired, a rejection is rare; "could not tell" is
    # neutral (inconclusive), never a failure.
    outcomes = _outcomes(1.0)
    assert outcomes.get("reject", 0) <= 10, outcomes  # <= 5%
    assert outcomes.get("pass", 0) >= 120, outcomes


def test_the_old_unpaired_rule_was_a_coin_flip_on_the_same_windows() -> None:
    # Pins M20 itself, so the comparison above is against a measured baseline.
    import random

    rng = random.Random(1)
    rejected = 0
    for _ in range(200):
        samples, blue, green = _paired_window(rng, 1.0)
        unpaired = [LiveSample(request=s.request, candidate_response=s.candidate_response,
                               baseline_response=s.baseline_response) for s in samples]
        rejected += not evaluate_canary([], unpaired, blue, green, version="v").passed
    assert rejected >= 80  # >= 40%


def test_a_thirty_percent_slower_candidate_is_rejected() -> None:
    outcomes = _outcomes(1.3, trials=50)
    assert outcomes == {"reject": 50}, outcomes


def test_a_paired_verdict_names_its_ratio_and_interval() -> None:
    import random

    samples, blue, green = _paired_window(random.Random(3), 1.5)
    v = evaluate_canary([], samples, blue, green, version="v")
    assert not v.passed and not v.inconclusive
    assert v.reason.startswith("live latency regression: total latency ratio")
    assert v.latency_ratio is not None and v.latency_ratio > 1.1
    assert v.latency_ci_low is not None and v.latency_ci_high is not None


def test_an_inconclusive_interval_is_neutral() -> None:
    from sis import episodic

    # Equal sums, very noisy pairs: the point estimate is within the limit,
    # the interval is not.
    samples = [LiveSample(request=[i], candidate_response=i, baseline_response=i,
                          baseline_latency=0.01, candidate_latency=0.001 if i % 2 else 0.019)
               for i in range(100)]
    v = evaluate_canary([], samples, [0.01] * 100, [0.01] * 100, version="v",
                        latency_tolerance=0.05)
    assert v.inconclusive and not v.passed, v.reason
    assert v.reason.startswith("live latency inconclusive")
    assert episodic.gate_from_reason(v.reason) == "canary_inconclusive"
    assert episodic.neutral_status(v.reason) == "inconclusive"


def test_explicit_pairs_override_the_samples() -> None:
    samples = _samples(100)  # no latencies on the samples
    v = evaluate_canary([], samples, [0.01] * 100, [0.01] * 100, version="v",
                        pairs=[(0.02, 0.01)] * 100)
    assert v.reason.startswith("live latency regression"), v.reason


def test_too_few_pairs_is_insufficient_evidence() -> None:
    v = evaluate_canary([], _samples(100), [0.01] * 100, [0.01] * 100, version="v",
                        pairs=[(0.01, 0.01)] * 20)
    assert v.reason.startswith("insufficient evidence: 20"), v.reason


def test_m21_a_third_of_green_failing_fast_is_rejected() -> None:
    # M21's simulation: 105 paired samples plus 45 fast green errors. The
    # errors once left the window unrecorded as samples and pulled green's
    # percentiles down, and the verdict was passed=True.
    samples = [LiveSample(request=[i], candidate_response=i, baseline_response=i)
               for i in range(105)]
    blue = [0.010] * 150
    green = [0.010] * 105 + [0.001] * 45
    v = evaluate_canary([], samples, blue, green, version="v",
                        baseline_errors=0, candidate_errors=45)
    assert not v.passed
    assert v.reason.startswith("live error rate: candidate failed 45 of 150"), v.reason
    assert v.candidate_errors == 45
    from sis import episodic

    assert episodic.gate_from_reason(v.reason) == "canary_errors"
    assert episodic.neutral_status(v.reason) is None


def test_green_failing_no_more_often_than_blue_passes_the_error_gate() -> None:
    v = evaluate_canary([SORTED_PERMUTATION], _samples(100),
                        _latencies(100, 0.010), _latencies(100, 0.008),
                        version="v", baseline_errors=3, candidate_errors=3)
    assert v.passed, v.reason


def test_error_rates_not_counts_are_compared() -> None:
    # SPLIT sends green a fraction of the traffic, so counts mislead: green's
    # 2 of 100 (2%) is below blue's 30 of 1000 (3%) and above 10 of 1000 (1%).
    lower = evaluate_canary([SORTED_PERMUTATION], _samples(100, shadow=False),
                            _latencies(1000, 0.010), _latencies(100, 0.008),
                            version="v", mode=CanaryMode.SPLIT,
                            baseline_errors=30, candidate_errors=2)
    higher = evaluate_canary([SORTED_PERMUTATION], _samples(100, shadow=False),
                             _latencies(1000, 0.010), _latencies(100, 0.008),
                             version="v", mode=CanaryMode.SPLIT,
                             baseline_errors=10, candidate_errors=2)
    assert lower.passed, lower.reason
    assert higher.reason.startswith("live error rate"), higher.reason


def test_a_request_green_failed_is_a_disagreement_not_a_dropped_sample() -> None:
    # Even with no error count passed in, a recorded green failure cannot pass.
    samples = _samples(100)
    samples[7] = LiveSample(request=samples[7].request, candidate_response=None,
                            baseline_response=samples[7].baseline_response,
                            candidate_error="ValueError: boom")
    v = evaluate_canary([SORTED_PERMUTATION], samples,
                        _latencies(100, 0.010), _latencies(100, 0.008), version="v")
    assert not v.passed
    assert v.invariant_violations == 0  # an error is not handed to the laws
    assert v.reason.startswith("response disagreement: 1 of 100"), v.reason


def test_failed_green_calls_are_left_out_of_the_latency_pairs() -> None:
    # A failure returns fast; counted as a pair it would make green look quicker.
    samples = [LiveSample(request=[i], candidate_response=i, baseline_response=i,
                          baseline_latency=0.01, candidate_latency=0.02)
               for i in range(100)]
    samples += [LiveSample(request=[i], candidate_response=None, baseline_response=i,
                           baseline_latency=0.01, candidate_latency=0.0001,
                           candidate_error="boom") for i in range(100, 400)]
    v = evaluate_canary([], samples, [0.01] * 400, [0.01] * 400, version="v",
                        mode=CanaryMode.SHADOW)
    # Disagreement decides first; the pairs it would have used exclude the failures.
    assert v.reason.startswith("response disagreement: 300"), v.reason
    from sis.canary import _shadow_pairs

    pairs = _shadow_pairs(samples)
    assert pairs is not None and len(pairs) == 100 and all(c == 0.02 for c, _ in pairs)


@pytest.mark.parametrize("bad", [-0.01, 0.51])
def test_latency_tolerance_is_bounded(bad: float) -> None:
    with pytest.raises(ValueError, match="latency_tolerance"):
        evaluate_canary([], _samples(100), [0.01] * 100, [0.01] * 100, version="v",
                        latency_tolerance=bad)


def test_negative_error_counts_are_refused() -> None:
    with pytest.raises(ValueError, match="error counts"):
        evaluate_canary([], _samples(100), [0.01] * 100, [0.01] * 100, version="v",
                        candidate_errors=-1)


class _Handle:
    """A stand-in for a Serve handle: ``handle.invoke.remote(*args)`` is awaitable."""

    def __init__(self, answer: Any, fail: bool = False) -> None:
        self.invoke = self
        self._answer, self._fail = answer, fail

    def remote(self, *args: Any) -> Any:
        async def call() -> Any:
            if self._fail:
                raise ValueError("green broke")
            return self._answer
        return call()


def _router(green_fails: bool = False, blue_fails: bool = False) -> Any:
    from sis.serving import CanaryRouter

    router = CanaryRouter.func_or_class(_Handle(1, fail=blue_fails), blue_version="v1")
    router._green = _Handle(1, fail=green_fails)
    router._green_version = "v2"
    router._weight = 0.05
    return router


def test_the_router_records_both_latencies_on_each_shadow_sample() -> None:
    import asyncio

    router = _router()
    asyncio.run(router.route(3))
    (sample,) = router.samples()
    assert sample.baseline_latency is not None and sample.baseline_latency >= 0
    assert sample.candidate_latency is not None and sample.candidate_latency >= 0
    assert sample.candidate_error is None


def test_the_router_records_a_green_failure_as_a_sample() -> None:
    import asyncio

    router = _router(green_fails=True)
    answer = asyncio.run(router.route(3))
    assert answer["result"] == 1  # the caller still gets blue's answer
    (sample,) = router.samples()
    assert sample.candidate_error is not None and "green broke" in sample.candidate_error
    assert router.window("v2", 60.0)[1] == 1


def test_the_router_records_no_sample_when_blue_failed() -> None:
    import asyncio

    router = _router(blue_fails=True)
    asyncio.run(router.route(3))
    assert router.samples() == []
    assert router.window("v1", 60.0)[1] == 1


def test_an_inconclusive_live_canary_is_a_neutral_cycle() -> None:
    from sis import org

    inconclusive = {"canary_passed": False, "inconclusive": True,
                    "reason": "live latency inconclusive: total latency ratio 1.05"}
    assert org.canary_neutral_status(inconclusive) == "inconclusive"
    for counted in ("live latency regression: total latency ratio 1.4",
                    "live error rate: candidate failed 3 of 150 calls",
                    "response disagreement: 1 of 150"):
        assert org.canary_neutral_status({"canary_passed": False, "reason": counted}) is None
    assert org.canary_neutral_status({"canary_passed": True, "reason": "canary passed"}) is None
    assert org.canary_neutral_status(None) is None
