"""Tests for the real REST adapters that don't need a live tenant or `requests`.

The `requests`-backed session is built lazily (`_session`), so these construct
the adapter without `__init__` and inject a fake HTTP client. This verifies the
wire behaviour (endpoints, request bodies, parsing) with recorded responses.
"""

import base64
from typing import Any

import pytest

from sis.adapters import InMemoryTelemetry
from sis.adapters_real import (
    DEFAULT_HTTP_TIMEOUT,
    ConfluenceDocumentStore,
    GitHubVersionControl,
    JiraWorkTracker,
    RealCloud,
    _http_timeout,
    _TimeoutHTTP,
)
from sis.ports import Cloud, IssueStatus, RequiresHumanApproval
from sis.settings import AtlassianSettings, GitHubSettings


class _Resp:
    def __init__(self, payload: dict[str, Any], status_code: int = 200,
                 text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

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


def test_children_rejects_a_parent_that_is_not_an_issue_key() -> None:
    # /search/jql takes JQL as a string with no parameter binding, and children()
    # interpolates the parent key straight into it. Today's callers only pass
    # internal keys — but that is a property of the callers, not an enforced one,
    # and Confluence intake exists precisely to let outside text into the org.
    # Validate at the boundary, and before any request goes out.
    jt, http = _tracker()
    with pytest.raises(ValueError, match="not a valid Jira issue key"):
        jt.children('SD-1" OR project = "SECRET')
    assert http.calls == []


def test_transition_survives_a_failed_comment() -> None:
    # Regression (2026-07-28 minor list): the comment POST was chained onto the
    # transition with raise_for_status(), so a 500 on the *comment* raised after
    # the transition had already been applied — and could not be undone. The
    # caller saw the whole transition fail and retried, but the issue had moved,
    # so the retry found no matching transition and hard-failed the cycle. An
    # audit note must never cost the state change it annotates.
    jt, http = _tracker()

    def _post(url: str, json: Any = None) -> _Resp:
        http.calls.append(("POST", url, json))
        if url.endswith("/comment"):
            raise RuntimeError("Jira 500 on comment")
        return _Resp({})

    def _get(url: str, params: Any = None) -> _Resp:
        http.calls.append(("GET", url, params))
        if url.endswith("/transitions"):
            return _Resp({"transitions": [{"id": "31", "to": {"name": "In Progress"}}]})
        return _Resp({"key": "SD-1",
                      "fields": {"summary": "s", "status": {"name": "In Progress"},
                                 "issuetype": {"name": "Story"}}})

    http.post = _post  # type: ignore[method-assign]
    http.get = _get  # type: ignore[method-assign]

    issue = jt.transition("SD-1", IssueStatus.IN_PROGRESS, comment="picked up")

    # The state change stands, and the lost comment is visible in telemetry
    # rather than silently swallowed.
    assert issue.status is IssueStatus.IN_PROGRESS
    emitted = [e["event"] for e in jt._tel.events()]
    assert "issue.comment_failed" in emitted
    assert "issue.transition" in emitted


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


class _FakeConfluence:
    """Simulates a tenant where the page title is already taken (a re-run)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def post(self, url: str, json: Any = None) -> _Resp:
        self.calls.append(("POST", url, json))
        return _Resp(
            {"errors": [{"status": 400, "code": "BAD_REQUEST"}]}, status_code=400,
            text="A page with this title already exists: A page already exists "
                 "with the same TITLE in this space",
        )

    def get(self, url: str, params: Any = None) -> _Resp:
        self.calls.append(("GET", url, params))
        if url.endswith("/spaces"):
            return _Resp({"results": [{"id": 999, "key": "TESTRUN"}]})
        if url.endswith("/spaces/999/pages"):
            return _Resp({"results": [{"id": 42, "title": "Project Charter"}]})
        if url.endswith("/pages/42"):
            return _Resp({"id": 42, "title": "Project Charter",
                          "version": {"number": 3}})
        return _Resp({}, status_code=404)

    def put(self, url: str, json: Any = None) -> _Resp:
        self.calls.append(("PUT", url, json))
        return _Resp({"id": 42})


def _docs() -> tuple[ConfluenceDocumentStore, _FakeConfluence]:
    docs = object.__new__(ConfluenceDocumentStore)  # bypass __init__ (builds a session)
    docs._s = AtlassianSettings(base_url="https://x.atlassian.net", email="a@b.c",
                                api_token="tok", jira_project="SD")
    docs._tel = InMemoryTelemetry()
    docs._space_ids = {}
    http = _FakeConfluence()
    docs._http = http
    return docs, http


def test_create_page_reuses_existing_page_on_duplicate_title() -> None:
    # Level-2 regression: Confluence enforces unique titles per space, so a
    # second run against a live tenant 400s on every fixed-title page (the
    # charter was the first casualty). create_page must fall back to updating
    # the existing page instead of crashing the cycle.
    docs, http = _docs()
    page = docs.create_page("TESTRUN", "Project Charter", "new charter text",
                            labels=["charter"])

    assert page.id == "42"
    assert page.body == "new charter text"

    put_calls = [c for c in http.calls if c[0] == "PUT"]
    assert len(put_calls) == 1
    _, url, body = put_calls[0]
    assert url.endswith("/pages/42")
    assert body["version"] == {"number": 4}  # bumped past the live version 3
    assert body["body"]["value"] == "new charter text"


def test_create_page_drops_cross_space_parent_on_404() -> None:
    # Level-2 regression: Confluence cannot parent a page across spaces (the
    # spec page in the spec space pointed at its proposal in the intake space)
    # and 404s on the create. The adapter must retry without the parent — the
    # provenance link is tracked in the SelfModel, not the page tree.
    docs, http = _docs()

    def _post(url: str, json: Any = None) -> _Resp:
        http.calls.append(("POST", url, dict(json)))  # snapshot: adapter mutates payload
        if "parentId" in json:
            return _Resp({"errors": [{"status": 404, "code": "NOT_FOUND"}]},
                         status_code=404,
                         text="Cannot find content with id [5406897] in space key [999]")
        return _Resp({"id": 77})

    http.post = _post  # type: ignore[method-assign]
    page = docs.create_page("TESTRUN", "Spec — X", "spec body", parent_id="5406897")

    assert page.id == "77"
    post_calls = [c for c in http.calls if c[0] == "POST"]
    assert len(post_calls) == 2
    assert "parentId" in post_calls[0][2]
    assert "parentId" not in post_calls[1][2]


def test_create_page_skips_version_bump_when_body_unchanged() -> None:
    # L2: on a re-run the duplicate-title fallback must NOT PUT a new version if
    # the stored body already matches — otherwise fixed-title pages churn a new
    # version every cycle.
    docs, http = _docs()

    def _get(url: str, params: Any = None) -> _Resp:
        http.calls.append(("GET", url, params))
        if url.endswith("/spaces"):
            return _Resp({"results": [{"id": 999, "key": "TESTRUN"}]})
        if url.endswith("/spaces/999/pages"):
            return _Resp({"results": [{"id": 42, "title": "Project Charter"}]})
        if url.endswith("/pages/42"):
            return _Resp({"id": 42, "version": {"number": 3},
                          "body": {"storage": {"value": "same charter text"}}})
        return _Resp({}, status_code=404)

    http.get = _get  # type: ignore[method-assign]
    page = docs.create_page("TESTRUN", "Project Charter", "same charter text")

    assert page.id == "42"
    assert not [c for c in http.calls if c[0] == "PUT"]  # no version bump
    assert any(e["event"] == "page.unchanged" for e in docs._tel.events())


def test_create_branch_reuses_existing_branch_on_422() -> None:
    # L8: a retry of the same story hits GitHub's "Reference already exists"
    # (422). create_branch must reuse the branch, not raise.
    gh, _ = _github()

    def _get(url: str, params: Any = None) -> _Resp:
        return _Resp({"object": {"sha": "abc123"}})  # base ref

    def _post(url: str, json: Any = None) -> _Resp:
        return _Resp({"message": "Reference already exists"}, status_code=422,
                     text="Reference already exists")

    gh._http.get = _get  # type: ignore[method-assign]
    gh._http.post = _post  # type: ignore[attr-defined]
    branch = gh.create_branch("feature/tes-9", base="main")

    assert branch.name == "feature/tes-9" and branch.base == "main"
    assert any(e["event"] == "branch.exists" for e in gh._tel.events())


# --- M6: every real-adapter call carries a timeout ------------------------


class _RecordingSession:
    """Stands in for a requests.Session; records the kwargs of each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kw: Any) -> None:
        self.calls.append(("get", kw))

    def post(self, url: str, **kw: Any) -> None:
        self.calls.append(("post", kw))

    def put(self, url: str, **kw: Any) -> None:
        self.calls.append(("put", kw))


def test_timeout_http_injects_a_default_timeout() -> None:
    # M6: requests has no default timeout, so the wrapper must add one to every
    # verb — otherwise a hung tenant API blocks the cycle forever.
    rec = _RecordingSession()
    http = _TimeoutHTTP(rec, 12.5)  # type: ignore[arg-type]
    http.get("u")
    http.post("u", json={"x": 1})
    http.put("u", json={"x": 1})
    assert [kw["timeout"] for _, kw in rec.calls] == [12.5, 12.5, 12.5]


def test_timeout_http_respects_an_explicit_timeout() -> None:
    # An explicit per-call timeout wins over the default.
    rec = _RecordingSession()
    http = _TimeoutHTTP(rec, 12.5)  # type: ignore[arg-type]
    http.get("u", timeout=1.0)
    assert rec.calls[0][1]["timeout"] == 1.0


def test_http_timeout_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIS_HTTP_TIMEOUT", raising=False)
    assert _http_timeout() == DEFAULT_HTTP_TIMEOUT
    monkeypatch.setenv("SIS_HTTP_TIMEOUT", "5")
    assert _http_timeout() == 5.0


def test_http_timeout_rejects_bad_values(monkeypatch: pytest.MonkeyPatch) -> None:
    # A garbled/non-positive timeout must fail loudly, never revert to "forever".
    monkeypatch.setenv("SIS_HTTP_TIMEOUT", "soon")
    with pytest.raises(ValueError, match="SIS_HTTP_TIMEOUT"):
        _http_timeout()
    monkeypatch.setenv("SIS_HTTP_TIMEOUT", "0")
    with pytest.raises(ValueError, match="must be positive"):
        _http_timeout()


def test_create_page_writes_labels_via_v1_endpoint() -> None:
    # L1: labels the roles tag pages with (charter/spec/proposal/outline) are
    # now written — via the v1 content-label endpoint (v2 has no label write).
    docs, http = _docs()
    posts: list[tuple[str, Any]] = []

    def _post(url: str, json: Any = None) -> _Resp:
        posts.append((url, json))
        return _Resp({"id": 77}) if url.endswith("/pages") else _Resp({})

    http.post = _post  # type: ignore[method-assign]
    page = docs.create_page("TESTRUN", "Project Charter", "body",
                            labels=["charter", "gov"])

    assert page.id == "77"
    label_posts = [p for p in posts if p[0].endswith("/content/77/label")]
    assert len(label_posts) == 1
    _, body = label_posts[0]
    assert body == [{"prefix": "global", "name": "charter"},
                    {"prefix": "global", "name": "gov"}]
    assert any(e["event"] == "page.labels_applied" for e in docs._tel.events())


def test_create_page_survives_a_label_failure() -> None:
    # Labels are cosmetic — a failing label API must not break the cycle.
    docs, http = _docs()

    def _post(url: str, json: Any = None) -> _Resp:
        if url.endswith("/pages"):
            return _Resp({"id": 77})
        raise RuntimeError("label API unavailable")

    http.post = _post  # type: ignore[method-assign]
    page = docs.create_page("TESTRUN", "Project Charter", "body", labels=["charter"])

    assert page.id == "77"  # cycle survived despite the label failure
    assert any(e["event"] == "page.labels_failed" for e in docs._tel.events())


# --- L11: a same-story retry reuses the existing open PR (L8's sibling) ----


def test_open_pr_reuses_existing_pr_on_422() -> None:
    # create_branch already reuses the branch on a retry (L8); open_pr must reuse
    # the PR too — GitHub 422s "a pull request already exists" for the head.
    gh, _ = _github()

    def _post(url: str, json: Any = None) -> _Resp:
        return _Resp({"message": "already exists"}, status_code=422,
                     text="A pull request already exists for o:feature/tes-9.")

    def _get(url: str, params: Any = None) -> _Resp:
        if url.endswith("/pulls"):
            assert params == {"head": "o:feature/tes-9", "state": "open"}
            return _Resp([{"number": 9, "title": "Optimise target (TES-9)"}])  # type: ignore[arg-type]
        return _Resp({}, status_code=404)

    gh._http.post = _post  # type: ignore[attr-defined]
    gh._http.get = _get    # type: ignore[method-assign]
    pr = gh.open_pr("feature/tes-9", "Optimise target (TES-9)")

    assert pr.id == "9"
    assert any(e["event"] == "pr.exists" for e in gh._tel.events())


# --- L14: the GitHub write boundary allows only SOFT target(s) -------------


def test_put_file_refuses_non_target_paths() -> None:
    # L14: _put_file is the last-line write guard. It must refuse a STRICT engine
    # path *and* a FORBIDDEN guardrail path — not just FORBIDDEN — before any API
    # call (defence in depth beyond the SWE's authorize_change).
    gh, http = _github()

    with pytest.raises(RequiresHumanApproval):
        gh._put_file("feature/x", "sis/org.py", "code", "msg")       # STRICT
    with pytest.raises(RequiresHumanApproval):
        gh._put_file("feature/x", "sis/gauntlet.py", "code", "msg")  # FORBIDDEN

    # The refusal happens before any HTTP call.
    assert not http.calls


# --- The Cloud port: both adapters must satisfy it, always -----------------


def test_real_cloud_still_satisfies_the_cloud_port() -> None:
    # Cloud is @runtime_checkable, so growing the port (shift_traffic /
    # live_metrics, docs/SERVE_CANARY.md step 7) silently drops any adapter that
    # doesn't grow with it. RealCloud is the one that gets forgotten — it lives
    # in a different module from InMemoryCloud, which already had a conformance
    # test. This is that test for the other side.
    assert isinstance(RealCloud(InMemoryTelemetry()), Cloud)


def test_real_cloud_refuses_to_fake_a_canary() -> None:
    # RealCloud records deployments; it has no traffic to split and no metrics
    # source. It must say so loudly rather than no-op — a silent no-op would let
    # a real run report a "passing canary" that never routed a request or
    # measured anything, which is the exact failure a canary exists to prevent.
    cloud = RealCloud(InMemoryTelemetry())
    with pytest.raises(NotImplementedError, match="ServeCloud"):
        cloud.shift_traffic("v1", 0.05)
    with pytest.raises(NotImplementedError, match="ServeCloud"):
        cloud.live_metrics("v1", 60.0)
