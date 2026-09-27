"""OMNI-126 (and OMNI-57): a PR awaiting a human survives a restart. No Ray here.

The second AWS run (2026-09-27) ran one `main.py` cycle, which opened testrun
PR #11, then started `--loop`. The pending PR lived only in the first
process's SelfModel, so the loop proposed the same change again as PR #12,
47 s later. The live wiring (restore → hold → a human decides → released) is
in ``tests/test_merge_observation.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sis import org
from sis.adapters import InMemoryTelemetry, InMemoryVersionControl
from sis.episodic import JsonlEpisodicStore
from sis.ports import PullRequest, PullRequestNotFound
from sis.roles import pr_resolution

_REPO = "ozumpe/testrun"
_VERIFIED: dict[str, Any] = {
    "status": "verified_awaiting_human_merge", "pr_id": "11", "contract": "sort",
    "canary": {"version": "feature/story-58@11"},
}


# --- what a PR's state means -------------------------------------------------


@pytest.mark.parametrize(("merged", "closed", "expected"), [
    (False, False, "hold"),       # under review, however long that takes
    (True, True, "promote"),      # a human merged it
    (False, True, "release"),     # a human declined it (OMNI-57)
])
def test_only_a_human_decision_ends_a_hold(merged: bool, closed: bool, expected: str) -> None:
    pr = PullRequest(id="11", branch="b", title="t", merged=merged, closed=closed)
    assert pr_resolution(pr) == expected


# --- what a cycle leaves behind ---------------------------------------------


def test_a_verified_cycle_on_github_is_remembered() -> None:
    record = org.pending_pr_record(_VERIFIED, repo=_REPO, now="T")
    assert record == {"pr_id": "11", "version": "feature/story-58@11",
                      "contract": "sort", "repo": _REPO, "since": "T"}


@pytest.mark.parametrize("result", [
    {**_VERIFIED, "status": "rolled_back"},      # no PR left for a human
    {**_VERIFIED, "status": "qa_rejected"},
    {**_VERIFIED, "canary": None},                # nothing deployed to hold
    {**_VERIFIED, "pr_id": None},
])
def test_only_a_verified_candidate_leaves_a_pr_to_wait_on(result: dict[str, Any]) -> None:
    assert org.pending_pr_record(result, repo=_REPO, now="T") is None


def test_an_in_memory_pr_is_never_remembered() -> None:
    # It dies with its adapter, and ids restart at PR-1: remembering one would
    # only ever restore a hold on nothing.
    assert org.pending_pr_record(_VERIFIED, repo=None, now="T") is None


def test_durable_only_with_real_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(org.config, "get", lambda key: "memory")
    assert org.durable_vcs() is None


# --- remembering and forgetting -----------------------------------------------


def _store(tmp_path: Path) -> JsonlEpisodicStore:
    return JsonlEpisodicStore(tmp_path / "episodic.jsonl")


def test_remembered_next_to_the_brake_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    org.remember_pending_pr(store, _VERIFIED, repo=_REPO)
    record = store.load_state(org.PENDING_PR_KEY)
    assert record is not None and record["pr_id"] == "11" and record["repo"] == _REPO
    assert (tmp_path / "episodic_state.json").exists()


def test_forgetting_only_clears_the_pr_that_was_decided(tmp_path: Path) -> None:
    store = _store(tmp_path)
    org.remember_pending_pr(store, _VERIFIED, repo=_REPO)
    org.forget_pending_pr(store, "9")        # a stale caller: not this PR
    assert store.load_state(org.PENDING_PR_KEY)["pr_id"] == "11"  # type: ignore[index]
    org.forget_pending_pr(store, "11")
    assert not store.load_state(org.PENDING_PR_KEY)


# --- restoring: the paths that return before any actor is asked -------------


def test_nothing_is_restored_without_a_durable_vcs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    org.remember_pending_pr(store, _VERIFIED, repo=_REPO)
    assert org.restore_pending_pr({}, store, repo=None) is None


def test_nothing_to_restore_says_nothing(tmp_path: Path) -> None:
    assert org.restore_pending_pr({}, _store(tmp_path), repo=_REPO) is None


def test_a_pr_in_another_repo_is_forgotten_not_waited_on(tmp_path: Path) -> None:
    # PR numbers are per repo: #11 here is a different PR from #11 there.
    store = _store(tmp_path)
    org.remember_pending_pr(store, _VERIFIED, repo="someone/else")
    line = org.restore_pending_pr({}, store, repo=_REPO)
    assert line is not None and "someone/else" in line and _REPO in line
    assert not store.load_state(org.PENDING_PR_KEY)


def test_unreadable_state_restores_nothing(tmp_path: Path) -> None:
    # The CEO has already booted with its breaker open over the same file.
    (tmp_path / "episodic_state.json").write_text("{not json", encoding="utf-8")
    assert org.restore_pending_pr({}, _store(tmp_path), repo=_REPO) is None


# --- the in-memory adapter's side of the port ----------------------------------


def test_an_unknown_pr_is_not_found() -> None:
    vcs = InMemoryVersionControl(InMemoryTelemetry())
    with pytest.raises(PullRequestNotFound):
        vcs.get_pr("PR-1", path=None)
    assert issubclass(PullRequestNotFound, LookupError)


def test_a_human_closing_a_pr_is_not_a_merge() -> None:
    vcs = InMemoryVersionControl(InMemoryTelemetry())
    pr = vcs.open_pr(vcs.create_branch("feature/x").name, "t", path="runtime/target.py")
    vcs.simulate_human_close(pr.id)
    seen = vcs.get_pr(pr.id, path=None)
    assert (seen.merged, seen.closed) == (False, True)
    vcs.simulate_human_merge(vcs.open_pr("feature/y", "t", path="p").id)
    assert vcs.get_pr("PR-2", path=None).closed is True


# --- a human's decision is logged as one (OMNI-57) -----------------------------


@pytest.mark.parametrize(("reason", "status"), [
    ("closed without merging", "human_declined"), ("no longer exists", "pr_vanished")])
def test_a_released_hold_is_logged_as_what_ended_it(reason: str, status: str) -> None:
    seen = {"released": True, "reason": reason, "pr": "11", "contract": "sort"}
    assert org.release_result(seen) == {"status": status, "pr_id": "11", "contract": "sort"}


def test_a_hold_that_was_not_released_logs_nothing() -> None:
    assert org.release_result({"released": False, "promoted": True}) is None
    assert org.release_result({}) is None


def test_a_decline_is_never_read_as_a_gauntlet_rejection(tmp_path: Path) -> None:
    # A human closing a PR judges the change; it is not the loop failing. No
    # reject gate, no cost, and nothing the breaker or the gate stats count.
    store = _store(tmp_path)
    org.record_release(store, {"released": True, "reason": "closed without merging",
                               "pr": "11", "contract": "sort"})
    (event,) = store.events()
    assert (event.outcome, event.pr_id, event.contract) == ("human_declined", "11", "sort")
    assert event.reject_gate is None and event.reject_reason is None
    assert event.gauntlet_passed is None and event.cost_usd == 0.0
    assert event.proposer == "human"
    assert store.summary()["rejected_by_gate"] == {}
