"""Tests for the in-memory port adapters (no Ray required)."""

import pytest

from sis.adapters import (
    InMemoryCloud,
    InMemoryDocumentStore,
    InMemoryTelemetry,
    InMemoryVersionControl,
    InMemoryWorkTracker,
)
from sis.ports import (
    Cloud,
    DocumentStore,
    IssueStatus,
    IssueType,
    RequiresHumanApproval,
    VersionControl,
    WorkTracker,
)


def _tel() -> InMemoryTelemetry:
    return InMemoryTelemetry()


def test_adapters_satisfy_ports() -> None:
    tel = _tel()
    assert isinstance(InMemoryDocumentStore(tel), DocumentStore)
    assert isinstance(InMemoryWorkTracker(tel), WorkTracker)
    assert isinstance(InMemoryVersionControl(tel), VersionControl)
    assert isinstance(InMemoryCloud(tel), Cloud)


def test_document_store_create_and_label() -> None:
    tel = _tel()
    docs = InMemoryDocumentStore(tel)
    page = docs.create_page("SPECS", "Spec A", "body", labels=["spec"])
    assert docs.get_page(page.id).title == "Spec A"
    assert docs.list_pages(label="spec") == [page]
    assert any(e["event"] == "page.created" for e in tel.events())


def test_work_tracker_workflow() -> None:
    tel = _tel()
    work = InMemoryWorkTracker(tel)
    epic = work.create_issue(IssueType.EPIC, "Epic")
    story = work.create_issue(IssueType.STORY, "Story", parent_id=epic.id)
    work.transition(story.id, IssueStatus.READY_FOR_REVIEW, comment="done")
    assert work.get_issue(story.id).status == IssueStatus.READY_FOR_REVIEW
    assert work.children(epic.id) == [story]


def test_never_commit_to_main() -> None:
    vcs = InMemoryVersionControl(_tel())
    with pytest.raises(RequiresHumanApproval):
        vcs.create_branch("main")
    with pytest.raises(RequiresHumanApproval):
        vcs.commit("main", "nope")


def test_live_target_source_empty_in_memory() -> None:
    # The in-memory path keeps no merged base, so the SWE falls back to the
    # local target file (a non-empty return would shadow it).
    assert InMemoryVersionControl(_tel()).live_target_source() == ""


def test_destructive_actions_are_gated() -> None:
    tel = _tel()
    docs = InMemoryDocumentStore(tel)
    work = InMemoryWorkTracker(tel)
    vcs = InMemoryVersionControl(tel)
    cloud = InMemoryCloud(tel)
    page = docs.create_page("S", "t", "b")
    issue = work.create_issue(IssueType.BUG, "b")
    pr = vcs.open_pr(vcs.create_branch("feature/x").name, "t")
    for call in (
        lambda: docs.archive_page(page.id),
        lambda: work.delete_issue(issue.id),
        lambda: vcs.merge_pr(pr.id),
        lambda: cloud.promote("v1"),
    ):
        with pytest.raises(RequiresHumanApproval):
            call()


def test_canary_uses_green_slot() -> None:
    cloud = InMemoryCloud(_tel())
    rec = cloud.deploy_canary("v1", metrics={"latency_seconds": 0.001})
    assert rec.slot == "green" and rec.live is False
    assert cloud.live_version() is None
