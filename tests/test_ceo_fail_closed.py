"""OMNI-61 acceptance: corrupt brake state means the breaker is open on bootstrap.

Its own module, and it shuts any running cluster down first: the CEO is a named
detached singleton, so a CEO left over from another module would be reused and
never read the corrupt state this test plants.
"""

from pathlib import Path

import pytest

ray = pytest.importorskip("ray")

from sis import episodic, org  # noqa: E402

_CORRUPT = '{"ceo": {"spent_usd": 3.2'  # truncated mid-write, the old failure mode


@pytest.fixture(scope="module")
def planted(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    tmp = tmp_path_factory.mktemp("brakes")
    store = episodic.JsonlEpisodicStore(tmp / "episodic.jsonl")
    (tmp / "episodic_state.json").write_text(_CORRUPT, encoding="utf-8")
    patch = pytest.MonkeyPatch()
    patch.setattr(org.episodic, "get_episodic_store", lambda *a, **k: store)
    ray.shutdown()
    try:
        yield org.bootstrap(), tmp / "episodic_state.json"
    finally:
        patch.undo()
        ray.shutdown()


def test_the_breaker_is_open_and_says_why(planted) -> None:  # type: ignore[no-untyped-def]
    handles, _ = planted
    ceo = handles["CEO"]
    assert ray.get(ceo.breaker_open.remote()) is True
    reason = ray.get(ceo.state_snapshot.remote())["trip_reason"]
    assert "unreadable" in reason and "sis.admin reset-breaker" in reason


def test_no_cycle_runs_and_the_evidence_is_not_overwritten(planted) -> None:  # type: ignore[no-untyped-def]
    handles, state_file = planted
    result = org.run_cycle(handles, "t", "b")
    assert result["status"] == "circuit_breaker_open"
    # run_cycle tried to persist spent=0 over the file; the store refused.
    assert Path(state_file).read_text(encoding="utf-8") == _CORRUPT
