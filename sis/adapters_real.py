"""sis.adapters_real — real-world adapters for the capability ports.

Concrete implementations backed by:
- **Confluence Cloud REST v2** (Document Store)
- **Jira Cloud REST v3** (Work Tracker)
- **GitHub REST v3** (Version Control)
- **AWS / Ray Serve** (Cloud) — thin placeholder; canary records only.
- **EIA's bulk petroleum file** (Sensor, OMNI-33) — captured into an
  append-only trace (:mod:`sis.sensor_trace`); every reading comes through
  :func:`sis.sensor_input.sanitise`, and none is built here.

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
import datetime
import hashlib
import json
import pathlib
import re
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from sis import config, sensor_trace
from sis.adapters import InMemoryTelemetry, SimSensor
from sis.clock import Clock, WallClock, parse_event_time
from sis.paths import SENSOR_TRACES_DIR
from sis.ports import (
    Branch,
    BranchState,
    DeployRecord,
    Issue,
    IssueStatus,
    IssueType,
    Page,
    PullRequest,
    PullRequestNotFound,
    Reading,
    RequiresHumanApproval,
    Severity,
    TimeAxis,
)
from sis.sensor_input import Absent, SeriesRule, SourceRules
from sis.sensor_trace import Fetched
from sis.settings import AtlassianSettings, GitHubSettings, Settings

if TYPE_CHECKING:
    import requests

# Default per-request timeout (seconds), applied to every real-adapter call so a
# wedged tenant API can't hang a whole cycle forever — there is no gauntlet-style
# timeout around adapter I/O (KNOWN_ISSUES.md M6). Override via SIS_HTTP_TIMEOUT.
DEFAULT_HTTP_TIMEOUT = 30.0


def _http_timeout() -> float:
    """The per-request timeout (``adapters.http_timeout_seconds``).

    A bad value fails loudly in :mod:`sis.config` rather than silently reverting
    to "wait forever" — the schema marks the key ``positive``, so zero and
    negative are both rejected at parse time.
    """
    seconds: float = config.get("adapters.http_timeout_seconds")
    return seconds


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

    def open_pr(
        self, branch: str, title: str, *, artifact: str = "", path: str, body: str = ""
    ) -> PullRequest:
        if artifact:
            # The contract's own target (OMNI-51). _put_file still refuses
            # anything that is not SOFT, whoever named it.
            self._put_file(branch, path, artifact, f"{title} (candidate)")
        resp = self._http.post(
            self._api("/pulls"),
            json={"title": title, "head": branch, "base": self._s.default_base,
                  "body": body or "Automated proposal. Human review + merge required."},
        )
        if resp.status_code == 422 and "already exists" in resp.text.lower():
            # L11 (L8's sibling): a retry of the same story — a PR for this head is
            # already open. create_branch already reuses the branch; reuse the PR
            # too instead of dying, so a re-run is idempotent end to end.
            return self._existing_pr(branch, title, artifact, path)
        data = _json(resp)
        pr_id = str(data["number"])
        self._tel.emit("pr.opened", pr_id=pr_id, branch=branch, title=title, path=path)
        return PullRequest(id=pr_id, branch=branch, title=title, artifact=artifact, path=path)

    def _existing_pr(self, branch: str, title: str, artifact: str, path: str) -> PullRequest:
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
                           title=str(prs[0].get("title", title)), artifact=artifact,
                           path=path)

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
        current = existing.json() if existing.status_code == 200 else {}
        sha = current.get("sha")
        if current.get("content") and base64.b64decode(current["content"]).decode() == content:
            # Already there: a feature's last step, written again when its PR
            # opens (OMNI-130). An identical commit would only add noise.
            self._tel.emit("commit.unchanged", branch=branch, path=path)
            return
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        self._http.put(self._api(f"/contents/{path}"), json=payload).raise_for_status()
        self._tel.emit("commit", branch=branch, path=path, message=message)

    def get_pr(self, pr_id: str, *, path: str | None) -> PullRequest:
        response = self._http.get(self._api(f"/pulls/{pr_id}"))
        if response.status_code == 404:
            # Only a definite "no such PR" becomes this (OMNI-126). Any other
            # failure stays an HTTP error: it says nothing about the PR, and a
            # remembered hold must survive an outage rather than be released.
            raise PullRequestNotFound(
                f"{self._s.owner}/{self._s.repo} has no PR #{pr_id}")
        data = _json(response)
        head_ref = str(data["head"]["ref"])
        # GitHub's PR API doesn't carry file contents, but QA re-validates the
        # candidate from pr.artifact — so fetch the caller's file at the head
        # ref (mirrors what open_pr() wrote via _put_file). No path, no fetch:
        # the merge watcher polls for status and needs no content.
        return PullRequest(
            id=str(data["number"]), branch=head_ref, title=str(data["title"]),
            artifact=self._get_file(head_ref, path) if path else "",
            merged=bool(data.get("merged", False)),
            path=path or "",
            closed=data.get("state") == "closed",  # merged or declined (OMNI-57)
        )

    def open_prs(self) -> list[PullRequest]:
        # One page of 100: more open PRs than that against one base is a
        # backlog no hold can fix, and the oldest — the one a hold adopts — is
        # on the first page. Any HTTP failure raises: the caller must treat "we
        # could not ask" as "something may be open" (OMNI-136), never as "none".
        listing = self._http.get(self._api("/pulls"), params={
            "state": "open", "base": self._s.default_base,
            "sort": "created", "direction": "asc", "per_page": 100})
        listing.raise_for_status()
        return [PullRequest(id=str(p["number"]), branch=str(p["head"]["ref"]),
                            title=str(p.get("title", "")))
                for p in cast("list[dict[str, Any]]", listing.json())]

    # Pages of 100 PRs read to learn which branches were ever proposed. A repo
    # with more has outgrown a scratch target; reading part of the list could
    # resume a branch a human declined, so it raises instead.
    _MAX_PR_PAGES = 10

    def unproposed_branches(self, prefix: str) -> list[BranchState]:
        # One call for the loop's branches, one per page of PRs, then a compare
        # only for the (few) branches no PR was opened from. Every HTTP failure
        # raises: the caller starts a fresh feature rather than guess (OMNI-135).
        refs = self._http.get(self._api(f"/git/matching-refs/heads/{prefix}"))
        refs.raise_for_status()
        names = [str(r["ref"]).removeprefix("refs/heads/")
                 for r in cast("list[dict[str, Any]]", refs.json())]
        if not names:
            return []
        proposed: set[str] = set()
        for page in range(1, self._MAX_PR_PAGES + 1):
            listing = self._http.get(self._api("/pulls"), params={
                "state": "all", "per_page": 100, "page": page})
            listing.raise_for_status()
            prs = cast("list[dict[str, Any]]", listing.json())
            proposed.update(str(p["head"]["ref"]) for p in prs)
            if len(prs) < 100:
                break
        else:
            raise RuntimeError(f"more than {self._MAX_PR_PAGES * 100} PRs; "
                               "cannot tell which branches were proposed")
        states = []
        for name in names:
            if name in proposed:
                continue
            compare = self._http.get(self._api(f"/compare/{self._s.default_base}...{name}"))
            compare.raise_for_status()
            data = compare.json()
            states.append(BranchState(
                name=name, behind_by=int(data.get("behind_by", 0)),
                messages=[str(c["commit"]["message"]) for c in data.get("commits", [])]))
        return states

    def write_file(self, branch: str, path: str, content: str, message: str) -> None:
        # _put_file refuses anything that is not a SOFT target, and main.
        if branch == "main":
            raise RequiresHumanApproval("the agent must never commit to main")
        self._put_file(branch, path, content, message)

    def read_file(self, ref: str, path: str) -> str:
        return self._get_file(ref, path)

    def live_target_source(self, path: str) -> str:
        """*path* as merged on the live base branch ("" if absent)."""
        return self._get_file(self._s.default_base, path)

    # Tries before a 5xx is raised (L34): enough to ride out a transient one.
    _READ_ATTEMPTS = 3

    def _get_file(self, ref: str, path: str) -> str:
        """Fetch and decode a file's content at *ref* ("" if absent).

        Only a 404 means absent (L34, OMNI-79). Any failure used to read as "",
        which for a Class-2 feature means "not built yet": a transient 5xx would
        have paid to rebuild a merged feature and opened a second PR for it. A
        5xx is retried; it and any other failure then raise, so the cycle fails
        loudly instead of acting on a file it could not read.
        """
        resp = self._http.get(self._api(f"/contents/{path}"), params={"ref": ref})
        for attempt in range(1, self._READ_ATTEMPTS):
            if resp.status_code < 500:
                break
            time.sleep(attempt)
            resp = self._http.get(self._api(f"/contents/{path}"), params={"ref": ref})
        if resp.status_code == 404:
            return ""
        content = str(_json(resp).get("content", ""))
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
        # See Cloud.promote: the gate is the observed merge, upstream. RealCloud
        # only records deployments, so promotion here is a bookkeeping entry —
        # ServeCloud is what actually moves traffic.
        self._live = version
        self._tel.emit("canary.promoted", version=version, slot="blue")
        return DeployRecord(version=version, slot="blue", live=True)

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


class SNSNotifier:
    """Pages the operator through one SNS topic (OMNI-62).

    SNS rather than SES, decided on the ticket: a topic with an email
    subscription needs no verified sender identity and no SES-sandbox recipient
    list, and the instance role needs only ``sns:Publish`` on this one topic —
    no credential is held here at all, so there is nothing for anything sharing
    the process to read. The region comes from the ARN, so a topic and a
    ``SIS_AWS_REGION`` that disagree cannot publish into the wrong region.
    """

    def __init__(self, topic_arn: str, telemetry: InMemoryTelemetry,
                 client: Any | None = None) -> None:
        parts = topic_arn.split(":")
        if len(parts) != 6 or parts[:3] != ["arn", "aws", "sns"]:
            raise ValueError(f"not an SNS topic ARN: {topic_arn!r}")
        self._arn = topic_arn
        self._tel = telemetry
        if client is None:
            import boto3  # lazy: only needed when paging for real

            client = boto3.client("sns", region_name=parts[3])
        self._sns = client

    def notify(self, severity: Severity, title: str, body: str) -> str:
        response = self._sns.publish(
            TopicArn=self._arn,
            Subject=sns_subject(f"[sis {severity.value}] {title}"),
            Message=body,
        )
        delivery = str(response["MessageId"])
        self._tel.emit("notify.sent", id=delivery, severity=severity.value, title=title)
        return delivery

    def subscriptions(self) -> tuple[int, int]:
        """Read-only: ``(confirmed, pending)`` subscriptions on the topic."""
        attrs = self._sns.get_topic_attributes(TopicArn=self._arn)["Attributes"]
        return (int(attrs.get("SubscriptionsConfirmed", 0)),
                int(attrs.get("SubscriptionsPending", 0)))

    def check(self) -> str:
        """Preflight: the topic exists, is visible, and reaches someone.

        Raises when no subscription is confirmed (OMNI-122). The first AWS run's
        preflight printed a tick next to "0 confirmed subscription(s)", and the
        breaker-trip page that followed was accepted by SNS and delivered to
        nobody.
        """
        confirmed, pending = self.subscriptions()
        if problem := pager_problem(confirmed, pending):
            raise RuntimeError(problem)
        return f"{self._arn} ({confirmed} confirmed subscription(s))"


def pager_problem(confirmed: int, pending: int) -> str | None:
    """Why a topic with these subscription counts pages nobody, or None. Pure."""
    if confirmed > 0:
        return None
    waiting = (f"{pending} subscription(s) await confirmation — click the link AWS "
               "mailed to alert_email" if pending
               else "nothing is subscribed — check infra/aws's alert_email")
    return (f"the pager topic has no confirmed subscription, so every page is "
            f"delivered to nobody: {waiting}")


def sns_subject(text: str) -> str:
    """What SNS accepts as an email Subject. Pure.

    ASCII, no line breaks or control characters, under 100 characters — a
    publish with anything else is rejected, and the page never goes out. A
    title can carry a brake reason or a gauntlet message, so it is cleaned
    here rather than trusted.
    """
    flat = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in text)
    return " ".join(flat.split())[:99] or "sis alert"


# --- Sensor: EIA's weekly petroleum data (OMNI-33) ---------------------------------
#
# Read from the bulk file, not API v2. One file holds every series' whole
# history, needs no key, and had each week's prices and stocks within hours of
# their release (last_updated 2026-10-06 and 2026-10-07). API v2 serves the same
# values (compared for 2022-09-23..2026-10-02) but needs a key and pages: one
# path suffices. EIA's terms (checked by Olaf, 2026-10-13): public domain, reuse
# with an acknowledgment that dates the publication, no implied endorsement, and
# no modified content presented as EIA's.

EIA_BULK_URL = "https://www.eia.gov/opendata/bulk/PET.zip"
EIA_TRACE = SENSOR_TRACES_DIR / "eia.jsonl"
EASTERN = ZoneInfo("America/New_York")
_EIA_HOSTS = ("https://www.eia.gov/", "https://api.eia.gov/")
_EIA_MEMBER = "PET.txt"
_EIA_MAX_ZIPPED = 250_000_000       # 56 MB on 2026-10-13
_EIA_MAX_UNZIPPED = 1_500_000_000   # 368 MB unpacked
_EIA_MAX_LINE = 4_000_000           # the longest line was 210 KB
_SERIES_PREFIX = b'{"series_id":"'
_PERIOD = re.compile(r"\d{8}\Z")


@dataclass(frozen=True)
class EIASeries:
    """A series the EIA sensor reads, and when a value of it is taken to be out.

    A value fetched long after it appeared gets a reconstructed ``known_at``:
    its period's date plus ``release_days``, at ``release_at`` Eastern. That is
    after the regular release and its holiday shifts, and before an irregular
    delay: the stocks for the week ending 2022-06-17 came out on 2022-06-29, and
    the retail prices for Monday 2022-06-20 on the same day (EIA's systems
    outage), the stocks for the week ending 2025-12-19 on 2025-12-29 at 5 p.m. A
    replay over a window like those checks EIA's release archive first
    (OMNI-35); the reconstructed mark says where to look.
    """

    rule: SeriesRule
    release_days: int
    release_at: datetime.time


def _weekly_stocks() -> EIASeries:
    # Weekly Petroleum Status Report: the week ending Friday, out on Wednesday
    # at 10:30 ET, or Thursday at noon in a holiday week. Taken as Thursday 5 p.m.
    return EIASeries(SeriesRule("Thousand Barrels", 10_000.0, 60_000.0,
                                period=datetime.timedelta(days=7)), 6, datetime.time(17))


def _weekly_retail() -> EIASeries:
    # Gasoline and Diesel Fuel Update: Monday's price, out on Monday, or Tuesday
    # after a Monday holiday. The hour is not verified; the bulk file had the
    # 2026-10-05 prices by Tuesday 13:46 ET. Taken as Tuesday 5 p.m.
    return EIASeries(SeriesRule("Dollars per Gallon", 0.5, 15.0), 1, datetime.time(17))


# EIA's own ids, as the bulk file writes them. A human adds a series here, with
# its range and its release; one the adapter did not declare is refused.
EIA_SERIES: Mapping[str, EIASeries] = {
    "PET.WGTSTP51.W": _weekly_stocks(),                 # West Coast (PADD 5) total gasoline stocks
    "PET.EMM_EPMRR_PTE_SCA_DPG.W": _weekly_retail(),    # California, regular reformulated, retail
    "PET.EMM_EPMRR_PTE_Y05LA_DPG.W": _weekly_retail(),  # Los Angeles
    "PET.EMM_EPMRR_PTE_Y05SF_DPG.W": _weekly_retail(),  # San Francisco
    "PET.EMM_EPMRR_PTE_NUS_DPG.W": _weekly_retail(),    # United States
}
EIA_RULES = SourceRules("eia", {series: spec.rule for series, spec in EIA_SERIES.items()})


class EIAFormatError(ValueError):
    """The bulk file is not shaped as the sensor was written for. Nothing is captured."""


class EIAFetchError(RuntimeError):
    """The bulk file could not be fetched, or what came back is not EIA's file."""


class _Transient(OSError):
    """A failure worth another attempt: a 429 or a 5xx. (Network errors are OSErrors too.)"""


@dataclass(frozen=True)
class EIABulk:
    """A downloaded bulk file: where it is, where it came from, and its digest."""

    path: pathlib.Path
    url: str
    sha256: str
    size: int


def eia_period(raw: object) -> object:
    """The instant a bulk-file period names, or *raw* as sent if it names none.

    EIA states a date (``20220930``), not an instant, and stocks are "as of the
    end of the period". The sensor places a date at midnight Eastern: a choice,
    written down here, not a fact from the source.
    """
    if isinstance(raw, str) and _PERIOD.match(raw):
        try:
            return datetime.datetime(int(raw[:4]), int(raw[4:6]), int(raw[6:]), tzinfo=EASTERN)
        except ValueError:
            return raw
    return raw


def eia_published(series: str, period: datetime.datetime) -> datetime.datetime:
    """When a value of *series* for *period* is taken to have been out (:class:`EIASeries`)."""
    spec = EIA_SERIES[series]
    day = period.astimezone(EASTERN).date() + datetime.timedelta(days=spec.release_days)
    return datetime.datetime.combine(day, spec.release_at, tzinfo=EASTERN)


def eia_attribution(updated: Mapping[str, str | None], fetched_at: datetime.datetime) -> str:
    """The acknowledgment EIA's reuse policy asks for, dated by the data's last update."""
    dates = [parse_event_time(value, where="last_updated") for value in updated.values() if value]
    when = max(dates) if dates else fetched_at
    return f"Source: U.S. Energy Information Administration ({when:%b %Y})"


def _instant_or_none(raw: object) -> str | None:
    if not isinstance(raw, str) or len(raw) > 40:
        return None
    try:
        return parse_event_time(raw, where="last_updated").isoformat()
    except ValueError:
        return None


def _lines(stream: IO[bytes]) -> Iterator[tuple[bytes, bool]]:
    """Each line of *stream*, and whether it was whole. One over the limit is skipped."""
    while line := stream.readline(_EIA_MAX_LINE + 1):
        if len(line) <= _EIA_MAX_LINE or line.endswith(b"\n"):
            yield line, True
            continue
        head = line
        while line and not line.endswith(b"\n"):
            line = stream.readline(_EIA_MAX_LINE + 1)
        yield head, False


def _eia_series(series: str, line: bytes) -> tuple[Fetched, str | None]:
    try:
        decoded = json.loads(line)
    except (ValueError, RecursionError):
        raise EIAFormatError(f"{series}: its line is not JSON") from None
    if not isinstance(decoded, dict) or decoded.get("series_id") != series \
            or not isinstance(decoded.get("data"), list):
        raise EIAFormatError(f"{series}: not a series with a list of values")
    values = [(eia_period(pair[0]), pair[1]) if isinstance(pair, list) and len(pair) == 2
              else (pair, None) for pair in decoded["data"]]
    return (Fetched(series, decoded.get("units"), values),
            _instant_or_none(decoded.get("last_updated")))


def read_eia_bulk(path: pathlib.Path) -> tuple[list[Fetched], dict[str, str | None]]:
    """The declared series in a bulk file, and when the file says each was last updated.

    Streamed: only a declared series' line is decoded, and nothing over the
    limits above is read. Values go to the door as sent; only periods are
    placed in time here (:func:`eia_period`). A declared series the file lacks
    is simply absent from the result, and the capture says so.
    """
    found: dict[str, Fetched] = {}
    updated: dict[str, str | None] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                info = archive.getinfo(_EIA_MEMBER)
            except KeyError:
                raise EIAFormatError(f"the bulk file has no {_EIA_MEMBER}") from None
            # Only deflate (what EIA ships) or no compression: the other methods
            # decompress a read without bounding its output, so a declared size
            # that lies would be found out only after the memory was gone.
            if info.compress_type not in (zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED):
                raise EIAFormatError(f"{_EIA_MEMBER} uses compression method "
                                     f"{info.compress_type}, not deflate")
            if info.file_size > _EIA_MAX_UNZIPPED:
                raise EIAFormatError(f"{_EIA_MEMBER} would unpack to {info.file_size} bytes, "
                                     f"over {_EIA_MAX_UNZIPPED}")
            with archive.open(info) as member:
                for line, whole in _lines(member):
                    if not line.startswith(_SERIES_PREFIX):
                        continue
                    end = line.find(b'"', len(_SERIES_PREFIX))
                    series = line[len(_SERIES_PREFIX):end].decode("ascii", "replace")
                    if end < 0 or series not in EIA_SERIES:
                        continue
                    if not whole:
                        raise EIAFormatError(f"{series}: its line is over {_EIA_MAX_LINE} bytes")
                    if series in found:
                        raise EIAFormatError(f"{series} appears twice")
                    found[series], updated[series] = _eia_series(series, line)
    except (zipfile.BadZipFile, EOFError) as exc:
        raise EIAFormatError(f"the bulk file is not a readable zip archive ({exc})") from None
    return list(found.values()), updated


def _download(session: Any, directory: pathlib.Path) -> EIABulk:
    with session.get(EIA_BULK_URL, stream=True, timeout=_http_timeout()) as response:
        status = int(response.status_code)
        if status == 429 or status >= 500:
            raise _Transient(f"HTTP {status}")
        if status != 200:
            raise EIAFetchError(f"HTTP {status} for {EIA_BULK_URL}")
        url = str(response.url)
        if not url.startswith(_EIA_HOSTS):
            raise EIAFetchError("the bulk file was redirected off EIA's hosts")
        declared = str(response.headers.get("Content-Length", ""))
        if declared.isdigit() and int(declared) > _EIA_MAX_ZIPPED:
            raise EIAFetchError(f"the bulk file is {declared} bytes, over {_EIA_MAX_ZIPPED}")
        digest, size = hashlib.sha256(), 0
        path = directory / "PET.zip"
        try:
            out = open(path, "wb")
        except OSError as exc:      # here, not EIA's: not worth another download
            raise EIAFetchError(f"cannot write the download: {exc.strerror}") from exc
        with out:
            for chunk in response.iter_content(chunk_size=1 << 20):
                size += len(chunk)
                if size > _EIA_MAX_ZIPPED:
                    raise EIAFetchError(f"the bulk file is over {_EIA_MAX_ZIPPED} bytes")
                digest.update(chunk)
                try:
                    out.write(chunk)
                except OSError as exc:
                    raise EIAFetchError(f"cannot write the download: {exc.strerror}") from exc
    return EIABulk(path, url, digest.hexdigest(), size)


def fetch_eia_bulk(directory: pathlib.Path, *, session: Any = None, attempts: int = 3,
                   sleep: Callable[[float], None] = time.sleep) -> EIABulk:
    """Download the bulk file into *directory*, retrying what may pass on its own.

    A dropped connection, a timeout, a 429 or a 5xx is tried again after 2 s,
    then 4 s. Anything else fails at once: a 404, a redirect off EIA's hosts, a
    file over the size limit or a local disk that will not take it will not
    improve by waiting.
    """
    transient: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError, _Transient)
    try:
        import requests

        transient += (requests.ConnectionError, requests.Timeout,
                      requests.exceptions.ChunkedEncodingError)
    except ImportError:             # only a test's fake session gets this far without it
        if session is None:
            raise
    if session is None:
        session = requests.Session()
    for attempt in range(1, attempts + 1):
        try:
            return _download(session, directory)
        except transient as exc:
            if attempt == attempts:
                raise EIAFetchError(f"the EIA bulk file failed {attempts} times; last: "
                                    f"{type(exc).__name__}: {str(exc)[:160]}") from exc
            sleep(2.0 ** attempt)
    raise EIAFetchError("no attempt was made")


class EIASensor:
    """EIA's weekly petroleum data (OMNI-33): captured from the bulk file, read from the trace.

    Two steps on purpose. :meth:`capture` fetches once and appends what is new
    or changed (``scripts/capture_sensor.py``). :meth:`read` answers from the
    trace alone, through the door again, so a read never waits on a download
    and a replay sees exactly what a live read saw. Nothing captured yet reads
    as nothing: an empty answer, like a week with no release, not an error.

    A read shows readings, and a reading cannot say "withdrawn": a value EIA
    later blanks stays readable as the vintage it was. :meth:`absent` shows the
    blanking, a stated absence with its own ``known_at``, so whoever builds an
    as-of view (OMNI-35) takes both. A period EIA stops sending altogether is
    counted as ``vanished`` in its capture.
    """

    def __init__(self, trace: pathlib.Path = EIA_TRACE, *,
                 fetch: Callable[[pathlib.Path], EIABulk] = fetch_eia_bulk,
                 clock: Clock | None = None) -> None:
        self.trace = sensor_trace.writable(trace)
        self._fetch = fetch
        self._clock = clock or WallClock()

    def capture(self) -> sensor_trace.CaptureReport:
        """Fetch the bulk file once, and append to the trace what is new or changed."""
        with tempfile.TemporaryDirectory(prefix="sis-eia-") as scratch:
            bulk = self._fetch(pathlib.Path(scratch))
            fetched, updated = read_eia_bulk(bulk.path)

        def block(prior: list[dict[str, Any]]) -> list[dict[str, Any]]:
            # Stamped under the trace's lock, after the download: a later stamp
            # is still an upper bound on when the values were known, and two
            # captures at once then append in the order they were stamped. An
            # earlier stamp that reached the lock second was refused as if the
            # clock had gone back (OMNI-160).
            fetched_at = self._clock.now()
            stamp = fetched_at.astimezone(datetime.UTC)
            provenance = {"url": bulk.url, "sha256": bulk.sha256, "bytes": bulk.size,
                          "member": _EIA_MEMBER, "last_updated": updated,
                          "attribution": eia_attribution(updated, fetched_at)}
            return sensor_trace.capture_lines(
                prior, fetched, EIA_RULES,
                capture=f"eia-{stamp:%Y%m%dT%H%M%S.%fZ}-{bulk.sha256[:8]}",
                fetched_at=fetched_at, published=eia_published, provenance=provenance)

        return sensor_trace.CaptureReport.of(sensor_trace.append(self.trace, block))

    def read(
        self, start: datetime.datetime, end: datetime.datetime, *, by: TimeAxis,
    ) -> list[Reading]:
        readings, _ = sensor_trace.read_back(sensor_trace.load(self.trace), EIA_RULES)
        return SimSensor(readings).read(start, end, by=by)

    def absent(
        self, start: datetime.datetime, end: datetime.datetime, *, by: TimeAxis,
    ) -> list[Absent]:
        """The periods EIA stated no value for, whose time on *by* is in ``[start, end)``.

        Ordered as :meth:`read` orders readings. A value EIA published and later
        blanked appears in :meth:`read` at its first ``known_at`` and here at
        the later one: after that, there is no value.
        """
        _, absent = sensor_trace.read_back(sensor_trace.load(self.trace), EIA_RULES)
        if start.utcoffset() is None or end.utcoffset() is None:
            raise ValueError("EIASensor.absent: a window needs timezones, as the trace has")
        other = TimeAxis.EVENT if by is TimeAxis.KNOWN else TimeAxis.KNOWN

        def at(item: Absent, axis: TimeAxis) -> datetime.datetime:
            return item.known_at if axis is TimeAxis.KNOWN else item.event_time
        return sorted((item for item in absent if start <= at(item, by) < end),
                      key=lambda item: (at(item, by), at(item, other), item.source, item.series))


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
