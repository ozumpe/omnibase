"""CEO brake/spend state: snapshot, rehydrate, and reset (M2/L9, needs Ray).

Its own module so it gets a fresh Ray cluster with clean CEO state — named
detached actors are singletons within a cluster, so a breaker-tripped CEO from
another module would mask these assertions.
"""

import pytest

ray = pytest.importorskip("ray")

from sis import org  # noqa: E402
from sis.roles import CEO  # noqa: E402


@pytest.fixture(scope="module")
def handles():  # type: ignore[no-untyped-def]
    h = org.bootstrap()  # brings up the shared substrate (SelfModel/Workspace)
    yield h
    ray.shutdown()


def test_snapshot_reflects_outcomes(handles) -> None:  # type: ignore[no-untyped-def]
    ceo = handles["CEO"]
    before = ray.get(ceo.state_snapshot.remote())["spent_usd"]
    ray.get(ceo.report_outcome.remote(success=False, cost_usd=0.10))
    snap = ray.get(ceo.state_snapshot.remote())
    assert snap["spent_usd"] == pytest.approx(before + 0.10)
    assert snap["consecutive_failures"] >= 1


def test_reset_breaker_clears_failures_but_not_spend(handles) -> None:  # type: ignore[no-untyped-def]
    # A fresh CEO with a tiny threshold, tripped by repeated failures.
    ceo = CEO.remote(budget_usd=10.0, breaker_threshold=2)
    ray.get(ceo.report_outcome.remote(success=False, cost_usd=0.20))
    ray.get(ceo.report_outcome.remote(success=False, cost_usd=0.20))
    assert ray.get(ceo.breaker_open.remote()) is True

    spent_before = ray.get(ceo.state_snapshot.remote())["spent_usd"]
    assert ray.get(ceo.reset_breaker.remote()) is True
    assert ray.get(ceo.breaker_open.remote()) is False
    snap = ray.get(ceo.state_snapshot.remote())
    assert snap["consecutive_failures"] == 0
    assert snap["spent_usd"] == pytest.approx(spent_before)  # spend is NOT reset (§4.1)


def test_fresh_ceo_rehydrates_persisted_state(handles) -> None:  # type: ignore[no-untyped-def]
    # A restart is a fresh actor built from the persisted snapshot (L9): a
    # previously-tripped breaker and accumulated spend must come back.
    state = {"spent_usd": 3.25, "consecutive_failures": 3, "accepted": 1, "tripped": True}
    ceo = CEO.remote(budget_usd=10.0, breaker_threshold=3, state=state)
    assert ray.get(ceo.breaker_open.remote()) is True
    snap = ray.get(ceo.state_snapshot.remote())
    assert snap["spent_usd"] == pytest.approx(3.25)
    assert snap["accepted"] == 1
    assert ray.get(ceo.economics.remote())["spent_usd"] == pytest.approx(3.25)
