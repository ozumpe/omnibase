"""Tests for the real REST adapters that don't need a live tenant or `requests`.

The `requests`-backed session is built lazily (`_session`), so these construct
the adapter without `__init__` and inject a fake HTTP client. This verifies the
wire behaviour (endpoints, request bodies, parsing) with recorded responses.
"""

import base64
from typing import Any

from sis.adapters import InMemoryTelemetry
from sis.adapters_real import GitHubVersionControl, JiraWorkTracker
from sis.ports import IssueStatus
from sis.settings import AtlassianSettings, GitHubSettings


class _Resp:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeHttp:
    """Records calls; returns canned responses for the search POST and issue GETs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def post(self, url: str, json: Any = None) -> _Resp:
        self.calls.append(("POST", url, json))
        return _Resp({"issues": [{"key": "SD-2"}, {"key": "SD-3"}]})

    def get(self, url: str, params: Any = None) -> _Resp:
        self.calls.append(("GET", url, params))
        key = url.rsplit("/", 1)[-1]
        return _Resp({
            "key": key,
            "fields": {"summary": f"summary of {key}",
                       "status": {"name": "To Do"},
                       "issuetype": {"name": "Story"}},
        })


def _tracker() -> tuple[JiraWorkTracker, _FakeHttp]:
    jt = object.__new__(JiraWorkTracker)  # bypass __init__ (which builds a requests session)
    jt._s = AtlassianSettings(base_url="https://x.atlassian.net", email="a@b.c",
                              api_token="tok", jira_project="SD")
    jt._tel = InMemoryTelemetry()
    http = _FakeHttp()
    jt._http = http
    return jt, http


def test_children_uses_enhanced_search_endpoint() -> None:
    jt, http = _tracker()
    issues = jt.children("SD-1")

    # H2 regression: must POST to /search/jql (not the removed GET /search).
    post_calls = [c for c in http.calls if c[0] == "POST"]
    assert len(post_calls) == 1
    _, url, body = post_calls[0]
    assert url.endswith("/rest/api/3/search/jql")
    assert body["jql"] == 'parent = "SD-1"'

    # Keys resolved to full issues via get_issue().
    assert [i.id for i in issues] == ["SD-2", "SD-3"]
    assert issues[0].status is IssueStatus.TODO
    assert issues[0].summary == "summary of SD-2"


def test_children_handles_empty_result() -> None:
    jt, http = _tracker()

    def _empty_post(url: str, json: Any = None) -> _Resp:
        http.calls.append(("POST", url, json))
        return _Resp({"issues": []})

    http.post = _empty_post  # type: ignore[method-assign]
    assert jt.children("SD-9") == []


class _FakeGitHub:
    """Serves a PR GET and a Contents GET (the file at the PR head ref)."""

    def __init__(self, artifact: str) -> None:
        self._artifact = artifact
        self.calls: list[tuple[str, str, Any]] = []

    def get(self, url: str, params: Any = None) -> _Resp:
        self.calls.append(("GET", url, params))
        if "/pulls/" in url:
            return _Resp({"number": 7, "head": {"ref": "feature/story-3"},
                          "title": "Optimise target", "merged": False})
        if "/contents/" in url:
            encoded = base64.b64encode(self._artifact.encode()).decode()
            return _Resp({"content": encoded})
        return _Resp({}, status_code=404)


def _github() -> tuple[GitHubVersionControl, _FakeGitHub]:
    gh = object.__new__(GitHubVersionControl)  # bypass __init__ (builds a session)
    gh._s = GitHubSettings(token="tok", owner="o", repo="r")
    gh._tel = InMemoryTelemetry()
    http = _FakeGitHub("OPTIMISED SOURCE")
    gh._http = http
    return gh, http


def test_get_pr_populates_artifact_from_head_ref() -> None:
    gh, http = _github()
    pr = gh.get_pr("7")

    # H2 regression: GitHub's PR API doesn't carry file contents, so get_pr must
    # fetch the candidate from the head ref — otherwise QA re-validates an empty
    # artifact and rejects every real cycle.
    assert pr.artifact == "OPTIMISED SOURCE"
    assert pr.branch == "feature/story-3"

    content_calls = [c for c in http.calls if "/contents/" in c[1]]
    assert len(content_calls) == 1
    _, url, params = content_calls[0]
    assert url.endswith("/contents/runtime/target.py")  # TARGET_REPO_PATH
    assert params == {"ref": "feature/story-3"}  # at the PR head, not main


def test_get_pr_artifact_empty_when_file_absent() -> None:
    gh, http = _github()

    def _no_file(url: str, params: Any = None) -> _Resp:
        http.calls.append(("GET", url, params))
        if "/pulls/" in url:
            return _Resp({"number": 7, "head": {"ref": "feature/x"},
                          "title": "t", "merged": False})
        return _Resp({}, status_code=404)  # contents missing

    http.get = _no_file  # type: ignore[method-assign]
    # A 404 on the file must not raise — artifact is simply empty.
    assert gh.get_pr("7").artifact == ""


def test_live_target_source_reads_the_base_branch() -> None:
    # A cycle following a merge must start from the merged target, so
    # live_target_source fetches the file at the default base (main), not a
    # feature ref — otherwise it would keep re-proposing the stale source.
    gh, http = _github()
    gh._s = GitHubSettings(token="tok", owner="o", repo="r", default_base="main")

    assert gh.live_target_source() == "OPTIMISED SOURCE"

    content_calls = [c for c in http.calls if "/contents/" in c[1]]
    assert len(content_calls) == 1
    _, url, params = content_calls[0]
    assert url.endswith("/contents/runtime/target.py")  # TARGET_REPO_PATH
    assert params == {"ref": "main"}  # the live base, not a feature branch


def test_live_target_source_empty_when_base_has_no_target() -> None:
    gh, _ = _github()

    def _no_file(url: str, params: Any = None) -> _Resp:
        return _Resp({}, status_code=404)

    gh._http.get = _no_file  # type: ignore[method-assign]
    # No target on the base yet (first cycle) — empty, so the SWE uses the local file.
    assert gh.live_target_source() == ""
