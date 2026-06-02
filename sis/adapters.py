"""sis.adapters — default in-memory adapters implementing the ports.

These are the bootstrap substrate: real, in-process implementations of the
five capability ports that hold artifact state in memory. They make the
whole org runnable locally with no external credentials, and they serve as
the shared artifact bus + audit trail.

Swapping in Confluence/Jira/GitHub/AWS later means writing classes with the
same method signatures (the ports in :mod:`sis.ports`) that call the
respective MCP/CLI/SDK — the roles do not change.

Destructive / irreversible actions raise :class:`RequiresHumanApproval`
rather than performing them, per the project's hard rules.
"""

from __future__ import annotations

import itertools

from sis.ports import (
    Branch,
    DeployRecord,
    Issue,
    IssueStatus,
    IssueType,
    Page,
    PullRequest,
    RequiresHumanApproval,
)


class InMemoryTelemetry:
    """Captures every emitted event — the audit trail / long-term memory."""

    def __init__(self) -> None:
        self._events: list[dict[str, object]] = []

    def emit(self, event: str, **fields: object) -> None:
        record: dict[str, object] = {"event": event, **fields}
        self._events.append(record)

    def events(self) -> list[dict[str, object]]:
        return list(self._events)


class InMemoryDocumentStore:
    """Confluence stand-in: pages keyed by id, with spaces and labels."""

    def __init__(self, telemetry: InMemoryTelemetry) -> None:
        self._pages: dict[str, Page] = {}
        self._ids = itertools.count(1)
        self._tel = telemetry

    def create_page(
        self, space: str, title: str, body: str, *, parent_id: str | None = None,
        labels: list[str] | None = None,
    ) -> Page:
        page_id = f"PAGE-{next(self._ids)}"
        page = Page(id=page_id, space=space, title=title, body=body,
                    labels=list(labels or []), parent_id=parent_id)
        self._pages[page_id] = page
        self._tel.emit("page.created", page_id=page_id, space=space, title=title)
        return page

    def get_page(self, page_id: str) -> Page:
        return self._pages[page_id]

    def list_pages(self, *, space: str | None = None, label: str | None = None) -> list[Page]:
        out = list(self._pages.values())
        if space is not None:
            out = [p for p in out if p.space == space]
        if label is not None:
            out = [p for p in out if label in p.labels]
        return out

    def archive_page(self, page_id: str) -> None:
        raise RequiresHumanApproval(f"archiving page {page_id} requires human approval")


class InMemoryWorkTracker:
    """Jira stand-in: issues with a status workflow and parent/child links."""

    def __init__(self, telemetry: InMemoryTelemetry) -> None:
        self._issues: dict[str, Issue] = {}
        self._ids = itertools.count(1)
        self._tel = telemetry

    def create_issue(
        self, issue_type: IssueType, summary: str, *, parent_id: str | None = None
    ) -> Issue:
        issue_id = f"{issue_type.name}-{next(self._ids)}"
        issue = Issue(id=issue_id, type=issue_type, summary=summary, parent_id=parent_id)
        self._issues[issue_id] = issue
        self._tel.emit("issue.created", issue_id=issue_id, type=issue_type.value,
                       summary=summary, parent_id=parent_id)
        return issue

    def transition(
        self, issue_id: str, status: IssueStatus, *, comment: str | None = None
    ) -> Issue:
        issue = self._issues[issue_id]
        previous = issue.status
        issue.status = status
        if comment:
            issue.comments.append(comment)
        self._tel.emit("issue.transition", issue_id=issue_id,
                       **{"from": previous.value, "to": status.value}, comment=comment)
        return issue

    def get_issue(self, issue_id: str) -> Issue:
        return self._issues[issue_id]

    def children(self, parent_id: str) -> list[Issue]:
        return [i for i in self._issues.values() if i.parent_id == parent_id]

    def delete_issue(self, issue_id: str) -> None:
        raise RequiresHumanApproval(f"deleting issue {issue_id} requires human approval")


class InMemoryVersionControl:
    """GitHub stand-in. Refuses to commit to main and to merge PRs (human gate)."""

    def __init__(self, telemetry: InMemoryTelemetry) -> None:
        self._branches: dict[str, Branch] = {}
        self._prs: dict[str, PullRequest] = {}
        self._ids = itertools.count(1)
        self._tel = telemetry

    def create_branch(self, name: str, *, base: str = "main") -> Branch:
        if name == "main":
            raise RequiresHumanApproval("the agent must not work directly on main")
        branch = Branch(name=name, base=base)
        self._branches[name] = branch
        self._tel.emit("branch.created", name=name, base=base)
        return branch

    def commit(self, branch: str, message: str) -> str:
        if branch == "main":
            raise RequiresHumanApproval("the agent must never commit to main")
        sha = f"sha-{next(self._ids):06d}"
        self._tel.emit("commit", branch=branch, sha=sha, message=message)
        return sha

    def open_pr(self, branch: str, title: str, *, artifact: str = "") -> PullRequest:
        pr_id = f"PR-{next(self._ids)}"
        pr = PullRequest(id=pr_id, branch=branch, title=title, artifact=artifact)
        self._prs[pr_id] = pr
        self._tel.emit("pr.opened", pr_id=pr_id, branch=branch, title=title)
        return pr

    def get_pr(self, pr_id: str) -> PullRequest:
        return self._prs[pr_id]

    def merge_pr(self, pr_id: str) -> PullRequest:
        raise RequiresHumanApproval(
            f"merging {pr_id} to main is the mandatory human-review gate (gauntlet step 6)"
        )


class InMemoryCloud:
    """AWS + Ray Serve canary stand-in. Blue is live; green is the canary."""

    def __init__(self, telemetry: InMemoryTelemetry) -> None:
        self._tel = telemetry
        self._live: str | None = None
        self._records: list[DeployRecord] = []

    def deploy_canary(
        self, version: str, *, metrics: dict[str, float] | None = None
    ) -> DeployRecord:
        record = DeployRecord(
            version=version, slot="green", live=False, metrics=dict(metrics or {})
        )
        self._records.append(record)
        self._tel.emit("canary.deployed", version=version, slot="green", metrics=record.metrics)
        return record

    def promote(self, version: str) -> DeployRecord:
        raise RequiresHumanApproval(
            f"promoting {version} from green canary to live follows the human PR merge"
        )

    def rollback(self, version: str) -> None:
        self._tel.emit("canary.rolledback", version=version)

    def live_version(self) -> str | None:
        return self._live
