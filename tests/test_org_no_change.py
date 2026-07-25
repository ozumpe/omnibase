"""A "no change" cycle is benign — no bug, no breaker (needs Ray).

Its own module so it gets a fresh Ray cluster with clean CEO state: named
detached actors are singletons within a cluster, so reusing the breaker-tripped
CEO from test_org_failures.py would mask the point of this test.
"""

from types import SimpleNamespace
from typing import Any

import pytest

ray = pytest.importorskip("ray")

from sis import org  # noqa: E402

# The SWE finds nothing to improve: the candidate is identical to the baseline,
# so the gauntlet returns "no change" (reject_gate="noop") — see KNOWN_ISSUES M3.
NO_CHANGE_IMPL: dict[str, Any] = {
    "passed": False,
    "reason": "no change: candidate is identical to the baseline",
    "pr_id": None,
    "cost_usd": 0.0,
    "candidate_sha": "cafe12345678",
}


@pytest.fixture(scope="module")
def handles():  # type: ignore[no-untyped-def]
    h = org.bootstrap()
    h["SWE"] = SimpleNamespace(
        implement=SimpleNamespace(remote=lambda story_id: ray.put(NO_CHANGE_IMPL)))
    yield h
    ray.shutdown()


def test_no_change_files_no_bug(handles) -> None:  # type: ignore[no-untyped-def]
    result = org.run_cycle(handles, "already optimal", "nothing to do")
    assert result["status"] == "no_change"
    assert "bug_id" not in result          # not a defect — no bug filed
    assert result.get("breaker_bug_id") is None


def test_no_change_never_trips_the_breaker(handles) -> None:  # type: ignore[no-untyped-def]
    # Many "nothing to do" cycles, well past the 3-failure threshold, must not
    # page a human: a no-op is not a failure.
    for _ in range(6):
        r = org.run_cycle(handles, "again", "still nothing")
        assert r["status"] == "no_change"
        assert r.get("breaker_bug_id") is None
    assert not ray.get(handles["CEO"].breaker_open.remote())
    # A further cycle still runs (not refused with circuit_breaker_open).
    assert org.run_cycle(handles, "x", "y")["status"] == "no_change"


def test_record_neutral_records_spend_but_not_a_failure(handles) -> None:  # type: ignore[no-untyped-def]
    from sis.roles import CEO

    # Own CEO instance (tiny budget) so the shared one isn't perturbed; SLO floor
    # raised out of the way to isolate the hard spend cap.
    ceo = CEO.remote(budget_usd=0.5, slo_min_spend_usd=10.0)  # type: ignore[attr-defined]
    # Neutral spend is recorded, but it's neither a failure nor an acceptance.
    assert ray.get(ceo.record_neutral.remote(cost_usd=0.3)) is None
    econ = ray.get(ceo.economics.remote())
    assert econ["spent_usd"] == 0.3
    assert econ["accepted"] == 0.0
    assert not ray.get(ceo.breaker_open.remote())
    # The hard spend cap still applies to neutral spend (0.3 + 0.3 > 0.5).
    assert ray.get(ceo.record_neutral.remote(cost_usd=0.3)) == "hard spend cap exceeded"
    assert ray.get(ceo.breaker_open.remote())
