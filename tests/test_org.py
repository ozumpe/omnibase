"""End-to-end test of the actor org cycle (requires Ray, runs locally)."""

import pytest

ray = pytest.importorskip("ray")

from sis import org  # noqa: E402


@pytest.fixture(scope="module")
def handles():  # type: ignore[no-untyped-def]
    h = org.bootstrap()
    yield h
    ray.shutdown()


def test_full_cycle_reaches_human_merge_gate(handles) -> None:  # type: ignore[no-untyped-def]
    result = org.run_cycle(
        handles,
        "Speed up divisor-sum",
        "It is too slow; make it faster without changing results.",
    )
    # The stub proposer returns the known-good O(sqrt n) variant, so the
    # cycle should pass the gauntlet, QA, and stop at the human merge gate.
    assert result["status"] == "verified_awaiting_human_merge"
    assert result["candidate_latency"] < result["baseline_latency"]
    assert result["pr_id"] is not None

    # Canary deployed to the green slot, never promoted to live.
    assert result["canary"]["slot"] == "green"
    assert result["canary"]["live"] is False

    # Provenance graph captured the full chain.
    kinds = {e["kind"] for e in result["provenance"]}
    assert {"spec", "epic", "story", "pr", "canary", "outcome"} <= kinds

    # The candidate diff is fingerprinted (12-hex sha) so the episodic log can
    # dedupe/correlate proposals — the join key of the learning dataset.
    assert len(result["candidate_sha"]) == 12
    assert all(c in "0123456789abcdef" for c in result["candidate_sha"])


def test_self_model_registry_has_full_org(handles) -> None:  # type: ignore[no-untyped-def]
    roles = {info["role"] for info in ray.get(handles["SelfModel"].registry.remote())}
    assert {"CEO", "PM", "CTO", "Designer", "SWE", "QA", "DevOps"} <= roles


def test_story_done_after_qa(handles) -> None:  # type: ignore[no-untyped-def]
    result = org.run_cycle(handles, "Another speedup", "make it fast")
    from sis.ports import IssueStatus
    issue = ray.get(handles["Workspace"].get_issue.remote(result["story_id"]))
    assert issue.status == IssueStatus.DONE
