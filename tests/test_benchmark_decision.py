"""The benchmark gate's verdict rule (OMNI-41), tested without timing anything.

`benchmark_decision` is pure so the part that used to be flaky — deciding from
noisy measurements — can be pinned with fixed numbers. The sandbox half (fresh
inputs, interleaved pairs) is covered by the adversarial corpus.
"""

import math

from sis.episodic import gate_from_reason
from sis.gauntlet import BenchmarkVerdict, _median_interval_rank, benchmark_decision

MARGIN = 0.90


def test_an_identical_candidate_is_rejected_even_when_one_pair_looks_faster() -> None:
    # The OMNI-41 failure mode: one noisy comparison made an identical candidate
    # look >=10% faster. One outlying pair must not decide the cycle any more.
    ratios = [0.72, 0.99, 1.00, 1.00, 1.01, 1.01, 1.02, 1.03, 1.05]
    decision = benchmark_decision(ratios, max_ratio=MARGIN)
    assert decision.verdict is BenchmarkVerdict.REJECT
    assert decision.ci_low > MARGIN


def test_a_clear_win_is_accepted() -> None:
    ratios = [0.004, 0.005, 0.005, 0.005, 0.006, 0.006, 0.006, 0.007, 0.008]
    assert benchmark_decision(ratios, max_ratio=MARGIN).verdict is BenchmarkVerdict.ACCEPT


def test_a_candidate_straddling_the_margin_is_inconclusive_not_rejected() -> None:
    ratios = [0.84, 0.86, 0.88, 0.89, 0.90, 0.91, 0.92, 0.94, 0.96]
    decision = benchmark_decision(ratios, max_ratio=MARGIN)
    assert decision.verdict is BenchmarkVerdict.INCONCLUSIVE
    assert decision.ci_low <= MARGIN < decision.ci_high


def test_too_few_usable_samples_cannot_decide() -> None:
    assert benchmark_decision([0.5, 0.5], max_ratio=MARGIN).verdict is BenchmarkVerdict.INCONCLUSIVE


def test_unmeasurable_samples_are_ignored_rather_than_trusted() -> None:
    # A zero, negative, or non-finite ratio is a clock artifact, not a speedup.
    junk = [0.0, -1.0, math.inf, math.nan]
    decision = benchmark_decision(junk + [1.0] * 9, max_ratio=MARGIN)
    assert decision.rounds == 9
    assert decision.verdict is BenchmarkVerdict.REJECT


def test_the_interval_really_covers_what_it_claims() -> None:
    # Exact binomial coverage: at the default 99 samples the interval must meet
    # 95%, and more samples must buy tolerance to outliers, not just width.
    k9, cov9 = _median_interval_rank(9, 0.95)
    k99, cov99 = _median_interval_rank(99, 0.95)
    assert cov9 >= 0.95 and cov99 >= 0.95
    assert k99 > k9 > 1


def test_inconclusive_is_logged_as_its_own_gate_not_as_no_improvement() -> None:
    # "Could not measure a difference" must not be counted as "not faster" in
    # rejected_by_gate — that conflation is what hid a noisy gate behind a
    # flaky test.
    reason = "benchmark inconclusive: candidate 0.000251s vs baseline 0.000282s per call"
    assert gate_from_reason(reason) == "benchmark_inconclusive"
    assert gate_from_reason("no improvement: candidate 1.0s vs baseline 1.0s") == "benchmark"
