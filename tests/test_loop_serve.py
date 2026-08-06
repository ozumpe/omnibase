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


def test_serve_holds_the_loop_while_a_canary_occupies_green(handles) -> None:  # type: ignore[no-untyped-def]
    # One canary at a time: a cycle's own canary traffic feeds the very
    # live_metrics window the trigger reads, and because cycles baseline from the
    # *merged* target, a second cycle would re-propose the change still sitting
    # unmerged in the first one's PR.
    import threading

    ray.get(handles["SelfModel"].set_slot.remote("green", "feature/x@1"))
    consulted: list[int] = []

    def _trigger():  # type: ignore[no-untyped-def]
        consulted.append(1)
        return loop.Work("x", "y")

    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    results = loop.serve(handles, _trigger, interval_s=0.01, max_cycles=1, stop_event=stop)

    assert results == []
    assert consulted == []  # the trigger was never even consulted — no spend, no work
    ray.get(handles["SelfModel"].set_slot.remote("green", None))


def test_retire_canary_releases_the_gate(handles) -> None:  # type: ignore[no-untyped-def]
    # Without a release the gate is a trap: green is set on canary deploy and
    # nothing else ever clears it, so the loop idles forever after cycle one.
    ray.get(handles["SelfModel"].set_slot.remote("green", "feature/x@1"))
    assert loop.canary_in_flight(ray.get(handles["SelfModel"].deployment.remote()))

    ray.get(handles["DevOps"].retire_canary.remote("feature/x@1"))

    assert loop.canary_in_flight(ray.get(handles["SelfModel"].deployment.remote())) is None


def test_serve_can_opt_out_of_the_gate(handles) -> None:  # type: ignore[no-untyped-def]
    # Escape hatch for the old always-propose behaviour.
    ray.get(handles["SelfModel"].set_slot.remote("green", "feature/x@1"))
    consulted: list[int] = []

    def _trigger():  # type: ignore[no-untyped-def]
        consulted.append(1)
        return None  # no work, so no cycle actually runs

    import threading
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    loop.serve(handles, _trigger, interval_s=0.01, stop_event=stop,
               one_canary_in_flight=False)

    assert consulted  # consulted despite green being occupied
    ray.get(handles["SelfModel"].set_slot.remote("green", None))
