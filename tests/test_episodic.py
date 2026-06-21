"""Tests for the episodic store (port + backends). No Ray, no network."""

import pytest

from sis.episodic import (
    DuckDBEpisodicStore,
    EpisodicEvent,
    JsonlEpisodicStore,
    NullEpisodicStore,
    event_from_cycle_result,
    gate_from_reason,
    get_episodic_store,
    summarize,
)


def _ev(outcome: str, **kw: object) -> EpisodicEvent:
    return EpisodicEvent(cycle_id=EpisodicEvent.new_cycle_id(), ts="2026-01-01T00:00:00",
                         outcome=outcome, **kw)  # type: ignore[arg-type]


def test_jsonl_round_trip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = JsonlEpisodicStore(tmp_path / "ep.jsonl")
    store.append(_ev("verified_awaiting_human_merge", cost_usd=0.0, baseline_latency=0.1,
                      candidate_latency=0.01, improvement_pct=90.0))
    store.append(_ev("rolled_back", reject_gate="mypy", reject_reason="mypy --strict failed"))
    events = store.events()
    assert [e.outcome for e in events] == ["verified_awaiting_human_merge", "rolled_back"]
    assert events[1].reject_gate == "mypy"


def test_jsonl_skips_malformed_and_legacy_lines(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "ep.jsonl"
    p.write_text('{"not":"an event"}\nnot json\n', encoding="utf-8")
    store = JsonlEpisodicStore(p)
    # legacy/extra keys ignored; non-JSON skipped — must not crash.
    assert all(isinstance(e, EpisodicEvent) for e in store.events())


def test_summary_rollups() -> None:
    events = [
        _ev("verified_awaiting_human_merge", cost_usd=2.0),
        _ev("verified_awaiting_human_merge", cost_usd=2.0),
        _ev("rolled_back", reject_gate="benchmark"),
        _ev("rolled_back", reject_gate="mypy"),
    ]
    s = summarize(events)
    assert s["total"] == 4
    assert s["accepted"] == 2
    assert s["by_outcome"]["rolled_back"] == 2
    assert s["rejected_by_gate"] == {"benchmark": 1, "mypy": 1}
    assert s["total_cost_usd"] == 4.0
    assert s["cost_per_accepted_usd"] == 2.0


def test_null_store_is_noop() -> None:
    store = NullEpisodicStore()
    store.append(_ev("rolled_back"))
    assert store.events() == []
    assert store.summary()["total"] == 0


def test_factory_selects_backend(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SIS_EPISODIC_STORE", "none")
    assert isinstance(get_episodic_store(), NullEpisodicStore)
    monkeypatch.setenv("SIS_EPISODIC_STORE", "jsonl")
    assert isinstance(get_episodic_store(), JsonlEpisodicStore)
    assert isinstance(get_episodic_store("none"), NullEpisodicStore)  # explicit arg wins


def test_gate_from_reason() -> None:
    assert gate_from_reason("mypy --strict failed") == "mypy"
    assert gate_from_reason("pytest failed") == "pytest"
    assert gate_from_reason("correctness mismatch (...)") == "correctness"
    assert gate_from_reason("no improvement: ...") == "benchmark"
    assert gate_from_reason("Policy blocked: ...") == "policy"
    assert gate_from_reason("gauntlet sandbox timed out after 2s") == "timeout"
    assert gate_from_reason(None) is None


def test_event_from_cycle_result_maps_fields() -> None:
    ev = event_from_cycle_result(
        {"status": "verified_awaiting_human_merge", "spec_id": "PAGE-2",
         "story_id": "STORY-3", "pr_id": "PR-2",
         "baseline_latency": 0.0004, "candidate_latency": 0.00002},
        cost_usd=0.0, proposer="stub")
    assert ev.outcome == "verified_awaiting_human_merge"
    assert ev.gauntlet_passed is True
    assert ev.improvement_pct == 95.0  # (1 - 0.00002/0.0004) * 100

    rej = event_from_cycle_result(
        {"status": "rolled_back", "reason": "mypy --strict failed"}, cost_usd=0.0)
    assert rej.gauntlet_passed is False
    assert rej.reject_gate == "mypy"


def test_duckdb_backend_parity(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("duckdb")  # only runs when the analytics extra is installed
    store = DuckDBEpisodicStore(tmp_path / "ep.duckdb")
    store.append(_ev("verified_awaiting_human_merge", cost_usd=2.0))
    store.append(_ev("rolled_back", reject_gate="benchmark"))
    assert [e.outcome for e in store.events()] == [
        "verified_awaiting_human_merge", "rolled_back"]
    # Same rollup shape/values as the JSONL/pure path.
    assert store.summary()["accepted"] == 1
    assert store.summary()["rejected_by_gate"] == {"benchmark": 1}
    # Ad-hoc SQL works.
    assert store.sql("SELECT count(*) FROM episodes")[0][0] == 2
