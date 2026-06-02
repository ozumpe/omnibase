"""sis.ports — capability interfaces (the "ports") and the artifacts they carry.

The *capability* is required; the *product* is replaceable. Each external
subsystem (Document Store, Work Tracker, Version Control, Cloud, Telemetry)
sits behind a ``typing.Protocol`` defined here. The default in-memory
adapters live in :mod:`sis.adapters`; real MCP-backed adapters (Confluence,
Jira, GitHub, AWS) can be dropped in later without touching the roles, as
long as they satisfy these protocols.

The artifacts (pages, issues, branches, PRs, deploy records) are the
durable substrate the actors coordinate through — simultaneously the work
queue, the inter-actor message bus, the audit trail, and long-term memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class IssueType(str, Enum):
    EPIC = "Epic"
    STORY = "Story"
    SPIKE = "Spike"
    BUG = "Bug"


class IssueStatus(str, Enum):
    BACKLOG = "Backlog"
    TODO = "To Do"
    IN_PROGRESS = "In Progress"
    READY_FOR_REVIEW = "Ready for Review"
    TBD = "TBD"  # QA bounced it back with feedback
    DONE = "Done"


@dataclass
class Page:
    id: str
    space: str
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    parent_id: str | None = None


@dataclass
class Issue:
    id: str
    type: IssueType
    summary: str
    status: IssueStatus = IssueStatus.BACKLOG
    parent_id: str | None = None
    comments: list[str] = field(default_factory=list)


@dataclass
class Branch:
    name: str
    base: str


@dataclass
class PullRequest:
    id: str
    branch: str
    title: str
    # The proposed file content rides in the PR until a human merges it.
    # The agent never writes this to the live target itself (human PR gate).
    artifact: str = ""
    merged: bool = False


@dataclass
class DeployRecord:
    version: str
    slot: str  # "blue" (live) | "green" (canary)
    live: bool
    metrics: dict[str, float] = field(default_factory=dict)


class RequiresHumanApproval(RuntimeError):
    """Raised when a destructive/irreversible action is attempted by the agent.

    Deleting issues, archiving pages, merging to ``main``, and promoting a
    canary to live all require a human — the agent surfaces them, it does
    not perform them.
    """


# --------------------------------------------------------------------------
# Ports
# --------------------------------------------------------------------------


@runtime_checkable
class DocumentStore(Protocol):
    """Designs/specs + the intake channel. Default adapter: Confluence."""

    def create_page(
        self, space: str, title: str, body: str, *, parent_id: str | None = None,
        labels: list[str] | None = None,
    ) -> Page: ...

    def get_page(self, page_id: str) -> Page: ...

    def list_pages(self, *, space: str | None = None, label: str | None = None) -> list[Page]: ...

    def archive_page(self, page_id: str) -> None:  # destructive
        ...


@runtime_checkable
class WorkTracker(Protocol):
    """Epics/stories/spikes/bugs + status workflow. Default adapter: Jira."""

    def create_issue(
        self, issue_type: IssueType, summary: str, *, parent_id: str | None = None
    ) -> Issue: ...

    def transition(
        self, issue_id: str, status: IssueStatus, *, comment: str | None = None
    ) -> Issue: ...

    def get_issue(self, issue_id: str) -> Issue: ...

    def children(self, parent_id: str) -> list[Issue]: ...

    def delete_issue(self, issue_id: str) -> None:  # destructive
        ...


@runtime_checkable
class VersionControl(Protocol):
    """Branches, commits, PRs. Default adapter: GitHub. Never commits to main."""

    def create_branch(self, name: str, *, base: str = "main") -> Branch: ...

    def commit(self, branch: str, message: str) -> str: ...

    def open_pr(self, branch: str, title: str, *, artifact: str = "") -> PullRequest: ...

    def get_pr(self, pr_id: str) -> PullRequest: ...

    def merge_pr(self, pr_id: str) -> PullRequest:  # to main → human-gated
        ...


@runtime_checkable
class Cloud(Protocol):
    """Compute + deploy targets. Default adapter: AWS + Ray Serve canary."""

    def deploy_canary(self, version: str, *, metrics: dict[str, float] | None = None) -> DeployRecord: ...

    def promote(self, version: str) -> DeployRecord:  # green → live → human-gated
        ...

    def rollback(self, version: str) -> None: ...

    def live_version(self) -> str | None: ...


@runtime_checkable
class Telemetry(Protocol):
    """Logs/metrics/traces + the audit trail of every artifact state change."""

    def emit(self, event: str, **fields: object) -> None: ...

    def events(self) -> list[dict[str, object]]: ...
