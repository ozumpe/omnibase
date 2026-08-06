"""sis.loop — the long-running driver loop (the "server" part).

``main.py`` runs one cycle and exits; this runs cycles until told to stop. It is
the difference between a self-improvement *cycle* and a self-improving *server*.

Kept testable by the project convention — a pure policy + injectable I/O:

- :func:`decide` is a pure function (breaker/budget/work → RUN | SKIP | STOP),
  unit-testable without Ray, time, or a network.
- :func:`run_loop` is the orchestrator with everything injected (a ``poll``
  callback, a ``run_cycle`` callback, the clock, a stop Event, a cycle bound),
  so tests drive it with fakes and it always terminates.
- :func:`serve` is the thin shell that wires the real Ray reads + ``org.run_cycle``
  and installs SIGINT/SIGTERM handlers.

Two independent exit conditions, on purpose:
- **``stop_event``** — graceful shutdown. A signal handler (or a test) sets it;
  the sleep is ``event.wait`` so shutdown is immediate, not after the interval.
- **``max_cycles``** — run at most N cycles then return (the test bound).
The loop also exits when the breaker trips or the budget is exhausted.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


class Action(str, Enum):
    RUN = "run"    # a trigger fired and we're clear to spend — run a cycle
    SKIP = "skip"  # idle tick: nothing to do, wait and poll again
    STOP = "stop"  # terminal: breaker open or budget exhausted — leave the loop


@dataclass(frozen=True)
class Work:
    """One unit of intake for a cycle — a proposal to refine and build."""

    title: str
    body: str


@dataclass(frozen=True)
class Tick:
    """A snapshot the loop policy decides on."""

    breaker_open: bool
    budget_ok: bool
    work: Work | None


def decide(tick: Tick) -> Action:
    """Pure loop policy. No Ray, no time, no I/O — just the decision."""
    if tick.breaker_open or not tick.budget_ok:
        return Action.STOP  # frozen loop / out of money: the human was already paged
    if tick.work is None:
        return Action.SKIP  # nothing triggered this tick
    return Action.RUN


def run_loop(
    poll: Callable[[], Tick],
    run_cycle: Callable[[Work], dict[str, Any]],
    *,
    interval_s: float = 30.0,
    max_cycles: int | None = None,
    stop_event: threading.Event | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[dict[str, Any]]:
    """Drive cycles until a stop condition. Returns the results of cycles run.

    Everything time/Ray-shaped is injected, so this is fully unit-testable and
    always terminates: pass ``max_cycles`` and/or a pre-set ``stop_event``.
    """
    stop = stop_event or threading.Event()
    # Default sleep is the *interruptible* wait, so setting the stop event wakes
    # the loop immediately instead of after the full interval.
    do_sleep = sleep if sleep is not None else stop.wait
    results: list[dict[str, Any]] = []
    cycles = 0
    while not stop.is_set():
        if max_cycles is not None and cycles >= max_cycles:
            break
        action = decide(tick := poll())
        if action is Action.STOP:
            break
        if action is Action.RUN:
            assert tick.work is not None  # decide() guarantees this
            results.append(run_cycle(tick.work))
            cycles += 1
        do_sleep(interval_s)
    return results


def once(title: str, body: str) -> Callable[[], Work | None]:
    """A trigger that yields one :class:`Work`, then ``None`` — a single build.

    A real deployment swaps this for a trigger that polls the intake space for
    new proposals (or a sustained-SLO-breach detector once there is a served
    endpoint to measure).
    """
    pending: list[Work] = [Work(title, body)]

    def _trigger() -> Work | None:
        return pending.pop() if pending else None

    return _trigger


def repeat(title: str, body: str) -> Callable[[], Work | None]:
    """A trigger that always yields the same :class:`Work`.

    For a demo that keeps running cycles — terminate it with ``max_cycles`` or a
    stop signal. (``once`` runs one build then idles; ``repeat`` never runs dry,
    so ``max_cycles`` is a clean bound and an idle interval never busy-spins.)
    """
    work = Work(title, body)
    return lambda: work


def _install_signal_handlers(stop: threading.Event) -> None:
    def _handler(signum: int, frame: Any) -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except ValueError:
            pass  # not in the main thread (e.g. under pytest) — skip


def serve(
    handles: dict[str, Any],
    trigger: Callable[[], Work | None],
    *,
    interval_s: float = 30.0,
    estimate_usd: float = 0.5,
    max_cycles: int | None = None,
    stop_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Run the loop against a live actor org, with graceful SIGINT/SIGTERM stop."""
    import ray

    from sis import org

    ceo = handles["CEO"]
    stop = stop_event or threading.Event()
    _install_signal_handlers(stop)

    def poll() -> Tick:
        econ = ray.get(ceo.economics.remote())  # read-only; no telemetry side effects
        budget_ok = econ["spent_usd"] + estimate_usd <= econ["budget_usd"]
        breaker_open = bool(ray.get(ceo.breaker_open.remote()))
        # Don't pull new work while frozen — decide() will STOP anyway.
        work = None if breaker_open else trigger()
        return Tick(breaker_open=breaker_open, budget_ok=budget_ok, work=work)

    def run_cycle(work: Work) -> dict[str, Any]:
        return org.run_cycle(handles, work.title, work.body, estimate_usd=estimate_usd)

    return run_loop(poll, run_cycle, interval_s=interval_s,
                    max_cycles=max_cycles, stop_event=stop)
