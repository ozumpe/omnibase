"""Tests for the server loop policy + orchestrator (pure — no Ray, no time)."""

import threading
from collections.abc import Callable

import pytest

from sis.loop import (
    Action,
    Tick,
    Work,
    breach_trigger,
    canary_in_flight,
    decide,
    once,
    run_loop,
    serve_breach,
    window_in_breach,
)

_WORK = Work("t", "b")


# --- decide(): the pure policy --------------------------------------------


def test_decide_runs_when_clear_and_work_present() -> None:
    assert decide(Tick(breaker_open=False, budget_ok=True, work=_WORK)) is Action.RUN


def test_decide_skips_when_no_work() -> None:
    assert decide(Tick(breaker_open=False, budget_ok=True, work=None)) is Action.SKIP


def test_decide_stops_on_breaker_or_no_budget() -> None:
    assert decide(Tick(breaker_open=True, budget_ok=True, work=_WORK)) is Action.STOP
    assert decide(Tick(breaker_open=False, budget_ok=False, work=_WORK)) is Action.STOP


# --- run_loop(): the orchestrator (fakes injected) ------------------------


def _poll(*, breaker_open: bool = False, budget_ok: bool = True,
          work: Work | None = _WORK) -> Callable[[], Tick]:
    tick = Tick(breaker_open=breaker_open, budget_ok=budget_ok, work=work)
    return lambda: tick


def test_max_cycles_bounds_the_loop() -> None:
    ran: list[Work] = []
    run_loop(_poll(), ran.append, max_cycles=3, sleep=lambda _s: None)  # type: ignore[arg-type]
    assert len(ran) == 3


def test_preset_stop_event_exits_immediately() -> None:
    ran: list[Work] = []
    stop = threading.Event()
    stop.set()  # "exit right away"
    run_loop(_poll(), ran.append, stop_event=stop, sleep=lambda _s: None)  # type: ignore[arg-type]
    assert ran == []


def test_breaker_stops_the_loop() -> None:
    ran: list[Work] = []
    # No max_cycles / no stop event — the breaker STOP is what must end it.
    run_loop(_poll(breaker_open=True), ran.append, sleep=lambda _s: None)  # type: ignore[arg-type]
    assert ran == []


def test_skip_tick_runs_nothing_then_stops_on_signal() -> None:
    # No work → SKIP; the injected sleep sets the stop event so it ends.
    ran: list[Work] = []
    stop = threading.Event()
    run_loop(_poll(work=None), ran.append, stop_event=stop,  # type: ignore[arg-type]
             sleep=lambda _s: stop.set())
    assert ran == []


def test_sleep_is_interruptible_by_default() -> None:
    # The default sleep is stop.wait, so a set event wakes it at once — a slow
    # run_cycle that sets the event mid-flight ends the loop without a real sleep.
    stop = threading.Event()
    ran: list[Work] = []

    def _run(work: Work) -> dict[str, object]:
        ran.append(work)
        stop.set()  # graceful stop requested during the cycle
        return {"status": "ok"}

    results = run_loop(_poll(), _run, stop_event=stop, interval_s=999.0)
    assert len(ran) == 1 and len(results) == 1  # did not hang on the 999s sleep


# --- once(): the demo/single-build trigger --------------------------------


def test_once_yields_one_then_none() -> None:
    trigger = once("title", "body")
    first = trigger()
    assert first == Work("title", "body")
    assert trigger() is None


# --- The real trigger: sustained SLO breach on live traffic (SERVE_CANARY 11) ---


def _window(p99: float, samples: float = 100.0) -> dict[str, float]:
    return {"p50": p99 / 2, "p95": p99, "p99": p99, "error_rate": 0.0, "samples": samples}


def test_window_in_breach_needs_the_sample_floor() -> None:
    # The p99 of a five-request window *is* one slow request. Acting on it is
    # the online restatement of L5's noise-floor problem.
    slow_but_thin = _window(p99=9.0, samples=5.0)
    assert not window_in_breach(slow_but_thin, slo_p99_s=1.0, min_samples=20)
    assert window_in_breach(_window(p99=9.0, samples=20.0), slo_p99_s=1.0, min_samples=20)


def test_empty_window_reads_as_healthy_not_breaching() -> None:
    # No traffic must never start a cycle — the safe direction.
    empty = {"p50": 0.0, "p95": 0.0, "p99": 0.0, "error_rate": 0.0, "samples": 0.0}
    assert not window_in_breach(empty, slo_p99_s=0.001)


def test_a_single_spike_is_not_a_breach() -> None:
    # DESIGN.md §4: sustained breach over a rolling window, never a single spike.
    assert not serve_breach(_window(p99=9.0), slo_p99_s=1.0,
                            consecutive=0, breach_window_ticks=3)


def test_breach_fires_once_the_streak_completes() -> None:
    assert serve_breach(_window(p99=9.0), slo_p99_s=1.0,
                        consecutive=2, breach_window_ticks=3)


def test_healthy_window_never_breaches_however_long_the_streak() -> None:
    assert not serve_breach(_window(p99=0.1), slo_p99_s=1.0,
                            consecutive=99, breach_window_ticks=3)


def test_breach_window_ticks_must_be_at_least_one() -> None:
    with pytest.raises(ValueError, match="breach_window_ticks"):
        serve_breach(_window(p99=9.0), slo_p99_s=1.0, consecutive=0, breach_window_ticks=0)


class _FakeCloud:
    """Feeds live_metrics from a scripted sequence of windows."""

    def __init__(self, windows: list[dict[str, float]]) -> None:
        self._windows = list(windows)
        self.reads = 0

    def live_metrics(self, version: str, window_s: float) -> dict[str, float]:
        self.reads += 1
        return self._windows.pop(0) if self._windows else _window(p99=0.0, samples=0.0)


def test_breach_trigger_yields_work_only_when_sustained() -> None:
    cloud = _FakeCloud([_window(9.0), _window(9.0), _window(9.0)])
    trigger = breach_trigger(cloud, "v1", slo_p99_s=1.0, title="t", body="b",  # type: ignore[arg-type]
                             breach_window_ticks=3)
    assert trigger() is None       # tick 1 — breaching, streak 1
    assert trigger() is None       # tick 2 — breaching, streak 2
    assert trigger() == Work("t", "b")  # tick 3 — sustained


def test_breach_trigger_resets_the_streak_on_a_healthy_tick() -> None:
    # Two breaching ticks, then recovery, then two more must NOT fire: the
    # streak has to be consecutive, or an intermittent target trickles cycles.
    cloud = _FakeCloud([_window(9.0), _window(9.0), _window(0.1), _window(9.0), _window(9.0)])
    trigger = breach_trigger(cloud, "v1", slo_p99_s=1.0, title="t", body="b",  # type: ignore[arg-type]
                             breach_window_ticks=3)
    assert [trigger() for _ in range(5)] == [None] * 5


def test_breach_trigger_resets_after_firing() -> None:
    # A long outage should produce one cycle per window, not one per tick.
    cloud = _FakeCloud([_window(9.0)] * 6)
    trigger = breach_trigger(cloud, "v1", slo_p99_s=1.0, title="t", body="b",  # type: ignore[arg-type]
                             breach_window_ticks=3)
    fired = [trigger() is not None for _ in range(6)]
    assert fired == [False, False, True, False, False, True]


# --- One canary in flight at a time ---------------------------------------


def test_canary_in_flight_reports_the_occupying_version() -> None:
    assert canary_in_flight({"slots": {"blue": "v0", "green": "v1"}}) == "v1"


def test_canary_in_flight_is_none_when_green_is_free() -> None:
    assert canary_in_flight({"slots": {"blue": "v0", "green": None}}) is None
    assert canary_in_flight({"slots": {}}) is None
    assert canary_in_flight({}) is None
