"""sis.futility — stop retraining that does not help, and say an input is likely missing (OMNI-228).

The model-error triggers (:mod:`sis.model_error`, wired by OMNI-197) start a
retrain when a predictor's error stays above what its actor accepts. Some
error no retrain can fix: a refinery outage or a hurricane moves the price,
and no amount of tuning on stocks and prices predicts it. A breach of that
kind would start cycle after cycle and pay for each. When a few retrains in a
row have not cut the error, the likely cause is a missing input, not a bad
model, and this module says so and stops the spending.

**A retrain is judged by the error after it.** Each retrain a trigger starts
is recorded with the window's MAE that started it (:data:`RETRAIN_STARTED`).
The error after a retrain is the MAE that started the next one, or for the
latest, the window's MAE now. A retrain improved when that error is at most
``1 - min_improvement`` of the error that started it; a retrain with nothing
after it yet is not judged. ``retrains`` non-improving retrains in a row,
counted back from the latest, stop the trigger for that contract: an
improving one resets the count, and fewer than ``retrains`` never stop it.

**On a stop**, a :data:`FUTILE_STOPPED` record goes into the episodic store
and a human is paged once (WARNING) with "likely missing input" and the weeks
with the largest errors. Until a reset, :meth:`Guard.may_start` refuses every
retrain for that contract, with no spend and no second page.

**A reset** is a human's (``python -m sis.admin reset-futility``, a
:data:`FUTILITY_RESET` record), or a change of the contract's *basis*: the
caller names the inputs a contract has (its declared input series, its
training split), and retrains on another basis do not count. A new input is
a new basis, so it gets a fresh count without anyone resetting anything.

**Not the circuit breaker.** The breaker trips on failures; this stops on
futility. Nothing here touches the breaker, and the breaker still applies to
every retrain this lets through. The records are read from this box's store
and the earlier runs' logs (:func:`sis.holdout.recorded_events`), so a new
box remembers a stop. None of them is a judgement on the held-out fixture.

Everything that decides is pure (:func:`judge`, :func:`worst_weeks`,
:func:`page_body`); :class:`Guard` reads and writes the store and pages.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sis.clock import Clock, now_iso
from sis.episodic import EpisodicEvent, EpisodicStore, ScoredWeek
from sis.holdout import recorded_events
from sis.ports import Notifier, Severity

# The episodic outcomes this module writes and reads. None costs anything,
# counts against the breaker, or is a judgement on a held-out fixture.
RETRAIN_STARTED = "retrain_started"
FUTILE_STOPPED = "futile_stopped"
FUTILITY_RESET = "futility_reset"
OUTCOMES = frozenset({RETRAIN_STARTED, FUTILE_STOPPED, FUTILITY_RESET})

# How many of the largest-error weeks a page names.
WORST_WEEKS = 3


@dataclass(frozen=True)
class Rule:
    """When retraining is futile: *retrains* in a row that cut the error by less
    than *min_improvement* (a fraction of the error that started each one)."""

    retrains: int
    min_improvement: float

    def __post_init__(self) -> None:
        if self.retrains < 1:
            raise ValueError(f"futility needs at least one retrain to judge, got {self.retrains}")
        if not 0 <= self.min_improvement < 1:
            raise ValueError("the minimum improvement is a fraction in [0, 1), got "
                             f"{self.min_improvement}")


@dataclass(frozen=True)
class Retrain:
    """One retrain a trigger started, as the episodic record has it."""

    cycle_id: str
    ts: str
    mae: float | None              # the window's MAE that started it


@dataclass(frozen=True)
class Verdict:
    """Whether a trigger may start a retrain for one contract, and why.

    ``stop`` refuses the retrain. ``newly`` says the stop is this tick's: the
    caller records it and pages; a stop recorded earlier is neither recorded
    nor paged again. ``streak`` is how many judged retrains in a row did not
    improve, latest first.
    """

    stop: bool
    newly: bool
    streak: int
    reason: str
    retrains: tuple[Retrain, ...] = ()


def improved(before: float | None, after: float | None, min_improvement: float) -> bool | None:
    """Whether the error went from *before* to at most ``(1 - min_improvement) * before``. Pure.

    None when either side is unknown: that retrain is not judged.
    """
    if before is None or after is None:
        return None
    return after <= before * (1 - min_improvement)


def _since_reset(events: Iterable[EpisodicEvent], contract: str) -> list[EpisodicEvent]:
    """The contract's futility records after its latest reset, in time order."""
    own = sorted((e for e in events if e.contract == contract and e.outcome in OUTCOMES),
                 key=lambda e: e.ts)
    last = max((i for i, e in enumerate(own) if e.outcome == FUTILITY_RESET), default=-1)
    return own[last + 1:]


def retrains(events: Iterable[EpisodicEvent], contract: str, basis: str) -> tuple[Retrain, ...]:
    """The retrains started for *contract* on *basis* since its last reset. Pure."""
    return tuple(Retrain(e.cycle_id, e.ts, e.trigger_mae)
                 for e in _since_reset(events, contract)
                 if e.outcome == RETRAIN_STARTED and e.trigger_basis == basis)


def judge(events: Iterable[EpisodicEvent], contract: str, *, basis: str,
          current_mae: float | None, rule: Rule) -> Verdict:
    """May a model-error trigger start another retrain for *contract*? Pure.

    *events* are the recorded episodic events (this box's and earlier runs').
    *basis* names the inputs the contract has now; *current_mae* is the error
    of the window that wants the retrain.
    """
    recent = _since_reset(events, contract)
    if any(e.outcome == FUTILE_STOPPED and e.trigger_basis == basis for e in recent):
        return Verdict(True, False, rule.retrains,
                       f"{contract}: retraining was stopped as futile and nothing has reset "
                       "it (a new input, or `python -m sis.admin reset-futility`)")
    started = retrains(events, contract, basis)
    afters = [r.mae for r in started[1:]] + ([current_mae] if started else [])
    streak = 0
    for retrain, after in zip(reversed(started), reversed(afters), strict=True):
        verdict = improved(retrain.mae, after, rule.min_improvement)
        if verdict is None:
            if streak == 0:
                continue        # nothing after the latest yet: not judged
            break
        if verdict:
            break
        streak += 1
    if streak >= rule.retrains:
        return Verdict(True, True, streak,
                       f"{contract}: {streak} retrain(s) in a row cut the error by less than "
                       f"{rule.min_improvement:.0%}; likely missing input", started)
    return Verdict(False, False, streak,
                   f"{contract}: {streak} of {rule.retrains} retrain(s) in a row without "
                   "improvement", started)


def worst_weeks(rows: Iterable[ScoredWeek], target: str, k: int = WORST_WEEKS
                ) -> list[ScoredWeek]:
    """The *k* weeks of *target* with the largest absolute error. Pure.

    Each (predictor, week) counts at its latest revision; a week with no
    error (skipped, or the actual withdrawn) is left out.
    """
    latest: dict[tuple[str, str], ScoredWeek] = {}
    for row in rows:
        if row.target != target:
            continue
        key = (row.predictor, row.week)
        held = latest.get(key)
        if held is None or row.revision > held.revision:
            latest[key] = row
    scored = [row for row in latest.values() if row.error is not None]
    return sorted(scored, key=lambda row: (-abs(row.error or 0.0), row.week))[:k]


def page_body(verdict: Verdict, worst: Sequence[ScoredWeek]) -> str:
    """What the page says: the stop, the evidence, and what a human can do. Pure."""
    lines = [verdict.reason + ".",
             "Retrains started and the error each started from:"]
    lines += [f"  {r.ts}  cycle {r.cycle_id}  MAE "
              f"{'unknown' if r.mae is None else format(r.mae, '.4g')}"
              for r in verdict.retrains[-(verdict.streak + 1):]]
    if worst:
        lines.append("The weeks with the largest errors:")
        lines += [f"  {row.week[:10]}  error {row.error:+.4g}" for row in worst]
    lines += ["No recorded input explains these weeks: the error looks to need a new input "
              "(an event signal, another series), not another retrain.",
              "No retrain starts for this contract until a new input changes its basis, or "
              "a human runs `python -m sis.admin reset-futility --contract <name> --reason "
              "\"...\"`. The circuit breaker is not touched."]
    return "\n".join(lines)


@dataclass
class Guard:
    """The I/O around :func:`judge`, for the model-error trigger (OMNI-197) to call.

    ``may_start`` before a retrain, and ``started`` once one is started.
    """

    store: EpisodicStore
    notifier: Notifier
    rule: Rule
    history_dir: Path | None = None
    clock: Clock | None = None

    def _events(self) -> list[EpisodicEvent]:
        return recorded_events(self.store, self.history_dir)

    def may_start(self, contract: str, *, basis: str, current_mae: float | None,
                  target: str | None = None) -> Verdict:
        """Judge, and on a new stop record it and page once. *target* names the
        series whose error series the page draws its worst weeks from."""
        verdict = judge(self._events(), contract, basis=basis, current_mae=current_mae,
                        rule=self.rule)
        if verdict.newly:
            self.store.append(EpisodicEvent(
                cycle_id=EpisodicEvent.new_cycle_id(), ts=now_iso(self.clock),
                outcome=FUTILE_STOPPED, contract=contract, reject_reason=verdict.reason,
                trigger_mae=current_mae, trigger_basis=basis))
            worst = worst_weeks(self.store.scored(), target) if target else []
            self.notifier.notify(Severity.WARNING,
                                 f"likely missing input: {contract}",
                                 page_body(verdict, worst))
        return verdict

    def started(self, contract: str, *, cycle_id: str, trigger: str, basis: str,
                mae: float | None) -> None:
        """Record that *trigger* started retrain *cycle_id* from a window MAE of *mae*."""
        self.store.append(EpisodicEvent(
            cycle_id=cycle_id, ts=now_iso(self.clock), outcome=RETRAIN_STARTED,
            contract=contract, trigger=trigger, trigger_mae=mae, trigger_basis=basis))


def reset(store: EpisodicStore, contract: str, reason: str, *, clock: Clock | None = None
          ) -> EpisodicEvent:
    """Record a human's reset for *contract*: earlier retrains and stops no longer count."""
    event = EpisodicEvent(cycle_id=EpisodicEvent.new_cycle_id(), ts=now_iso(clock),
                          outcome=FUTILITY_RESET, contract=contract, reject_reason=reason)
    store.append(event)
    return event


def status(events: Iterable[EpisodicEvent], contract: str) -> str:
    """One line on where *contract* stands, on any basis. Pure."""
    recent = _since_reset(events, contract)
    stopped = [e for e in recent if e.outcome == FUTILE_STOPPED]
    started = sum(1 for e in recent if e.outcome == RETRAIN_STARTED)
    if stopped:
        return (f"{contract}: STOPPED as futile at {stopped[-1].ts} "
                f"({started} retrain(s) since the last reset)")
    return f"{contract}: {started} retrain(s) since the last reset, not stopped"


__all__ = ["FUTILE_STOPPED", "FUTILITY_RESET", "OUTCOMES", "RETRAIN_STARTED", "WORST_WEEKS",
           "Guard", "Retrain", "Rule", "Verdict", "improved", "judge", "page_body", "reset",
           "retrains", "status", "worst_weeks"]
