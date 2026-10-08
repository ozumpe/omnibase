"""The benchmark gate's verdict rule (OMNI-41), tested without timing anything.

`benchmark_decision` is pure so the part that used to be flaky — deciding from
noisy measurements — can be pinned with fixed numbers. The sandbox half (fresh
inputs, interleaved pairs, an output channel the candidate cannot write to) is
covered by the adversarial corpus.
"""

import math
import random
import statistics

from sis import gauntlet
from sis.episodic import gate_from_reason, neutral_status
from sis.gauntlet import (
    BenchmarkVerdict,
    _timed_work,
    _TimedWork,
    _UnusedInputs,
    benchmark_cpus,
    benchmark_decision,
)
from sis.sandbox_worker import WorkerError

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


# --- OMNI-152 (KNOWN_ISSUES H7): no input is given out twice -----------------


def _one_of(top: int) -> _UnusedInputs:
    return _UnusedInputs(lambda rng: [rng.randint(1, top)])


def test_no_input_is_given_out_twice_across_takes() -> None:
    unused = _one_of(10_000)
    rng = random.Random(7)
    given = [args[0] for _ in range(20) for args in unused.take(rng, 300)]
    assert len(given) == 6_000
    assert len(set(given)) == len(given)


def test_a_range_that_runs_short_gives_fewer_inputs_not_repeats() -> None:
    # 50 values and a request for 1000: the old gate drew 1000, 950 of them
    # repeats, which is what a cache was paid for.
    unused = _one_of(50)
    given = [args[0] for args in unused.take(random.Random(7), 1_000)]
    assert sorted(given) == list(range(1, 51))
    assert unused.take(random.Random(8), 1_000) == []


def test_the_last_unused_inputs_of_a_small_range_are_found() -> None:
    # 300 values, 200 of them met: 99 are asked for and 100 are left. Giving up
    # after a fixed 100 repeats in a row lost this about every other time, and
    # the benchmark then reported a range it could have timed as too small.
    for seed in range(20):
        unused = _one_of(300)
        for met in range(1, 201):
            unused.use([met])
        assert len(unused.take(random.Random(seed), 99)) == 99, seed


def test_an_input_already_used_elsewhere_is_never_given_out() -> None:
    # What the candidate met in the differential phase is not timed later.
    unused = _one_of(50)
    for met in range(1, 41):
        assert unused.use([met])
    assert not unused.use([40])
    given = {args[0] for args in unused.take(random.Random(7), 1_000)}
    assert given and given <= set(range(41, 51))


def test_inputs_are_compared_at_least_as_coarsely_as_a_cache_could_key_them() -> None:
    # A dict keys 1, 1.0 and True alike, so a cache built on one answers all
    # three from one entry. (functools.cache keeps a lone int apart from 1.0;
    # being stricter than that costs nothing.)
    unused = _UnusedInputs(lambda rng: [0])
    assert unused.use([1])
    assert not unused.use([1.0])
    assert not unused.use([True])
    assert unused.use([[3, 1, 2], {"k": [1]}])
    assert not unused.use([[3, 1, 2], {"k": [1]}])
    assert unused.use([[1, 2, 3], {"k": [1]}])


# --- OMNI-152: the timed batches, sized by time and filled with unused inputs --
#
# _timed_work with a scripted baseline, so the sizing loop is pinned without a
# worker or a clock: the baseline "takes" per_call seconds for each input.


def _work(
    top: int, *, samples: int = 99, min_batch: int = 1, per_call: float = 1.0,
    window: float = 64.0, met: range = range(0),
) -> tuple[_TimedWork, list[list[int]]]:
    unused = _one_of(top)
    for n in met:
        unused.use([n])
    probes: list[list[int]] = []

    def time_baseline(probe: list[list[int]]) -> float:
        probes.append([args[0] for args in probe])
        return per_call * len(probe)

    work = _timed_work(unused, random.Random(11), samples=samples, min_batch=min_batch,
                       window=window, time_baseline=time_baseline)
    return work, probes


def _timed(work: _TimedWork) -> list[int]:
    return [args[0] for batch in work.batches for args in batch]


def test_the_batch_grows_to_the_window_and_no_input_is_used_twice() -> None:
    work, probes = _work(10**9)
    assert [len(probe) for probe in probes] == [1, 2, 4, 8, 16, 32, 64]
    assert (work.batch, work.capped) == (64, False)
    assert [len(batch) for batch in work.batches] == [64] * 99
    timed = _timed(work)
    assert len(set(timed)) == len(timed)
    assert set(timed).isdisjoint(n for probe in probes for n in probe)


def test_a_small_range_caps_the_batch_while_it_is_growing() -> None:
    # 2,000 values and a baseline so fast the window is never reached: the
    # batch stops where the inputs do. Doubling by drawing with replacement,
    # which is H7 itself, fills 99 batches of 32 here, most of them repeats.
    work, probes = _work(2_000, per_call=0.0)
    assert work.capped
    assert work.batch == work.found // 99
    assert 16 <= work.batch <= 19
    timed = _timed(work)
    assert len(timed) == 99 * work.batch
    assert len(set(timed)) == len(timed)
    assert set(timed).isdisjoint(n for probe in probes for n in probe)


def test_the_timed_inputs_are_set_aside_before_sizing_can_use_them_up() -> None:
    # 250 values. Set aside first, they fill 99 batches of 2. Sizing first
    # would spend 127 of them on sizing exchanges and leave batches of 1.
    work, _ = _work(250, per_call=0.0)
    assert (work.batch, work.capped) == (2, True)


def test_a_minimum_batch_the_range_cannot_fill_is_cut_down_without_sizing() -> None:
    work, probes = _work(2_000, min_batch=1024)
    assert probes == []
    assert work.capped
    assert work.batch == 2_000 // 99
    assert len(set(_timed(work))) == 99 * work.batch


def test_a_range_that_gives_out_while_sizing_keeps_the_batch_it_has() -> None:
    # Exactly one input per sample, and none left for the first sizing exchange.
    work, probes = _work(99)
    assert probes == []
    assert (work.batch, work.capped) == (1, True)
    assert sorted(_timed(work)) == list(range(1, 100))


def test_a_range_smaller_than_the_samples_gives_nothing_to_time() -> None:
    work, probes = _work(60)
    assert work.batches == [] and work.batch == 0 and probes == []
    assert work.capped and work.found == 60


def test_inputs_met_before_the_benchmark_are_never_timed() -> None:
    work, _ = _work(2_000, per_call=0.0, met=range(1, 1_501))
    timed = _timed(work)
    assert timed and min(timed) > 1_500


# --- OMNI-154 (KNOWN_ISSUES H9): one CPU for each benchmark worker ---------------


def _cores(*groups: tuple[int, ...]) -> dict[int, frozenset[int]]:
    return {cpu: frozenset(group) for group in groups for cpu in group}


def test_the_two_workers_get_different_cpus() -> None:
    # Where the host does not know its hyperthreads: the highest and the lowest.
    assert benchmark_cpus(range(4), {}) == (3, 0)
    assert benchmark_cpus(range(12), {}) == (11, 0)
    assert benchmark_cpus([2, 3], {}) == (3, 2)


def test_the_two_cpus_are_on_different_cores_where_the_host_can_tell() -> None:
    # The two usual numberings of two cores with two threads each.
    assert benchmark_cpus(range(4), _cores((0, 2), (1, 3))) == (3, 2)
    assert benchmark_cpus(range(4), _cores((0, 1), (2, 3))) == (3, 1)
    # One core, two threads: there is no other core to go to.
    assert benchmark_cpus(range(2), _cores((0, 1))) == (1, 0)


def test_a_single_cpu_is_shared_and_none_is_an_error() -> None:
    assert benchmark_cpus([5], {}) == (5, 5)
    try:
        benchmark_cpus([], {})
    except ValueError:
        pass
    else:
        raise AssertionError("no CPU must not pass for a confinement")


class _FakeWorker:
    def __init__(self, cpu: int | None, *, fails: bool = False) -> None:
        self.cpu, self.fails = cpu, fails
        self.log: list[object] = []

    def confine(self, cpu: int) -> None:
        if self.fails:
            raise WorkerError("docker update failed: no such container")
        self.log.append(("confine", cpu))
        self.cpu = cpu

    def call(self, calls: list[list[object]], *, timeout_s: float) -> object:
        self.log.append(("call", list(calls)))
        return object()


def test_halfway_each_worker_moves_to_the_others_cpu_before_the_clock_runs_again() -> None:
    cand, base = _FakeWorker(3), _FakeWorker(0)
    gauntlet._swap_cpus(cand, base, deadline=float("inf"))
    assert (cand.cpu, base.cpu) == (0, 3)
    # One untimed, empty exchange each after the move.
    assert cand.log == [("confine", 0), ("call", [])]
    assert base.log == [("confine", 3), ("call", [])]


def test_workers_that_are_not_confined_or_share_a_cpu_do_not_swap() -> None:
    for cand, base in ((_FakeWorker(None), _FakeWorker(None)), (_FakeWorker(5), _FakeWorker(5))):
        gauntlet._swap_cpus(cand, base, deadline=float("inf"))
        assert cand.log == [] and base.log == []


def test_a_swap_that_fails_is_the_harnesss_fault() -> None:
    try:
        gauntlet._swap_cpus(_FakeWorker(3, fails=True), _FakeWorker(0), deadline=float("inf"))
    except gauntlet._WorkerFailed as failed:
        assert failed.result.reason.startswith("harness: the benchmark's workers could not swap")
        assert gate_from_reason(failed.result.reason) == "harness"
    else:
        raise AssertionError("a failed swap went unreported")

