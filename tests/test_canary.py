"""Tests for the online canary verdict — pure, no Ray/Serve/network/clock."""

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
