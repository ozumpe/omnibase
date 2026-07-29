"""sis.workspace — the shared artifact bus as a named, detached Ray actor.

All roles coordinate through durable artifacts rather than chatting. To make
those artifacts genuinely *shared* across separate Ray actors, the
in-memory adapters live inside a single ``Workspace`` actor that everyone
looks up by name. The Workspace delegates to the five port adapters and is
the single source of truth for pages, issues, branches, PRs, and deploys —
plus the telemetry audit trail.

Replacing the in-memory adapters with MCP-backed ones (Confluence/Jira/
GitHub/AWS) is a change *inside* this actor; the roles are unaffected.
"""

from __future__ import annotations

from typing import Any

import ray

from sis.adapters import (
    InMemoryCloud,
    InMemoryDocumentStore,
    InMemoryTelemetry,
    InMemoryVersionControl,
    InMemoryWorkTracker,
)
from sis.ports import (
    Branch,
    Cloud,
    DeployRecord,
    DocumentStore,
    Issue,
    IssueStatus,
    IssueType,
    Page,
    PullRequest,
    VersionControl,
    WorkTracker,
)

WORKSPACE_NAME = "Workspace"


@ray.remote
class Workspace:
    """Owns the five capability adapters and exposes them over Ray."""

    def __init__(self) -> None:
        from sis.settings import load_settings, settings_summary

        tel = InMemoryTelemetry()
        self._tel = tel
        settings = load_settings()

        self.docs: DocumentStore
        self.work: WorkTracker
        self.vcs: VersionControl
        self.cloud: Cloud

        if settings.adapters == "real":
            # Real Confluence/Jira/GitHub/AWS adapters, credentials from the
            # configured secret source (local YAML or AWS Secrets Manager).
            from sis.adapters_real import make_real_adapters

            self.docs, self.work, self.vcs, self.cloud = make_real_adapters(settings, tel)
        else:
            # Default: in-memory artifact bus — no credentials, fully local.
            self.docs = InMemoryDocumentStore(tel)
            self.work = InMemoryWorkTracker(tel)
            self.vcs = InMemoryVersionControl(tel)
            self.cloud = InMemoryCloud(tel)

        tel.emit("workspace.ready", **settings_summary(settings))

    # --- Document Store (Confluence) ---
    def create_page(
        self, space: str, title: str, body: str, parent_id: str | None = None,
        labels: list[str] | None = None,
    ) -> Page:
        return self.docs.create_page(space, title, body, parent_id=parent_id, labels=labels)

    def get_page(self, page_id: str) -> Page:
        return self.docs.get_page(page_id)

    def list_pages(self, space: str | None = None, label: str | None = None) -> list[Page]:
        return self.docs.list_pages(space=space, label=label)

    # --- Work Tracker (Jira) ---
    def create_issue(
        self, issue_type: IssueType, summary: str, parent_id: str | None = None
    ) -> Issue:
        return self.work.create_issue(issue_type, summary, parent_id=parent_id)

    def transition(self, issue_id: str, status: IssueStatus, comment: str | None = None) -> Issue:
        return self.work.transition(issue_id, status, comment=comment)

    def get_issue(self, issue_id: str) -> Issue:
        return self.work.get_issue(issue_id)

    def children(self, parent_id: str) -> list[Issue]:
        return self.work.children(parent_id)

    # --- Version Control (GitHub) ---
    def create_branch(self, name: str, base: str = "main") -> Branch:
        return self.vcs.create_branch(name, base=base)

    def commit(self, branch: str, message: str) -> str:
        return self.vcs.commit(branch, message)

    def open_pr(self, branch: str, title: str, artifact: str = "") -> PullRequest:
        return self.vcs.open_pr(branch, title, artifact=artifact)

    def get_pr(self, pr_id: str) -> PullRequest:
        return self.vcs.get_pr(pr_id)

    def live_target_source(self) -> str:
        return self.vcs.live_target_source()

    # --- Cloud (AWS + Ray Serve canary) ---
    def deploy_canary(self, version: str, metrics: dict[str, float] | None = None) -> DeployRecord:
        return self.cloud.deploy_canary(version, metrics=metrics)

    def rollback(self, version: str) -> None:
        self.cloud.rollback(version)

    # --- Telemetry / audit trail ---
    def emit(self, event: str, **fields: object) -> None:
        self._tel.emit(event, **fields)

    def events(self) -> list[dict[str, object]]:
        return self._tel.events()


def get_workspace() -> Any:
    """Return the named Workspace handle (a Ray ActorHandle), creating it if necessary.

    Shares the ``sis`` namespace and uses atomic get-or-create so a persistent
    cluster reuses the one Workspace across runs instead of duplicating it (M2).
    """
    return Workspace.options(  # type: ignore[attr-defined]
        name=WORKSPACE_NAME, namespace="sis", lifetime="detached", get_if_exists=True
    ).remote()
