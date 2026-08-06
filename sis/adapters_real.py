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

NOTE: these issue live API calls. They have been validated end-to-end against
a live tenant (runbook Level 2, incl. re-runs), but still point them at a
scratch space/project/repo before anything that matters — a cycle creates
real pages, issues, branches, and PRs.
"""

from __future__ import annotations

import base64
import os
import re
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

# Default per-request timeout (seconds), applied to every real-adapter call so a
# wedged tenant API can't hang a whole cycle forever — there is no gauntlet-style
# timeout around adapter I/O (KNOWN_ISSUES.md M6). Override via SIS_HTTP_TIMEOUT.
DEFAULT_HTTP_TIMEOUT = 30.0


def _http_timeout() -> float:
    """The per-request timeout, from SIS_HTTP_TIMEOUT or the default. A bad value
    fails loudly rather than silently reverting to "wait forever"."""
    raw = os.getenv("SIS_HTTP_TIMEOUT")
    if not raw:
        return DEFAULT_HTTP_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"SIS_HTTP_TIMEOUT={raw!r} is not a valid number") from None
    if value <= 0:
        raise ValueError(f"SIS_HTTP_TIMEOUT={raw!r} must be positive")
    return value


class _TimeoutHTTP:
    """Wraps a ``requests.Session`` to apply a default ``timeout`` to every call.

    ``requests`` defaults to *no* timeout, so any missed call site would block a
    cycle indefinitely on a hung tenant API. Routing the adapters through this
    wrapper makes the timeout the default for every request while still letting a
    caller pass an explicit ``timeout=`` (it wins). Only the verbs the adapters
    use are exposed — a deliberately small surface.
    """

    def __init__(self, session: requests.Session, timeout: float) -> None:
        self._s = session
        self._timeout = timeout

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        return self._s.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        return self._s.post(url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        return self._s.put(url, **kwargs)


def _session(email: str | None, token: str) -> _TimeoutHTTP:
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
    return _TimeoutHTTP(session, _http_timeout())


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
        resp = self._http.post(self._api("/pages"), json=payload)
        if resp.status_code == 404 and parent_id:
            # Confluence cannot parent a page across spaces (e.g. a spec in the
            # spec space under its proposal in the intake space) — it 404s on
            # the parent. The provenance link lives in the SelfModel anyway, so
            # drop the parent rather than fail the cycle.
            payload.pop("parentId")
            resp = self._http.post(self._api("/pages"), json=payload)
            self._tel.emit("page.parent_dropped", space=space, title=title,
                           parent_id=parent_id)
        if resp.status_code == 400 and "already exists" in resp.text.lower():
            # Confluence enforces unique titles per space, so a re-run against a
            # live tenant 400s on every fixed-title page (charter, specs, …).
            # Reuse the existing page and refresh its content instead.
            page_id = self._page_id_by_title(space, title)
            changed = self._update_body(page_id, title, body)
            self._tel.emit("page.updated" if changed else "page.unchanged",
                           page_id=page_id, space=space, title=title)
            self._apply_labels(page_id, labels)
            return Page(id=page_id, space=space, title=title, body=body,
                        labels=list(labels or []), parent_id=parent_id)
        data = _json(resp)
        page_id = str(data["id"])
        self._tel.emit("page.created", page_id=page_id, space=space, title=title)
        self._apply_labels(page_id, labels)
        return Page(id=page_id, space=space, title=title, body=body,
                    labels=list(labels or []), parent_id=parent_id)

    def _apply_labels(self, page_id: str, labels: list[str] | None) -> None:
        """Attach labels to a page (best-effort, so a failure never breaks a
        cycle — labels are cosmetic). Confluence v2 has no label *write*, so
        this uses the v1 content-label endpoint; the rest of the adapter is v2.
        Adding an existing label is a no-op, so this is idempotent on re-runs."""
        if not labels:
            return
        url = f"{self._s.base_url}/wiki/rest/api/content/{page_id}/label"
        try:
            resp = self._http.post(
                url, json=[{"prefix": "global", "name": name} for name in labels])
            resp.raise_for_status()
            self._tel.emit("page.labels_applied", page_id=page_id, labels=labels)
        except Exception as exc:  # noqa: BLE001 - cosmetic; never fail a cycle on a label
            self._tel.emit("page.labels_failed", page_id=page_id, labels=labels,
                           error=type(exc).__name__)

    def _page_id_by_title(self, space: str, title: str) -> str:
        data = _json(self._http.get(
            self._api(f"/spaces/{self._space_id(space)}/pages"),
            params={"title": title}))
        results = data.get("results", [])
        if not results:
            raise RuntimeError(f"Confluence page {title!r} not found in space {space!r}")
        return str(results[0]["id"])

    def _update_body(self, page_id: str, title: str, body: str) -> bool:
        """Refresh a page's body in place. Returns whether a new version was
        written — a no-op body is skipped so re-runs don't churn the version
        history on fixed-title pages (L2)."""
        current = _json(self._http.get(self._api(f"/pages/{page_id}"),
                                       params={"body-format": "storage"}))
        stored = str(current.get("body", {}).get("storage", {}).get("value", ""))
        if stored == body:
            return False
        version = int(current.get("version", {}).get("number", 0))
        _json(self._http.put(self._api(f"/pages/{page_id}"), json={
            "id": page_id,
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": body},
            "version": {"number": version + 1},
        }))
        return True

    def get_page(self, page_id: str) -> Page:
        data = _json(self._http.get(self._api(f"/pages/{page_id}"),
                                    params={"body-format": "storage"}))
        return Page(id=str(data["id"]), space=str(data.get("spaceId", "")),
                    title=str(data["title"]),
                    body=str(data.get("body", {}).get("storage", {}).get("value", "")))

    def list_pages(self, *, space: str | None = None, label: str | None = None) -> list[Page]:
        # `label` is accepted for protocol conformance but not applied: v2
        # GET /pages has no label filter (that needs a CQL search), and no
        # caller in the loop filters by label. See KNOWN_ISSUES.md L1.
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

# Jira's issue-key grammar: PROJECT-123. Anything outside it must never reach a
# JQL string (see JiraWorkTracker.children).
_JIRA_KEY = re.compile(r"[A-Z][A-Z0-9_]*-[0-9]+")


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
            # Best-effort, like the page labels in _apply_labels: the transition
            # above has already succeeded and is not undoable here. Raising on
            # the *comment* would report the whole transition as failed, so the
            # caller retries against an issue that has already moved — and the
            # second attempt finds no matching transition and hard-fails the
            # cycle. The comment is an audit note; losing one must not cost the
            # state change it annotates.
            try:
                self._http.post(self._api(f"/issue/{issue_id}/comment"),
                                json={"body": _adf(comment)}).raise_for_status()
            except Exception as exc:  # noqa: BLE001 - never fail a completed transition
                self._tel.emit("issue.comment_failed", issue_id=issue_id,
                               to=status.value, error=str(exc))
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
        # Enhanced search: the classic GET /rest/api/3/search was removed by
        # Atlassian; use POST /rest/api/3/search/jql. Only keys are needed —
        # get_issue() fetches each issue's fields.
        # /search/jql takes JQL as a string with no parameter binding, so the
        # key is validated against Jira's key grammar before interpolation.
        # Today every caller passes an internal key — but that is a property of
        # the callers, not something enforced here, and the intake path exists
        # to let outside text reach the org. Enforce it at the boundary.
        if not _JIRA_KEY.fullmatch(parent_id):
            raise ValueError(f"not a valid Jira issue key: {parent_id!r}")
        jql = f'parent = "{parent_id}"'
        data = _json(self._http.post(self._api("/search/jql"),
                                     json={"jql": jql, "fields": ["key"]}))
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
        if resp.status_code == 422 and "already exists" in resp.text.lower():
            # L8: a retry of the same story — the branch is already there. Reuse
            # it; open_pr's _put_file updates the target on it either way.
            self._tel.emit("branch.exists", name=name, base=base)
            return Branch(name=name, base=base)
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
        resp = self._http.post(
            self._api("/pulls"),
            json={"title": title, "head": branch, "base": self._s.default_base,
                  "body": "Automated proposal. Human review + merge required."},
        )
        if resp.status_code == 422 and "already exists" in resp.text.lower():
            # L11 (L8's sibling): a retry of the same story — a PR for this head is
            # already open. create_branch already reuses the branch; reuse the PR
            # too instead of dying, so a re-run is idempotent end to end.
            return self._existing_pr(branch, title, artifact)
        data = _json(resp)
        pr_id = str(data["number"])
        self._tel.emit("pr.opened", pr_id=pr_id, branch=branch, title=title)
        return PullRequest(id=pr_id, branch=branch, title=title, artifact=artifact)

    def _existing_pr(self, branch: str, title: str, artifact: str) -> PullRequest:
        """Find the open PR already opened for *branch* (used on a 422 re-run)."""
        listing = self._http.get(
            self._api("/pulls"),
            params={"head": f"{self._s.owner}:{branch}", "state": "open"})
        listing.raise_for_status()
        prs = listing.json()
        if not prs:  # 422 for some other reason — surface it
            raise RuntimeError(f"open_pr got 422 but no open PR exists for {branch}")
        pr_id = str(prs[0]["number"])
        self._tel.emit("pr.exists", pr_id=pr_id, branch=branch)
        return PullRequest(id=pr_id, branch=branch,
                           title=str(prs[0].get("title", title)), artifact=artifact)

    def _put_file(self, branch: str, path: str, content: str, message: str) -> None:
        # Hard stop at the write boundary: the loop's GitHub writes are limited to
        # SOFT optimisation target(s). This refuses FORBIDDEN guardrail code *and*
        # STRICT engine code — defence in depth beyond the SWE's authorize_change,
        # so a future caller passing a non-target path can't reach the API (L14).
        from sis.policy import ChangeTier, classify

        tier = classify(path)
        if tier is not ChangeTier.SOFT:
            raise RequiresHumanApproval(
                f"{path} is {tier.value}-tier; the loop's writes are limited to the "
                "SOFT optimisation target(s)"
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
        head_ref = str(data["head"]["ref"])
        # GitHub's PR API doesn't carry file contents, but QA re-validates the
        # candidate from pr.artifact — so fetch the proposed target at the head
        # ref and populate it (mirrors what open_pr() wrote via _put_file).
        return PullRequest(
            id=str(data["number"]), branch=head_ref, title=str(data["title"]),
            artifact=self._get_file(head_ref, TARGET_REPO_PATH),
            merged=bool(data.get("merged", False)),
        )

    def live_target_source(self) -> str:
        """The target file as merged on the live base branch ("" if absent)."""
        return self._get_file(self._s.default_base, TARGET_REPO_PATH)

    def _get_file(self, ref: str, path: str) -> str:
        """Fetch and decode a file's content at *ref* ("" if absent)."""
        resp = self._http.get(self._api(f"/contents/{path}"), params={"ref": ref})
        if resp.status_code != 200:
            return ""
        content = str(resp.json().get("content", ""))
        # GitHub base64-encodes with embedded newlines; b64decode ignores them.
        return base64.b64decode(content).decode("utf-8") if content else ""

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

    # shift_traffic/live_metrics exist so RealCloud keeps satisfying the
    # @runtime_checkable Cloud protocol now that the port has grown them; there
    # is no traffic to split and no deployment to measure until ServeCloud
    # lands. They raise rather than no-op on purpose: a silent no-op here would
    # let a real run report a "passing canary" that never routed a request or
    # measured anything — the failure mode a canary exists to prevent.
    def shift_traffic(self, version: str, fraction: float) -> None:
        raise NotImplementedError(
            "RealCloud cannot split traffic — it records deployments only. "
            "Use ServeCloud (docs/SERVE_CANARY.md) for a weighted canary."
        )

    def live_metrics(self, version: str, window_s: float) -> dict[str, float]:
        raise NotImplementedError(
            "RealCloud has no live metrics source — it records deployments only. "
            "Use ServeCloud (docs/SERVE_CANARY.md) for real per-version metrics."
        )

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
