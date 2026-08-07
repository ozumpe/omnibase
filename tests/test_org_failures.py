"""Failure-path wiring of the org cycle: bugs filed, breaker paged (needs Ray).

Runs in its own module so it gets a fresh Ray cluster (test_org.py's teardown
shuts the previous one down) — tripping the circuit breaker here must not
poison the healthy-path tests.
"""

from types import SimpleNamespace
from typing import Any

import pytest

ray = pytest.importorskip("ray")

from sis import org  # noqa: E402
from sis.ports import IssueType  # noqa: E402

FAILED_IMPL: dict[str, Any] = {
    "passed": False,
    "reason": "no improvement: candidate 0.000200s vs baseline 0.000100s (need ≤ 90%)",
    "pr_id": None,
    "cost_usd": 0.0,
    "candidate_sha": "deadbeef0123",
}


@pytest.fixture(scope="module")
def handles():  # type: ignore[no-untyped-def]
    h = org.bootstrap()
    # Deterministic failures: swap the SWE handle for a fake whose implement()
    # always fails the gauntlet. ray.put gives run_cycle the ObjectRef shape it
    # expects from a real actor call.
    h["SWE"] = SimpleNamespace(
        implement=SimpleNamespace(remote=lambda story_id, contract_name=None: ray.put(FAILED_IMPL)))
    yield h
    ray.shutdown()


def test_failed_cycle_files_a_bug(handles) -> None:  # type: ignore[no-untyped-def]
    result = org.run_cycle(handles, "will fail", "make it fast")
    assert result["status"] == "rolled_back"
    # The failure became an artifact in the work tracker (ACTORS.md: DevOps
    # files bug/defect Jiras), not just an episodic-log line.
    assert result["bug_id"] is not None
    bug = ray.get(handles["Workspace"].get_issue.remote(result["bug_id"]))
    assert bug.type is IssueType.BUG


def test_circuit_breaker_files_a_page_then_opens(handles) -> None:  # type: ignore[no-untyped-def]
    # Threshold is 3 consecutive failures; drive cycles until the breaker trips.
    breaker_bug = None
    for _ in range(8):
        r = org.run_cycle(handles, "will fail", "again")
        if r.get("breaker_bug_id"):
            breaker_bug = r["breaker_bug_id"]
        if r["status"] == "circuit_breaker_open":
            break

    # The trip filed exactly one "page a human" bug, once.
    assert breaker_bug is not None
    page = ray.get(handles["Workspace"].get_issue.remote(breaker_bug))
    assert page.type is IssueType.BUG
    assert "CIRCUIT BREAKER" in page.summary

    # Once open, further cycles are refused before any work/spend.
    assert org.run_cycle(handles, "x", "y")["status"] == "circuit_breaker_open"


def test_charter_is_set_at_bootstrap(handles) -> None:  # type: ignore[no-untyped-def]
    # The CEO wrote the top-level charter once; provenance roots at it.
    kinds = [e["kind"] for e in ray.get(handles["SelfModel"].provenance.remote())]
    assert "charter" in kinds
