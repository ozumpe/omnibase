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
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from sis import artifact_sync
from sis.artifact_sync import ArtifactSync
from sis.episodic import NEUTRAL_STATUSES

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
    # An operator's pause (sis.admin, OMNI-61). Unlike an open breaker it
    # does not stop the loop: it idles, and picks up again on resume.
    paused: bool = False


def decide(tick: Tick) -> Action:
    """Pure loop policy. No Ray, no time, no I/O — just the decision."""
    if tick.breaker_open or not tick.budget_ok:
        return Action.STOP  # frozen loop / out of money: run_loop's on_stop pages
    if tick.paused:
        return Action.SKIP  # an operator said wait; keep polling for resume
    if tick.work is None:
        return Action.SKIP  # nothing triggered this tick
    return Action.RUN


def converged(statuses: Sequence[str], after: int) -> bool:
    """Whether the last *after* cycles all found nothing to improve. Pure (OMNI-138).

    "Nothing to improve" is every neutral status: no change, an inconclusive
    benchmark, no gain from the base. They cost no breaker count, so without
    this a finished target would keep spending until the budget ran out.
    Anything else in between — an accepted step, a failure — resets the run.
    """
    n = max(1, after)
    return len(statuses) >= n and all(s in NEUTRAL_STATUSES for s in statuses[-n:])


def stop_alert(tick: Tick, cycles_run: int) -> tuple[str, str] | None:
    """The page a loop stop needs, as (title, body), or None. Pure (OMNI-62).

    A STOP means no further cycle will start until a human acts, so it pages —
    once. A breaker that tripped during this run was already paged at the trip
    (``org.run_cycle``); one that was open before the first cycle was not, and
    a budget that ran out stopped the loop silently (KNOWN_ISSUES L24) while
    this module's own comment claimed "the human was already paged".
    """
    if tick.breaker_open:
        if cycles_run:
            return None  # paged when it tripped
        return ("loop did not start: circuit breaker already open",
                "The loop found the breaker open before running a single cycle — "
                "tripped in an earlier run, or held open because brake state was "
                "unreadable. Inspect with `python -m sis.admin status`.")
    if not tick.budget_ok:
        return ("loop stopped: spend budget exhausted",
                f"After {cycles_run} cycle(s) the next estimate would exceed "
                "brakes.budget_usd. No cycle starts until the budget is raised.")
    return None


def stop_summary(
    tick: Tick | None,
    cycles_run: int,
    *,
    max_cycles: int | None,
    interrupted: bool,
    trip_reason: str | None,
    spent_usd: float,
    budget_usd: float,
    converged_note: str | None = None,
) -> str:
    """One console line: why the loop stopped, and where spend stands. Pure.

    OMNI-123: the first AWS run printed ``loop stopped after 2 cycle(s)`` — not
    that the breaker had tripped, nor why. *tick* is the one that made
    :func:`decide` return STOP, or None when the loop ended by max_cycles or a
    stop signal rather than by a condition. *converged_note* says the loop
    stopped itself because the target converged (OMNI-138); it sets the stop
    event to do so, which is why it is checked before *interrupted*.
    """
    if tick is not None and tick.breaker_open:
        why = f"circuit breaker open ({trip_reason or 'reason not recorded'})"
    elif tick is not None and not tick.budget_ok:
        why = "spend budget exhausted"
    elif converged_note:
        why = converged_note
    elif interrupted:
        why = "interrupted (Ctrl-C, SIGTERM or a closed terminal)"
    elif max_cycles is not None and cycles_run >= max_cycles:
        why = f"reached loop.max_cycles ({max_cycles})"
    else:
        why = "no further work"
    return (f"[loop] stopped after {cycles_run} cycle(s): {why}; "
            f"spent ${spent_usd:.4f} of ${budget_usd:.2f}")


def run_loop(
    poll: Callable[[], Tick],
    run_cycle: Callable[[Work], dict[str, Any]],
    *,
    interval_s: float = 30.0,
    max_cycles: int | None = None,
    stop_event: threading.Event | None = None,
    sleep: Callable[[float], None] | None = None,
    on_stop: Callable[[Tick, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Drive cycles until a stop condition. Returns the results of cycles run.

    Everything time/Ray-shaped is injected, so this is fully unit-testable and
    always terminates: pass ``max_cycles`` and/or a pre-set ``stop_event``.

    ``on_stop`` is called with the tick that made :func:`decide` return STOP
    and the number of cycles run — not on ``max_cycles`` or a stop event, which
    are an operator's choices rather than a condition someone must act on.
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
            if on_stop is not None:
                on_stop(tick, cycles)
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


def sync_artifacts(sync: ArtifactSync | None, workspace: Any, *, final: bool) -> None:
    """Send the run's dataset to the artifacts bucket, if one is configured (OMNI-140).

    Says something only when it matters: a failure always (on stderr, and as a
    ``artifacts.sync_failed`` event), a success only for the last sync of the
    run, so a supervised console is not one line longer per cycle. Never raises.
    """
    if sync is None:
        return
    result = sync.sync()
    if result.error is not None:
        print(f"[sis] WARNING: artifacts not synced to {sync.destination}: {result.error}",
              file=sys.stderr, flush=True)
        try:
            workspace.emit.remote("artifacts.sync_failed", destination=sync.destination,
                                  error=result.error)
        except Exception:  # noqa: BLE001 - the warning above already told the operator
            pass
    elif final:
        print(f"[sis] artifacts synced to {sync.destination} "
              f"({', '.join(result.uploaded) or 'nothing to sync'})", flush=True)


def _install_signal_handlers(stop: threading.Event) -> None:
    def _handler(signum: int, frame: Any) -> None:
        stop.set()

    # SIGHUP is a terminal that went away: closing a tmux pane, or an SSM
    # session that ends with no tmux under it (OMNI-139). Left at its default it
    # kills the process on the spot, with no final sync and no stop summary; as
    # a graceful stop the cycle in flight finishes and the dataset is uploaded.
    sigs = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        sigs.append(signal.SIGHUP)
    for sig in sigs:
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
    canary_backend: str | None = None,
    converged_after: int | None = None,
    artifacts: Callable[[], ArtifactSync | None] | None = None,
    artifact_sync_every: int | None = None,
) -> list[dict[str, Any]]:
    """Run the loop against a live actor org, with graceful SIGINT/SIGTERM stop.

    The run's episodic log, audit and console log (``runtime/loop.log``, when
    the runbook tees one) go to ``adapters.artifacts_bucket`` every
    ``artifact_sync_every`` (default ``loop.artifact_sync_every``) cycles and
    when the loop stops for any reason, a crash included (OMNI-140, OMNI-139). *artifacts*
    builds the sync (default: from the configuration); one that returns None
    syncs nothing. A failed sync warns and never stops the loop.

    ``converged_after`` (default: ``loop.converged_after``) neutral cycles in a
    row stop the loop: the target has converged (OMNI-138). A WARNING page, not
    a breaker trip — nothing is broken, and there is nothing to reset.

    ``canary_backend`` is threaded to every cycle's ``DevOps.canary()`` call
    (OMNI-14) — ``"serve"`` for a real Ray Serve deployment judged against
    live traffic, anything else for the legacy in-memory/real recording. Most
    relevant here of anywhere: a continuously-running server is exactly where
    a real served canary matters, unlike a single demo cycle.

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

    from sis import config, episodic, org

    after = (converged_after if converged_after is not None
             else int(config.config().loop.converged_after))
    sync_every = (artifact_sync_every if artifact_sync_every is not None
                  else int(config.config().loop.artifact_sync_every))
    sync = (artifacts or artifact_sync.from_config)()
    ceo = handles["CEO"]
    self_model = handles["SelfModel"]
    workspace = handles["Workspace"]
    devops = handles["DevOps"]
    stop = stop_event or threading.Event()
    _install_signal_handlers(stop)
    # The last open-PR line printed, so a hold says so once rather than every tick.
    last_line: list[str | None] = [None]

    def poll() -> Tick:
        econ = ray.get(ceo.economics.remote())  # read-only; no telemetry side effects
        budget_ok = econ["spent_usd"] + estimate_usd <= econ["budget_usd"]
        breaker_open = bool(ray.get(ceo.breaker_open.remote()))
        paused = ray.get(ceo.pause_reason.remote()) is not None
        held_by = None
        if one_canary_in_flight and not breaker_open:
            deployment = ray.get(self_model.deployment.remote())
            held_by = canary_in_flight(deployment)
            # Check for a human merge *before* deciding to hold, so the tick
            # that observes the merge is also the tick that may start the next
            # cycle — rather than idling one whole interval after the release.
            pending = pending_merge(deployment) if (held_by and watch_merges) else None
            if pending:
                seen = ray.get(devops.observe_merge.remote(pending))
                # A merge promotes; a close without merging releases (OMNI-57).
                # Either way a human has decided, so the PR is no longer
                # remembered for the next start (OMNI-126).
                if seen["promoted"] or seen.get("released"):
                    held_by = None
                    store = episodic.get_episodic_store()
                    org.record_release(store, seen)   # a decline is logged (OMNI-57)
                    org.forget_pending_pr(store, pending)
            if held_by:
                ray.get(workspace.emit.remote("loop.held_for_canary", version=held_by))
            elif not paused:
                # Nothing held in this process's memory; the VCS may still know
                # of a PR awaiting a human (OMNI-136) — opened on a box since
                # rebuilt, or left open without a hold. Checked before any new
                # cycle: an adopted PR holds green, and the branch above takes
                # over from the next tick.
                hold, line = org.hold_for_open_prs(handles, episodic.get_episodic_store())
                if line and line != last_line[0]:
                    print(line, flush=True)
                last_line[0] = line
                if hold:
                    held_by = "an open PR"
                    ray.get(workspace.emit.remote("loop.held_for_open_pr", detail=line))
        # Don't pull new work while frozen or while a canary is still being
        # evaluated — decide() will SKIP/STOP on a None work item.
        work = None if (breaker_open or held_by or paused) else trigger()
        return Tick(breaker_open=breaker_open, budget_ok=budget_ok, work=work,
                    paused=paused)

    statuses: list[str] = []
    converged_note: list[str | None] = [None]

    def run_cycle(work: Work) -> dict[str, Any]:
        result = org.run_cycle(handles, work.title, work.body, estimate_usd=estimate_usd,
                               canary_backend=canary_backend)
        print(org.cycle_summary(result), flush=True)  # OMNI-123: every cycle says why
        statuses.append(str(result.get("status")))
        if artifact_sync.due(len(statuses), sync_every):
            sync_artifacts(sync, workspace, final=False)
        if converged(statuses, after):
            target = result.get("contract") or "the target"
            converged_note[0] = (f"{target} has converged ({max(1, after)} cycle(s) in a "
                                 "row found nothing to improve)")
            stop.set()   # this run is done; the loop ends before the next poll
        return result

    stopped_by: list[Tick] = []

    def on_stop(tick: Tick, cycles_run: int) -> None:
        stopped_by.append(tick)
        if (alert := stop_alert(tick, cycles_run)) is not None:
            from sis.ports import Severity

            org.page(workspace, episodic.get_episodic_store(), Severity.CRITICAL, *alert)

    try:
        results = run_loop(poll, run_cycle, interval_s=interval_s,
                           max_cycles=max_cycles, stop_event=stop, on_stop=on_stop)
        econ = ray.get(ceo.economics.remote())
        if converged_note[0] is not None:
            # A human decides what to optimise next; the loop is not broken, so
            # WARNING rather than the CRITICAL a breaker trip pages (OMNI-138).
            from sis.ports import Severity

            org.page(workspace, episodic.get_episodic_store(), Severity.WARNING,
                     f"loop stopped: {converged_note[0]}",
                     f"{converged_note[0]}. Spent ${float(econ['spent_usd']):.4f} of "
                     f"${float(econ['budget_usd']):.2f}. Nothing to reset: choose another "
                     "contract (--contract) or add a target, then start the loop again.")
        print(stop_summary(
            stopped_by[0] if stopped_by else None, len(results),
            max_cycles=max_cycles, interrupted=stop.is_set(),
            trip_reason=ray.get(ceo.state_snapshot.remote()).get("trip_reason"),
            spent_usd=float(econ["spent_usd"]), budget_usd=float(econ["budget_usd"]),
            converged_note=converged_note[0],
        ), flush=True)
    except Exception as exc:
        # One line for the console log: Python prints the traceback itself only
        # when the process exits, after the sync below has uploaded the log.
        print(f"[loop] crashed: {type(exc).__name__}: {' '.join(str(exc).split())[:200]}",
              file=sys.stderr, flush=True)
        raise
    finally:
        # Every way out, an exception included: the log is the point of the run.
        # After the stop summary, so the console log the runbook tees to
        # runtime/loop.log (OMNI-139) is uploaded with its last line in it.
        sync_artifacts(sync, workspace, final=True)
    return results
