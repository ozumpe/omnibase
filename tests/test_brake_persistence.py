"""OMNI-157 (KNOWN_ISSUES H12): a failed brake-state save stops the loop.

Before, ``run_cycle`` warned when the CEO's state could not be saved and went
on. A restart then restored the last state that had been saved, and every
dollar charged since was forgotten: the reviewer's disk-full probe left $0.70
live and $0.10 on disk. Now a cycle's spend is reserved, and the reservation
saved, before anything is spent; a save that fails opens the breaker.

Its own module, so it gets its own Ray cluster and its own CEO (needs Ray).
"""

from pathlib import Path
from typing import Any

import pytest

ray = pytest.importorskip("ray")

from sis import episodic, org  # noqa: E402
from sis.roles import CEO  # noqa: E402

ESTIMATE = 0.5


class _DiskFull(episodic.JsonlEpisodicStore):
    """Saves the CEO's state *saves* times, then fails like a full disk."""

    def __init__(self, path: Path, saves: int) -> None:
        super().__init__(path)
        self.saves = saves

    def save_state(self, key: str, value: dict[str, Any]) -> bool:
        if key == "ceo":
            if self.saves <= 0:
                raise OSError(28, "No space left on device")
            self.saves -= 1
        return super().save_state(key, value)


@pytest.fixture(scope="module")
def handles():  # type: ignore[no-untyped-def]
    h = org.bootstrap()
    ray.get(h["SWE"].__ray_call__.remote(_proposals_cost_money))
    yield h
    ray.shutdown()


@pytest.fixture(autouse=True)
def _a_closed_breaker(handles: dict[str, Any]) -> None:
    ray.get(handles["CEO"].reset_breaker.remote())


def _proposals_cost_money(_swe: Any) -> None:
    """Inside the SWE's process: the stub's proposal costs $0.05, and is counted."""
    from sis import proposer

    real, calls = proposer.propose, []

    def paid(*args: Any, **kwargs: Any) -> str:
        calls.append(1)
        candidate = real(*args, **kwargs)
        proposer._last_cost_usd = 0.05
        return candidate

    proposer.propose = paid  # type: ignore[assignment]
    proposer._calls = calls  # type: ignore[attr-defined]


def _proposals_made(_swe: Any) -> int:
    from sis import proposer

    return len(proposer._calls)  # type: ignore[attr-defined]


def _store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, saves: int) -> _DiskFull:
    store = _DiskFull(tmp_path / "episodic.jsonl", saves)
    monkeypatch.setattr(episodic, "get_episodic_store", lambda: store)
    return store


def _pages(handles: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in ray.get(handles["Workspace"].events.remote())
            if e["event"] == "notify.sent"
            and str(e["title"]).startswith("circuit breaker open: brake state not persisted")]


def test_a_reservation_that_cannot_be_saved_stops_the_cycle_before_any_spend(
    handles: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _store(monkeypatch, tmp_path, saves=0)
    ceo = handles["CEO"]
    spent = ray.get(ceo.state_snapshot.remote())["spent_usd"]
    proposals, pages = (ray.get(handles["SWE"].__ray_call__.remote(_proposals_made)),
                        len(_pages(handles)))

    result = org.run_cycle(handles, "speed it up", "make it fast", estimate_usd=ESTIMATE)

    assert result["status"] == "circuit_breaker_open"
    assert "No space left" in result["brakes_unpersisted"]
    assert "NOT saved" in org.cycle_summary(result)
    # Nothing was asked of the proposer, so nothing was spent.
    assert ray.get(handles["SWE"].__ray_call__.remote(_proposals_made)) == proposals
    state = ray.get(ceo.state_snapshot.remote())
    assert (state["spent_usd"], state["reserved_usd"]) == (pytest.approx(spent), 0.0)
    # The loop stops, and a human hears why.
    assert ray.get(ceo.breaker_open.remote()) is True
    assert "brake state not persisted" in state["trip_reason"]
    assert len(_pages(handles)) == pages + 1
    assert result["breaker_bug_id"]


def test_a_save_that_fails_after_the_cycle_opens_the_breaker_and_a_restart_keeps_the_spend(
    handles: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    store = _store(monkeypatch, tmp_path, saves=1)   # the reservation is saved, then no more
    ceo = handles["CEO"]
    pages = len(_pages(handles))

    result = org.run_cycle(handles, "speed it up", "make it fast", estimate_usd=ESTIMATE)

    # The cycle's own outcome stands, and says its state was not saved.
    assert result["status"] not in ("error", "circuit_breaker_open"), result
    assert result["cost_usd"] == pytest.approx(0.05)
    assert "No space left" in result["brakes_unpersisted"]
    assert ray.get(ceo.breaker_open.remote()) is True
    assert len(_pages(handles)) == pages + 1
    live = ray.get(ceo.state_snapshot.remote())["spent_usd"]

    # On disk: the state before the cycle, with its reservation. A restart from
    # it counts the reservation as spent, so it never sees less than was spent.
    saved = store.load_state("ceo")
    assert saved is not None and saved["reserved_usd"] == ESTIMATE
    assert saved["spent_usd"] == pytest.approx(live - 0.05)
    restarted = CEO.remote(budget_usd=10.0, state=saved)
    assert ray.get(restarted.state_snapshot.remote())["spent_usd"] >= live

    # And nothing more is spent: the next cycle does not run.
    proposals = ray.get(handles["SWE"].__ray_call__.remote(_proposals_made))
    follow_up = org.run_cycle(handles, "speed it up", "make it fast", estimate_usd=ESTIMATE)
    assert follow_up["status"] == "circuit_breaker_open"
    assert ray.get(handles["SWE"].__ray_call__.remote(_proposals_made)) == proposals


def test_a_cycle_that_saves_leaves_no_reservation_behind(
    handles: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    store = _store(monkeypatch, tmp_path, saves=100)
    result = org.run_cycle(handles, "speed it up", "make it fast", estimate_usd=ESTIMATE)
    assert "brakes_unpersisted" not in result
    saved = store.load_state("ceo")
    assert saved is not None and saved["reserved_usd"] == 0.0
    assert ray.get(handles["CEO"].breaker_open.remote()) is False


def test_a_ceo_started_from_a_reservation_counts_it_as_spent(handles: dict[str, Any]) -> None:
    ceo = CEO.remote(budget_usd=10.0, state={
        "spent_usd": 0.10, "consecutive_failures": 0, "accepted": 0, "tripped": False,
        "reserved_usd": 0.60})
    state = ray.get(ceo.state_snapshot.remote())
    assert (state["spent_usd"], state["reserved_usd"]) == (pytest.approx(0.70), 0.0)
    # A state saved before reservations existed still loads.
    old = CEO.remote(budget_usd=10.0, state={"spent_usd": 0.10})
    assert ray.get(old.state_snapshot.remote())["spent_usd"] == pytest.approx(0.10)


# --- OMNI-158 (H13): an admin snapshot older than the loop's is never written ---------


def test_an_admin_snapshot_taken_before_a_cycle_does_not_undo_its_spend(
    handles: dict[str, Any], tmp_path: Path,
) -> None:
    # The reviewer's sequence, with the real CEO: sis.admin takes its snapshot,
    # a cycle is charged and the loop saves, then sis.admin writes its own.
    store = episodic.JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    ceo = handles["CEO"]
    ray.get(ceo.pause.remote("looking into a spike"))
    admins = ray.get(ceo.state_snapshot.remote())
    ray.get(ceo.resume.remote())
    ray.get(ceo.report_outcome.remote(success=True, cost_usd=0.60))
    loops = ray.get(ceo.state_snapshot.remote())
    assert loops["version"] > admins["version"]

    assert store.save_state("ceo", loops) is True
    assert store.save_state("ceo", admins) is False
    assert store.load_state("ceo") == loops
    # A CEO started from what was saved continues past it.
    restarted = CEO.remote(budget_usd=10.0, state=loops)
    ray.get(restarted.pause.remote("x"))
    assert ray.get(restarted.state_snapshot.remote())["version"] > loops["version"]
