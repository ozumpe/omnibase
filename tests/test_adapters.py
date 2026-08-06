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


def test_deploy_canary_starts_at_zero_traffic() -> None:
    # A deployed canary must not receive traffic until something shifts it
    # there — deploy and traffic split are separate decisions.
    cloud = InMemoryCloud(_tel())
    cloud.deploy_canary("v1")
    assert cloud.traffic_weights() == {"v1": 0.0}


def test_shift_traffic_records_the_split() -> None:
    tel = _tel()
    cloud = InMemoryCloud(tel)
    cloud.deploy_canary("v1")
    cloud.shift_traffic("v1", 0.05)
    assert cloud.traffic_weights()["v1"] == 0.05
    assert any(e["event"] == "canary.traffic_shifted" for e in tel.events())


@pytest.mark.parametrize("fraction", [-0.1, 1.5])
def test_shift_traffic_rejects_a_fraction_outside_0_1(fraction: float) -> None:
    cloud = InMemoryCloud(_tel())
    with pytest.raises(ValueError, match=r"0\.0\.\.1\.0"):
        cloud.shift_traffic("v1", fraction)


def test_rollback_takes_the_version_out_of_the_split() -> None:
    # Rollback is "kill the canary": it must stop traffic, not just log.
    cloud = InMemoryCloud(_tel())
    cloud.deploy_canary("v1")
    cloud.shift_traffic("v1", 0.5)
    cloud.rollback("v1")
    assert cloud.traffic_weights()["v1"] == 0.0


def test_live_metrics_summarises_observed_requests() -> None:
    cloud = InMemoryCloud(_tel())
    for latency in (0.01, 0.02, 0.03, 0.04):
        cloud.observe("v1", latency)
    cloud.observe("v1", 0.05, error=True)
    metrics = cloud.live_metrics("v1", window_s=60.0)
    assert metrics["samples"] == 5.0
    assert metrics["error_rate"] == pytest.approx(0.2)
    assert metrics["p99"] == 0.05


def test_live_metrics_excludes_observations_outside_the_window() -> None:
    # The window is what makes this a *rolling* signal — a canary must not be
    # judged on traffic from before it was deployed. Injected clock, no sleep.
    now = [1000.0]
    cloud = InMemoryCloud(_tel(), clock=lambda: now[0])
    cloud.observe("v1", 9.9)     # ancient, and slow enough to be obvious
    now[0] += 100.0
    cloud.observe("v1", 0.01)
    metrics = cloud.live_metrics("v1", window_s=30.0)
    assert metrics["samples"] == 1.0
    assert metrics["p99"] == 0.01


def test_live_metrics_for_an_unknown_version_is_empty_not_an_error() -> None:
    cloud = InMemoryCloud(_tel())
    assert cloud.live_metrics("never-deployed", window_s=60.0)["samples"] == 0.0


def test_observations_are_kept_per_version() -> None:
    # Blue and green are compared against each other; mixing their samples
    # would make every canary verdict meaningless.
    cloud = InMemoryCloud(_tel())
    cloud.observe("blue", 0.10)
    cloud.observe("green", 0.01)
    assert cloud.live_metrics("blue", 60.0)["p50"] == 0.10
    assert cloud.live_metrics("green", 60.0)["p50"] == 0.01
