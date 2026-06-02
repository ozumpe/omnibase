"""sis.adapters_real — real-world adapters for the capability ports.

Concrete implementations backed by:
- **Confluence Cloud REST v2** (Document Store)
- **Jira Cloud REST v3** (Work Tracker)
- **GitHub REST v3** (Version Control)
- **AWS / Ray Serve** (Cloud) — thin placeholder; canary records only.

They satisfy the same protocols as the in-memory adapters in
:mod:`sis.adapters`, so the roles are unchanged — :func:`make_real_adapters`
is selected by :class:`~sis.settings.Settings` when ``SIS_ADAPTERS=real``.

Credentials come from :mod:`sis.settings` (gitignored YAML locally; AWS
Secrets Manager in the cloud) — never hard-coded here.

``requests`` is imported lazily so the default in-memory path needs no extra
deps. The destructive/irreversible guardrails (no commits to main, no PR
merge, no canary promotion without a human) are preserved.

NOTE: these issue live API calls and have not been exercised against a real
tenant in this repo's tests — validate against a scratch space/project/repo
before pointing them at anything that matters.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any, cast

from sis.adapters import InMemoryTelemetry
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
from sis.settings import AtlassianSettings, GitHubSettings, Settings

if TYPE_CHECKING:
    import requests

TARGET_REPO_PATH = "runtime/target.py"  # the file the loop optimises


def _session(email: str | None, token: str) -> requests.Session:
    import requests
    from requests.auth import HTTPBasicAuth

    session = requests.Session()
    if email:  # Atlassian uses basic auth (email:token)
        session.auth = HTTPBasicAuth(email, token)
        session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    else:  # GitHub uses a bearer token
        session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
    return session


def _json(response: requests.Response) -> dict[str, Any]:
    response.raise_for_status()
    return cast("dict[str, Any]", response.json())


# --------------------------------------------------------------------------
# Confluence (Document Store)
# --------------------------------------------------------------------------


class ConfluenceDocumentStore:
    def __init__(self, settings: AtlassianSettings, telemetry: InMemoryTelemetry) -> None:
        self._s = settings
        self._tel = telemetry
        self._http = _session(settings.email, settings.api_token)
        self._space_ids: dict[str, str] = {}

    def _api(self, path: str) -> str:
        return f"{self._s.base_url}/wiki/api/v2{path}"

    def _space_id(self, key: str) -> str:
        if key not in self._space_ids:
            data = _json(self._http.get(self._api("/spaces"), params={"keys": key}))
            results = data.get("results", [])
            if not results:
                raise RuntimeError(f"Confluence space {key!r} not found")
            self._space_ids[key] = str(results[0]["id"])
        return self._space_ids[key]

    def create_page(
        self, space: str, title: str, body: str, *, parent_id: str | None = None,
        labels: list[str] | None = None,
    ) -> Page:
        payload: dict[str, Any] = {
            "spaceId": self._space_id(space),
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": body},
        }
        if parent_id:
            payload["parentId"] = parent_id
        data = _json(self._http.post(self._api("/pages"), json=payload))
        self._tel.emit("page.created", page_id=data["id"], space=space, title=title)
        return Page(id=str(data["id"]), space=space, title=title, body=body,
                    labels=list(labels or []), parent_id=parent_id)

    def get_page(self, page_id: str) -> Page:
        data = _json(self._http.get(self._api(f"/pages/{page_id}"),
                                    params={"body-format": "storage"}))
        return Page(id=str(data["id"]), space=str(data.get("spaceId", "")),
                    title=str(data["title"]),
                    body=str(data.get("body", {}).get("storage", {}).get("value", "")))

    def list_pages(self, *, space: str | None = None, label: str | None = None) -> list[Page]:
        params: dict[str, Any] = {}
        if space:
            params["space-id"] = self._space_id(space)
        data = _json(self._http.get(self._api("/pages"), params=params))
        return [Page(id=str(p["id"]), space=space or "", title=str(p["title"]), body="")
                for p in data.get("results", [])]

    def archive_page(self, page_id: str) -> None:
        raise RequiresHumanApproval(f"archiving page {page_id} requires human approval")


# --------------------------------------------------------------------------
# Jira (Work Tracker)
# --------------------------------------------------------------------------


class JiraWorkTracker:
    def __init__(self, settings: AtlassianSettings, telemetry: InMemoryTelemetry) -> None:
        self._s = settings
        self._tel = telemetry
        self._http = _session(settings.email, settings.api_token)

    def _api(self, path: str) -> str:
        return f"{self._s.base_url}/rest/api/3{path}"

    def create_issue(
        self, issue_type: IssueType, summary: str, *, parent_id: str | None = None
    ) -> Issue:
        fields: dict[str, Any] = {
            "project": {"key": self._s.jira_project},
            "summary": summary,
            "issuetype": {"name": issue_type.value},
        }
        if parent_id:
            fields["parent"] = {"key": parent_id}
        data = _json(self._http.post(self._api("/issue"), json={"fields": fields}))
        issue_key = str(data["key"])
        self._tel.emit("issue.created", issue_id=issue_key, type=issue_type.value)
        return Issue(id=issue_key, type=issue_type, summary=summary, parent_id=parent_id)

    def transition(
        self, issue_id: str, status: IssueStatus, *, comment: str | None = None
    ) -> Issue:
        # Resolve the workflow transition whose target status matches by name.
        data = _json(self._http.get(self._api(f"/issue/{issue_id}/transitions")))
        match = next(
            (t for t in data.get("transitions", [])
             if str(t.get("to", {}).get("name", "")).lower() == status.value.lower()),
            None,
        )
        if match is None:
            raise RuntimeError(
                f"No Jira transition to {status.value!r} available on {issue_id}"
            )
        body: dict[str, Any] = {"transition": {"id": match["id"]}}
        resp = self._http.post(self._api(f"/issue/{issue_id}/transitions"), json=body)
        resp.raise_for_status()
        if comment:
            self._http.post(self._api(f"/issue/{issue_id}/comment"),
                            json={"body": _adf(comment)}).raise_for_status()
        self._tel.emit("issue.transition", issue_id=issue_id, to=status.value, comment=comment)
        return self.get_issue(issue_id)

    def get_issue(self, issue_id: str) -> Issue:
        data = _json(self._http.get(self._api(f"/issue/{issue_id}")))
        f = data.get("fields", {})
        status_name = str(f.get("status", {}).get("name", "Backlog"))
        return Issue(
            id=str(data["key"]),
            type=IssueType(str(f.get("issuetype", {}).get("name", "Story"))),
            summary=str(f.get("summary", "")),
            status=_status_from_name(status_name),
            parent_id=(str(f["parent"]["key"]) if f.get("parent") else None),
        )

    def children(self, parent_id: str) -> list[Issue]:
        jql = f'parent = "{parent_id}"'
        data = _json(self._http.get(self._api("/search"), params={"jql": jql}))
        return [self.get_issue(str(i["key"])) for i in data.get("issues", [])]

    def delete_issue(self, issue_id: str) -> None:
        raise RequiresHumanApproval(f"deleting issue {issue_id} requires human approval")


# --------------------------------------------------------------------------
# GitHub (Version Control)
# --------------------------------------------------------------------------


class GitHubVersionControl:
    def __init__(self, settings: GitHubSettings, telemetry: InMemoryTelemetry) -> None:
        self._s = settings
        self._tel = telemetry
        self._http = _session(None, settings.token)

    def _api(self, path: str) -> str:
        return f"https://api.github.com/repos/{self._s.owner}/{self._s.repo}{path}"

    def create_branch(self, name: str, *, base: str = "main") -> Branch:
        if name == "main":
            raise RequiresHumanApproval("the agent must not work directly on main")
        ref = _json(self._http.get(self._api(f"/git/ref/heads/{base}")))
        sha = ref["object"]["sha"]
        resp = self._http.post(self._api("/git/refs"),
                               json={"ref": f"refs/heads/{name}", "sha": sha})
        resp.raise_for_status()
        self._tel.emit("branch.created", name=name, base=base)
        return Branch(name=name, base=base)

    def commit(self, branch: str, message: str) -> str:
        # For GitHub the file write happens in open_pr (it carries the artifact).
        # commit() is a logging marker here; never operate on main.
        if branch == "main":
            raise RequiresHumanApproval("the agent must never commit to main")
        self._tel.emit("commit.deferred", branch=branch, message=message)
        return ""

    def open_pr(self, branch: str, title: str, *, artifact: str = "") -> PullRequest:
        if artifact:
            self._put_file(branch, TARGET_REPO_PATH, artifact, f"{title} (candidate)")
        data = _json(self._http.post(
            self._api("/pulls"),
            json={"title": title, "head": branch, "base": self._s.default_base,
                  "body": "Automated proposal. Human review + merge required."},
        ))
        pr_id = str(data["number"])
        self._tel.emit("pr.opened", pr_id=pr_id, branch=branch, title=title)
        return PullRequest(id=pr_id, branch=branch, title=title, artifact=artifact)

    def _put_file(self, branch: str, path: str, content: str, message: str) -> None:
        # Hard stop: never write guardrail/safety code, whatever the caller asked.
        from sis.policy import ChangeTier, classify

        if classify(path) is ChangeTier.FORBIDDEN:
            raise RequiresHumanApproval(
                f"{path} is guardrail code; the loop must never write it"
            )
        existing = self._http.get(self._api(f"/contents/{path}"), params={"ref": branch})
        sha = existing.json().get("sha") if existing.status_code == 200 else None
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        self._http.put(self._api(f"/contents/{path}"), json=payload).raise_for_status()
        self._tel.emit("commit", branch=branch, path=path, message=message)

    def get_pr(self, pr_id: str) -> PullRequest:
        data = _json(self._http.get(self._api(f"/pulls/{pr_id}")))
        return PullRequest(id=str(data["number"]), branch=str(data["head"]["ref"]),
                           title=str(data["title"]), merged=bool(data.get("merged", False)))

    def merge_pr(self, pr_id: str) -> PullRequest:
        raise RequiresHumanApproval(
            f"merging {pr_id} to {self._s.default_base} is the mandatory human-review gate"
        )


# --------------------------------------------------------------------------
# Cloud (AWS + Ray Serve canary) — placeholder for the cloud deployment
# --------------------------------------------------------------------------


class RealCloud:
    """Records canaries; promotion/rollback wire to Ray Serve/AWS later."""

    def __init__(self, telemetry: InMemoryTelemetry) -> None:
        self._tel = telemetry
        self._live: str | None = None

    def deploy_canary(
        self, version: str, *, metrics: dict[str, float] | None = None
    ) -> DeployRecord:
        self._tel.emit("canary.deployed", version=version, slot="green", metrics=metrics or {})
        return DeployRecord(version=version, slot="green", live=False, metrics=dict(metrics or {}))

    def promote(self, version: str) -> DeployRecord:
        raise RequiresHumanApproval(f"promoting {version} to live follows the human PR merge")

    def rollback(self, version: str) -> None:
        self._tel.emit("canary.rolledback", version=version)

    def live_version(self) -> str | None:
        return self._live


# --------------------------------------------------------------------------
# Helpers + factory
# --------------------------------------------------------------------------


def _adf(text: str) -> dict[str, Any]:
    """Wrap plain text in a minimal Atlassian Document Format node (Jira v3)."""
    return {
        "type": "doc", "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _status_from_name(name: str) -> IssueStatus:
    for status in IssueStatus:
        if status.value.lower() == name.lower():
            return status
    return IssueStatus.BACKLOG


def make_real_adapters(
    settings: Settings, telemetry: InMemoryTelemetry
) -> tuple[Any, Any, Any, Any]:
    """Build (DocumentStore, WorkTracker, VersionControl, Cloud) from settings."""
    atlassian = settings.require_atlassian()
    github = settings.require_github()
    return (
        ConfluenceDocumentStore(atlassian, telemetry),
        JiraWorkTracker(atlassian, telemetry),
        GitHubVersionControl(github, telemetry),
        RealCloud(telemetry),
    )
