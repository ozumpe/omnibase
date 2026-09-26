"""The benchmark gate's verdict rule (OMNI-41), tested without timing anything.

`benchmark_decision` is pure so the part that used to be flaky — deciding from
noisy measurements — can be pinned with fixed numbers. The sandbox half (fresh
inputs, interleaved pairs, an output channel the candidate cannot write to) is
covered by the adversarial corpus.
"""

import math
import statistics

from sis.episodic import gate_from_reason, neutral_status
from sis.gauntlet import BenchmarkVerdict, benchmark_decision

MARGIN = 0.90


def _pairs(cand: list[float], base: list[float]) -> list[tuple[float, float]]:
    return list(zip(cand, base, strict=True))


def test_an_identical_candidate_is_rejected_even_when_one_pair_looks_faster() -> None:
    # The OMNI-41 flake: one noisy comparison made an identical candidate look
    # >=10% faster. A single outlying pair must not decide the cycle.
    base = [1.0] * 40
    cand = [0.7] + [1.0] * 39
    decision = benchmark_decision(_pairs(cand, base), max_ratio=MARGIN)
    assert decision.verdict is BenchmarkVerdict.REJECT
    assert decision.ratio > MARGIN


def test_a_clear_win_is_accepted() -> None:
    base = [1.0 + 0.1 * (i % 5) for i in range(40)]
    cand = [b * 0.005 for b in base]
    decision = benchmark_decision(_pairs(cand, base), max_ratio=MARGIN)
    assert decision.verdict is BenchmarkVerdict.ACCEPT
    assert decision.ci_high <= MARGIN


def test_fast_on_typical_inputs_but_slower_in_total_is_rejected() -> None:
    # The hole a pre-merge review found in the first cut, which decided on the
    # MEDIAN per-pair ratio: 10x faster on the 70% of cheap inputs, 4x slower on
    # the 30% of expensive ones. Typical input: fast. Total cost: 2.3x slower.
    base = [1.0] * 70 + [3.0] * 30
    cand = [0.1] * 70 + [12.0] * 30
    pairs = _pairs(cand, base)
    assert statistics.median(c / b for c, b in pairs) <= MARGIN  # the old rule's view
    decision = benchmark_decision(pairs, max_ratio=MARGIN)
    assert decision.verdict is BenchmarkVerdict.REJECT
    assert decision.ratio > 2.0


def test_looks_faster_but_unproven_is_inconclusive() -> None:
    # Best estimate 0.85 — past the margin — but so noisy the interval reaches
    # above it. The evidence leans toward the candidate without proving it.
    base = [1.0] * 20
    cand = [0.4, 1.3] * 10
    decision = benchmark_decision(_pairs(cand, base), max_ratio=MARGIN)
    assert decision.ratio <= MARGIN < decision.ci_high
    assert decision.verdict is BenchmarkVerdict.INCONCLUSIVE


def test_a_slower_candidate_cannot_hide_in_inconclusive_by_being_noisy() -> None:
    # Inconclusive is neutral (no bug, no breaker), so it must be unreachable
    # for a candidate whose best estimate misses the margin — however wide its
    # interval. A reviewer steered the first cut there with a bimodal candidate.
    base = [1.0] * 20
    cand = [0.1, 2.1] * 10
    decision = benchmark_decision(_pairs(cand, base), max_ratio=MARGIN)
    assert decision.ci_low < MARGIN  # the interval straddles...
    assert decision.verdict is BenchmarkVerdict.REJECT  # ...and it is still rejected


def test_too_few_usable_pairs_is_unmeasurable_not_inconclusive() -> None:
    # Not neutral: a candidate sharing the harness's process can zero or NaN
    # its own timings on purpose, so "could not measure" must not be a place to
    # hide the way INCONCLUSIVE (neutral) would be.
    decision = benchmark_decision([(0.1, 1.0)] * 9, max_ratio=MARGIN)
    assert decision.verdict is BenchmarkVerdict.UNMEASURABLE


def test_unmeasurable_pairs_are_ignored_rather_than_trusted() -> None:
    # Zero, negative or non-finite timings are clock artifacts, not speedups.
    junk = [(0.0, 1.0), (1.0, 0.0), (-1.0, 1.0), (math.inf, 1.0), (math.nan, 1.0)]
    decision = benchmark_decision(junk + [(1.0, 1.0)] * 20, max_ratio=MARGIN)
    assert decision.samples == 20
    assert decision.verdict is BenchmarkVerdict.REJECT


def test_the_decision_is_deterministic() -> None:
    # Pure: the bootstrap is seeded, so the same measurements always give the
    # same verdict and the same interval.
    pairs = _pairs([0.4, 1.3] * 10, [1.0] * 20)
    first = benchmark_decision(pairs, max_ratio=MARGIN)
    assert benchmark_decision(pairs, max_ratio=MARGIN) == first


def test_inconclusive_is_logged_as_its_own_gate_and_is_neutral() -> None:
    # "Could not confirm a difference" must not be counted as "not faster" in
    # rejected_by_gate, and both the SWE's run and QA's re-run must route it the
    # same way (one definition: episodic.neutral_status).
    reason = "benchmark inconclusive: candidate ~0.000251s vs baseline 0.000282s per call"
    assert gate_from_reason(reason) == "benchmark_inconclusive"
    assert neutral_status(reason) == "inconclusive"
    assert neutral_status("no change: candidate is identical to the baseline") == "no_change"
    assert gate_from_reason("no improvement: candidate 1.0s vs baseline 1.0s") == "benchmark"
    assert neutral_status("no improvement: candidate 1.0s vs baseline 1.0s") is None
    assert neutral_status(None) is None


def test_measurement_failures_are_named_and_never_neutral() -> None:
    for reason, gate in (
        ("benchmark unmeasurable: only 3 usable timing pairs of 109", "benchmark_unmeasurable"),
        ("benchmark output malformed or missing — expected PAIRS", "benchmark_malformed"),
    ):
        assert gate_from_reason(reason) == gate
        assert neutral_status(reason) is None
