"""CEO brake/spend state: snapshot, rehydrate, and reset (M2/L9, needs Ray).

Its own module so it gets a fresh Ray cluster with clean CEO state — named
detached actors are singletons within a cluster, so a breaker-tripped CEO from
another module would mask these assertions.
"""

import pytest

ray = pytest.importorskip("ray")

from sis import org  # noqa: E402
from sis.roles import CEO  # noqa: E402


@pytest.fixture(scope="module")
def handles():  # type: ignore[no-untyped-def]
    h = org.bootstrap()  # brings up the shared substrate (SelfModel/Workspace)
    yield h
    ray.shutdown()


def test_snapshot_reflects_outcomes(handles) -> None:  # type: ignore[no-untyped-def]
    ceo = handles["CEO"]
    before = ray.get(ceo.state_snapshot.remote())["spent_usd"]
    ray.get(ceo.report_outcome.remote(success=False, cost_usd=0.10))
    snap = ray.get(ceo.state_snapshot.remote())
    assert snap["spent_usd"] == pytest.approx(before + 0.10)
    assert snap["consecutive_failures"] >= 1


def test_reset_breaker_clears_failures_but_not_spend(handles) -> None:  # type: ignore[no-untyped-def]
    # A fresh CEO with a tiny threshold, tripped by repeated failures.
    ceo = CEO.remote(budget_usd=10.0, breaker_threshold=2)
    ray.get(ceo.report_outcome.remote(success=False, cost_usd=0.20))
    ray.get(ceo.report_outcome.remote(success=False, cost_usd=0.20))
    assert ray.get(ceo.breaker_open.remote()) is True

    spent_before = ray.get(ceo.state_snapshot.remote())["spent_usd"]
    assert ray.get(ceo.reset_breaker.remote()) is True
    assert ray.get(ceo.breaker_open.remote()) is False
    snap = ray.get(ceo.state_snapshot.remote())
    assert snap["consecutive_failures"] == 0
    assert snap["spent_usd"] == pytest.approx(spent_before)  # spend is NOT reset (§4.1)


def test_fresh_ceo_rehydrates_persisted_state(handles) -> None:  # type: ignore[no-untyped-def]
    # A restart is a fresh actor built from the persisted snapshot (L9): a
    # previously-tripped breaker and accumulated spend must come back.
    state = {"spent_usd": 3.25, "consecutive_failures": 3, "accepted": 1, "tripped": True}
    ceo = CEO.remote(budget_usd=10.0, breaker_threshold=3, state=state)
    assert ray.get(ceo.breaker_open.remote()) is True
    snap = ray.get(ceo.state_snapshot.remote())
    assert snap["spent_usd"] == pytest.approx(3.25)
    assert snap["accepted"] == 1
    assert ray.get(ceo.economics.remote())["spent_usd"] == pytest.approx(3.25)


def test_an_over_budget_cycle_counts_for_less_than_a_wrong_one(handles) -> None:  # type: ignore[no-untyped-def]
    # OMNI-24: with the default weight 0.5, two correct-but-slow (`slo`) cycles
    # fill the streak as much as one wrong one does.
    ceo = CEO.remote(budget_usd=10.0, breaker_threshold=2)
    ray.get(ceo.report_outcome.remote(success=False, reject_gate="slo"))
    ray.get(ceo.report_outcome.remote(success=False, reject_gate="slo"))
    assert ray.get(ceo.state_snapshot.remote())["consecutive_failures"] == pytest.approx(1.0)
    assert ray.get(ceo.breaker_open.remote()) is False
    ray.get(ceo.report_outcome.remote(success=False, reject_gate="correctness"))
    assert ray.get(ceo.breaker_open.remote()) is True


def test_a_pre_omni_24_integer_streak_still_rehydrates(handles) -> None:  # type: ignore[no-untyped-def]
    state = {"spent_usd": 0.0, "consecutive_failures": 1, "accepted": 0, "tripped": False}
    ceo = CEO.remote(budget_usd=10.0, breaker_threshold=3, state=state)
    ray.get(ceo.report_outcome.remote(success=False, reject_gate="slo"))
    assert ray.get(ceo.state_snapshot.remote())["consecutive_failures"] == pytest.approx(1.5)


# --- OMNI-61: fail closed, pause, and the operator's CLI -------------------------


def test_a_ceo_booted_from_unreadable_state_holds_the_breaker(handles) -> None:  # type: ignore[no-untyped-def]
    from sis.roles import unreadable_brake_state

    ceo = CEO.remote(budget_usd=10.0, breaker_threshold=3,
                     state=unreadable_brake_state("episodic_state.json: truncated"))
    assert ray.get(ceo.breaker_open.remote()) is True
    assert ray.get(ceo.approve_budget.remote(0.01)) is False
    snap = ray.get(ceo.state_snapshot.remote())
    assert "unreadable" in snap["trip_reason"]
    # Only a deliberate reset clears it, and the reason goes with it.
    ray.get(ceo.reset_breaker.remote())
    assert ray.get(ceo.state_snapshot.remote())["trip_reason"] is None


def test_a_trip_records_which_brake_tripped(handles) -> None:  # type: ignore[no-untyped-def]
    ceo = CEO.remote(budget_usd=10.0, breaker_threshold=1)
    ray.get(ceo.report_outcome.remote(success=False))
    assert ray.get(ceo.state_snapshot.remote())["trip_reason"] == "consecutive failure threshold"


def test_a_pause_refuses_cycles_without_touching_the_brakes(handles) -> None:  # type: ignore[no-untyped-def]
    ceo = handles["CEO"]
    before = ray.get(ceo.state_snapshot.remote())
    ray.get(ceo.pause.remote("maintenance window for the test"))
    try:
        result = org.run_cycle(handles, "t", "b")
        assert result["status"] == "paused"
        assert result["pause_reason"] == "maintenance window for the test"
        after = ray.get(ceo.state_snapshot.remote())
        for key in ("spent_usd", "consecutive_failures", "accepted", "tripped"):
            assert after[key] == before[key], key
    finally:
        ray.get(ceo.resume.remote())
    assert ray.get(ceo.pause_reason.remote()) is None


def test_the_admin_cli_acts_on_the_live_ceo_and_audits_it(  # type: ignore[no-untyped-def]
    handles, tmp_path, monkeypatch
) -> None:
    import json

    from sis import admin

    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(admin, "OPERATOR_AUDIT_JSONL", audit)
    reason = "pausing to check the CLI end to end"
    assert admin.main(["pause", "--reason", reason]) == 0
    assert ray.get(handles["CEO"].pause_reason.remote()) == reason
    assert admin.main(["resume", "--reason", "resuming after the CLI check"]) == 0
    assert ray.get(handles["CEO"].pause_reason.remote()) is None
    # A refused action changes nothing and is not audited.
    assert admin.main(["pause", "--reason", "short"]) == 1

    entries = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert [e["path"] for e in entries] == ["ceo.pause", "ceo.resume"]
    assert entries[0]["justification"] == reason
    assert entries[0]["before"]["paused"] is None and entries[0]["after"]["paused"] == reason
