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

import sys
from typing import Any

import ray

from sis.adapters import (
    InMemoryCloud,
    InMemoryDocumentStore,
    InMemoryNotifier,
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
    Notifier,
    Page,
    PullRequest,
    Severity,
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

        # The pager (OMNI-62) is chosen by its own key, not adapters.mode: a
        # real-adapter run without a topic still runs — but says, once and
        # loudly, that a breaker trip will reach nobody.
        from sis import config

        self.notifier: Notifier
        topic = config.get("adapters.notify_sns_topic_arn")
        if topic:
            from sis.adapters_real import SNSNotifier, pager_problem

            sns = SNSNotifier(str(topic), tel)
            self.notifier = sns
            # OMNI-122: a topic nobody confirmed accepts every page and delivers
            # none. The first AWS run started that way; say so before a cycle.
            try:
                if problem := pager_problem(*sns.subscriptions()):
                    print(f"[sis] WARNING: {problem}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - a warning, not a refusal
                print(f"[sis] WARNING: could not verify the pager's subscriptions: {exc}",
                      file=sys.stderr)
        else:
            self.notifier = InMemoryNotifier(tel)
            if settings.adapters == "real":
                print("[sis] WARNING: adapters.mode=real but no pager is configured "
                      "(SIS_NOTIFY_SNS_TOPIC_ARN): a breaker trip will only file a "
                      "bug nobody watches in real time", file=sys.stderr)

        tel.emit("workspace.ready", **settings_summary(settings))

    # --- Notifier (SNS) ---
    def notify(self, severity: Severity, title: str, body: str) -> dict[str, str]:
        """Page a human. Never raises (OMNI-62).

        A page that fails to send must not take down the loop it was reporting
        on — but it must not vanish either: it is emitted, printed, and handed
        back so the driver can record it in the episodic store.
        """
        try:
            return {"delivered": self.notifier.notify(severity, title, body)}
        except Exception as exc:  # noqa: BLE001 - a failed page is reported, not raised
            self._tel.emit("notify.failed", severity=severity.value, title=title,
                           error=str(exc))
            print(f"[sis] WARNING: page not sent ({title}): {exc}", file=sys.stderr)
            return {"error": str(exc)}

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

    def open_pr(
        self, branch: str, title: str, artifact: str, path: str, body: str = ""
    ) -> PullRequest:
        return self.vcs.open_pr(branch, title, artifact=artifact, path=path, body=body)

    def get_pr(self, pr_id: str, path: str | None) -> PullRequest:
        return self.vcs.get_pr(pr_id, path=path)

    def open_prs(self) -> list[PullRequest]:
        return self.vcs.open_prs()

    def live_target_source(self, path: str) -> str:
        return self.vcs.live_target_source(path)

    def write_file(self, branch: str, path: str, content: str, message: str) -> None:
        self.vcs.write_file(branch, path, content, message)

    def read_file(self, ref: str, path: str) -> str:
        return self.vcs.read_file(ref, path)

    # --- Cloud (AWS + Ray Serve canary) ---
    def deploy_canary(self, version: str, metrics: dict[str, float] | None = None) -> DeployRecord:
        return self.cloud.deploy_canary(version, metrics=metrics)

    def promote(self, version: str) -> DeployRecord:
        return self.cloud.promote(version)

    def live_version(self) -> str | None:
        return self.cloud.live_version()

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
