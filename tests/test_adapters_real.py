"""Tests for the real REST adapters that don't need a live tenant or `requests`.

The `requests`-backed session is built lazily (`_session`), so these construct
the adapter without `__init__` and inject a fake HTTP client. This verifies the
wire behaviour (endpoints, request bodies, parsing) with recorded responses.
"""

from typing import Any

from sis.adapters import InMemoryTelemetry
from sis.adapters_real import JiraWorkTracker
from sis.ports import IssueStatus
from sis.settings import AtlassianSettings


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

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
