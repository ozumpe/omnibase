"""Tests for the server loop policy + orchestrator (pure — no Ray, no time)."""

import threading
from collections.abc import Callable

from sis.loop import Action, Tick, Work, decide, once, run_loop

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
