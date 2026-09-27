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
    # OMNI-123: every result carries its cost and the spend so far, so the
    # console line can report money for every exit, not just the success path.
    assert "cost_usd" in result and result["economics"]["budget_usd"] > 0
    assert org.cycle_summary(result).startswith("[cycle] rolled_back: ")


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
    # OMNI-62: the bug is the audit trail; a person is paged as well — once.
    pages = [e for e in ray.get(handles["Workspace"].events.remote())
             if e["event"] == "notify.sent" and str(e["title"]).startswith("circuit breaker")]
    assert len(pages) == 1 and pages[0]["severity"] == "critical"

    # Once open, further cycles are refused before any work/spend.
    assert org.run_cycle(handles, "x", "y")["status"] == "circuit_breaker_open"


def test_charter_is_set_at_bootstrap(handles) -> None:  # type: ignore[no-untyped-def]
    # The CEO wrote the top-level charter once; provenance roots at it.
    kinds = [e["kind"] for e in ray.get(handles["SelfModel"].provenance.remote())]
    assert "charter" in kinds
