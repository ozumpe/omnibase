"""serve() wiring against a live actor org (needs Ray). Own module for a fresh
Ray cluster — named detached actors are singletons within a cluster."""

import pytest

ray = pytest.importorskip("ray")

from sis import loop, org  # noqa: E402


@pytest.fixture(scope="module")
def handles():  # type: ignore[no-untyped-def]
    h = org.bootstrap()
    yield h
    ray.shutdown()


def test_serve_runs_a_bounded_real_cycle(handles) -> None:  # type: ignore[no-untyped-def]
    # max_cycles=1 + the once() trigger → exactly one real cycle end to end.
    results = loop.serve(
        handles,
        loop.once("Speed up divisor-sum", "Too slow; make it faster, same results."),
        interval_s=0.0,
        max_cycles=1,
    )
    assert len(results) == 1
    assert results[0]["status"] == "verified_awaiting_human_merge"


def test_serve_stops_immediately_when_event_preset(handles) -> None:  # type: ignore[no-untyped-def]
    import threading

    stop = threading.Event()
    stop.set()
    results = loop.serve(handles, loop.once("x", "y"), interval_s=0.0, stop_event=stop)
    assert results == []  # graceful stop before any cycle
