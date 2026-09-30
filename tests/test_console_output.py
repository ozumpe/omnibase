"""OMNI-123: the console says why — per cycle, and when the loop stops. Pure, no Ray.

The cases are the first AWS run's own (OMNI-29, 2026-09-27): its console said
`cycle status: rolled_back` and `loop stopped after 2 cycle(s)`, and the rest
had to be read out of the episodic log afterwards.
"""

from __future__ import annotations

from typing import Any

from sis import org
from sis.loop import Tick, stop_summary
from sis.org import cycle_summary

_ECON = {"spent_usd": 0.040155, "budget_usd": 1.0}
_RUN_REASON = ("no improvement: candidate ~0.000001s vs baseline 0.000001s per call "
               "(need ≤ 90%); total-time ratio 1.3384, 95% interval [1.2271, 1.4743] over "
               "109 paired samples; seed=2064960016")


def _summary(**result: Any) -> str:
    return cycle_summary(result)


def test_a_rejected_cycle_names_its_gate_its_reason_and_its_cost() -> None:
    line = _summary(status="rolled_back", reason=_RUN_REASON,
                    cost_usd=0.040155, economics=_ECON)
    assert line.startswith("[cycle] rolled_back: benchmark: no improvement")
    assert "ratio 1.3384, 95% interval [1.2271, 1.4743]" in line
    assert line.endswith("(cost $0.0402; spent $0.0402 of $1.00)")


def test_a_reason_is_kept_to_one_line() -> None:
    line = _summary(status="rolled_back", reason="mypy --strict failed\n  error: x\n  error: y")
    assert "\n" not in line and "mypy: mypy --strict failed error: x error: y" in line


def test_a_cycle_without_a_reason_says_so() -> None:
    # Say so rather than print nothing after the colon.
    assert _summary(status="qa_rejected") == "[cycle] qa_rejected: no reason recorded"


def test_a_qa_rejection_keeps_its_reason_and_gate() -> None:
    # OMNI-56 (M17): QA's re-run of the gauntlet used to file "QA rejected" with
    # no reason, and the CEO never saw which gate it was.
    gate, summary = org.rejection_bug("STORY-2", "invariant violated in sandbox (seed=4): x",
                                      pr_id="PR-7")
    assert gate == "invariant"
    assert summary == "QA rejected STORY-2 (PR PR-7): invariant violated in sandbox (seed=4): x"
    line = _summary(status="qa_rejected", reason="invariant violated in sandbox (seed=4): x")
    assert line == "[cycle] qa_rejected: invariant: invariant violated in sandbox (seed=4): x"


def test_a_harness_fault_at_qa_is_filed_as_one() -> None:
    gate, summary = org.rejection_bug("STORY-2", "harness: the acceptance gate raised (OSError)",
                                      pr_id="PR-7")
    assert gate == "harness"
    assert summary.startswith("Infrastructure fault (sandbox) during QA of STORY-2 (PR PR-7)")
    # The SWE stage's wording is unchanged.
    assert org.rejection_bug("STORY-2", "harness: x")[1].startswith(
        "Infrastructure fault (sandbox) during STORY-2 — the candidate was not judged")
    assert org.rejection_bug("STORY-2", "mypy --strict failed") == (
        "mypy", "Cycle failed for STORY-2: mypy --strict failed")


def test_an_accepted_cycle_names_its_pr_and_the_speedup() -> None:
    line = _summary(status="verified_awaiting_human_merge", pr_id="12",
                    baseline_latency=0.0012, candidate_latency=0.0003)
    assert "PR 12 awaits a human merge (baseline 0.001200s -> candidate 0.000300s)" in line


def test_cycles_that_never_ran_say_why() -> None:
    assert "paused by an operator: maintenance" in _summary(
        status="paused", pause_reason="maintenance")
    assert "circuit breaker open, no cycle ran" in _summary(status="circuit_breaker_open")
    assert "spend cap reached" in _summary(status="budget_denied")


def _stop(tick: Tick | None, cycles: int, **kw: Any) -> str:
    defaults: dict[str, Any] = {"max_cycles": 3, "interrupted": False, "trip_reason": None,
                                "spent_usd": 0.10704, "budget_usd": 1.0}
    return stop_summary(tick, cycles, **{**defaults, **kw})


def test_the_first_runs_stop_would_now_say_the_breaker_tripped() -> None:
    line = _stop(Tick(breaker_open=True, budget_ok=True, work=None), 2,
                 trip_reason="consecutive failure threshold")
    assert line == ("[loop] stopped after 2 cycle(s): circuit breaker open "
                    "(consecutive failure threshold); spent $0.1070 of $1.00")


def test_every_other_way_the_loop_ends_is_named() -> None:
    assert "spend budget exhausted" in _stop(
        Tick(breaker_open=False, budget_ok=False, work=None), 5)
    assert "interrupted" in _stop(None, 1, interrupted=True)
    assert "reached loop.max_cycles (3)" in _stop(None, 3)
    assert "no further work" in _stop(None, 1)


def test_an_already_built_feature_names_its_gate() -> None:
    # OMNI-147's "already built" printed as "gate unknown" in v0.3.5.
    line = _summary(status="no_gain",
                    reason="already built: runtime/roman.py passes every gate")
    assert line == ("[cycle] no_gain: built: already built: "
                    "runtime/roman.py passes every gate")
