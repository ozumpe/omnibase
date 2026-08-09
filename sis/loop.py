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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sis.ports import Cloud


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


# --------------------------------------------------------------------------
# The real trigger: a sustained SLO breach on live traffic
# --------------------------------------------------------------------------

# A breach decision needs enough requests behind it to mean anything: the p99 of
# a five-request window *is* one slow request. Spending an LLM cycle on that is
# the online restatement of L5's noise-floor problem, so the floor is a first
# class parameter rather than a hidden constant.
DEFAULT_MIN_BREACH_SAMPLES = 20

# How many consecutive breaching ticks before the loop acts. CLAUDE.md/DESIGN.md
# §4: trigger on a *sustained* breach over a rolling window, never a single spike.
DEFAULT_BREACH_WINDOW_TICKS = 3


def window_in_breach(
    metrics: Mapping[str, float],
    *,
    slo_p99_s: float,
    min_samples: int = DEFAULT_MIN_BREACH_SAMPLES,
) -> bool:
    """Is this one ``live_metrics`` window over the SLO? Pure.

    A window that hasn't cleared ``min_samples`` is *not* a breach — too little
    traffic to judge. An empty window reports ``p99=0.0``, so it also reads as
    healthy, which is the safe direction: no traffic must never start a cycle.
    """
    if metrics.get("samples", 0.0) < min_samples:
        return False
    return metrics.get("p99", 0.0) > slo_p99_s


def serve_breach(
    metrics: Mapping[str, float],
    *,
    slo_p99_s: float,
    consecutive: int,
    breach_window_ticks: int = DEFAULT_BREACH_WINDOW_TICKS,
    min_samples: int = DEFAULT_MIN_BREACH_SAMPLES,
) -> bool:
    """Sustained breach only — never a single spike. Pure.

    ``consecutive`` is how many *prior* consecutive ticks were already in
    breach; this tick completes the streak when the total reaches
    ``breach_window_ticks``. The counter lives in the caller
    (:func:`breach_trigger` owns it) so this stays a function of its arguments.
    """
    if breach_window_ticks < 1:
        raise ValueError(f"breach_window_ticks must be >= 1, got {breach_window_ticks}")
    if not window_in_breach(metrics, slo_p99_s=slo_p99_s, min_samples=min_samples):
        return False
    return consecutive + 1 >= breach_window_ticks


def breach_trigger(
    cloud: Cloud,
    version: str,
    *,
    slo_p99_s: float,
    title: str,
    body: str,
    window_s: float = 60.0,
    breach_window_ticks: int = DEFAULT_BREACH_WINDOW_TICKS,
    min_samples: int = DEFAULT_MIN_BREACH_SAMPLES,
) -> Callable[[], Work | None]:
    """The real monitor, in the shape ``serve()``'s ``trigger`` expects.

    The impure half of :func:`serve_breach`: it owns the ``live_metrics`` read
    and the consecutive-tick counter, and yields :class:`Work` only when the
    breach is sustained. Drop-in for :func:`repeat` — this is what makes
    ``main.py --loop`` a self-improving *server* rather than a scheduler
    replaying one canned proposal.

    The streak resets both when a tick comes back healthy and after firing, so a
    long outage produces one cycle per ``breach_window_ticks``, not one per tick.
    """
    consecutive = 0

    def _trigger() -> Work | None:
        nonlocal consecutive
        metrics = cloud.live_metrics(version, window_s)
        if serve_breach(metrics, slo_p99_s=slo_p99_s, consecutive=consecutive,
                        breach_window_ticks=breach_window_ticks,
                        min_samples=min_samples):
            consecutive = 0
            return Work(title, body)
        consecutive = (
            consecutive + 1
            if window_in_breach(metrics, slo_p99_s=slo_p99_s, min_samples=min_samples)
            else 0
        )
        return None

    return _trigger


def canary_in_flight(deployment: Mapping[str, Any]) -> str | None:
    """The version occupying the green slot, if any. Pure.

    One canary at a time: a cycle's own canary traffic feeds the very
    ``live_metrics`` window :func:`breach_trigger` reads, and collecting a window
    is minutes-scale, so a second cycle started meanwhile would both corrupt the
    first one's measurement and (because cycles baseline from the *merged*
    target) re-propose the change still sitting unmerged in the first one's PR.
    """
    slots = deployment.get("slots", {})
    green = slots.get("green")
    return str(green) if green else None


def pending_merge(deployment: Mapping[str, Any]) -> str | None:
    """The PR whose merge would release the current canary, if any. Pure.

    Sibling of :func:`canary_in_flight`, and separate from it because the two
    answer different questions: green can be occupied by a canary that has no
    PR to wait on (a manual ``deploy_canary``), and a recorded PR is only
    actionable while green is actually held.
    """
    pending = deployment.get("pending_pr")
    return str(pending) if pending else None


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
    one_canary_in_flight: bool = True,
    watch_merges: bool = True,
) -> list[dict[str, Any]]:
    """Run the loop against a live actor org, with graceful SIGINT/SIGTERM stop.

    ``one_canary_in_flight`` (default on) holds the next cycle while a canary
    still occupies the green slot — see :func:`canary_in_flight`.

    ``watch_merges`` (default on) is what *releases* that hold. On every tick
    where a canary is held, the loop re-reads the pending PR and, if a human
    has merged it, promotes the candidate and frees green (see
    ``DevOps.observe_merge``). Without it the loop stops permanently at the
    first successful cycle, which is why ``Cloud.promote()`` had no caller at
    all before OMNI-15.

    The loop still never merges and never decides to promote — it only notices
    that a human did. Pass ``watch_merges=False`` to hold until an operator
    calls ``retire_canary``/``observe_merge`` by hand.
    """
    import ray

    from sis import org

    ceo = handles["CEO"]
    self_model = handles["SelfModel"]
    workspace = handles["Workspace"]
    devops = handles["DevOps"]
    stop = stop_event or threading.Event()
    _install_signal_handlers(stop)

    def poll() -> Tick:
        econ = ray.get(ceo.economics.remote())  # read-only; no telemetry side effects
        budget_ok = econ["spent_usd"] + estimate_usd <= econ["budget_usd"]
        breaker_open = bool(ray.get(ceo.breaker_open.remote()))
        held_by = None
        if one_canary_in_flight and not breaker_open:
            deployment = ray.get(self_model.deployment.remote())
            held_by = canary_in_flight(deployment)
            # Check for a human merge *before* deciding to hold, so the tick
            # that observes the merge is also the tick that may start the next
            # cycle — rather than idling one whole interval after the release.
            pending = pending_merge(deployment) if (held_by and watch_merges) else None
            if pending and ray.get(devops.observe_merge.remote(pending))["promoted"]:
                held_by = None
            if held_by:
                ray.get(workspace.emit.remote("loop.held_for_canary", version=held_by))
        # Don't pull new work while frozen or while a canary is still being
        # evaluated — decide() will SKIP/STOP on a None work item.
        work = None if (breaker_open or held_by) else trigger()
        return Tick(breaker_open=breaker_open, budget_ok=budget_ok, work=work)

    def run_cycle(work: Work) -> dict[str, Any]:
        return org.run_cycle(handles, work.title, work.body, estimate_usd=estimate_usd)

    return run_loop(poll, run_cycle, interval_s=interval_s,
                    max_cycles=max_cycles, stop_event=stop)
