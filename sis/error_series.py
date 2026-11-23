"""sis.error_series — the predictor's error series, scored after each capture (OMNI-198).

Phase B's triggers (:mod:`sis.model_error`) judge a history of errors, and
that history has to outlive one replay, a restart and a new box. This module
keeps it: after a capture, the live predictor is scored on the weeks that have
become scorable since the last scoring, and each one is appended to the
episodic store as a :class:`~sis.episodic.ScoredWeek`.

- **Each week is scored once per predictor version.** A week already in the
  series is not asked again, so scoring twice after one capture adds nothing.
  A week that could not be scored yet (its next week not yet published, the
  predictor failed, nothing to compare with) is not written, and is tried
  again after the next capture.
- **A revision is a new row, never an overwrite.** When a later vintage of the
  actual differs from the one a row saw, the week gets a row with the next
  ``revision``: the same prediction, the new actual and errors. A withdrawn
  actual is a revision whose actual is None.
- **The questions are :func:`sis.prediction_error.questions`' questions**, so
  the predictor is shown exactly what a replay shows it, never a value before
  its ``known_at``, and it runs in the sandbox (the caller passes the asking).

Which weeks are new, and how a revision is written, is decided here in pure
functions (:func:`plan`, :func:`rows`); :func:`score_new` does the reading,
asking and writing. Nothing here starts a cycle or pages anyone: wiring the
series to the triggers is OMNI-197.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import pathlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sis.clock import Clock, now_iso
from sis.episodic import EpisodicStore, ScoredWeek
from sis.ports import Reading, Telemetry
from sis.prediction_error import CALIFORNIA, Question, Step, questions, score, skill
from sis.sensor_input import Absent, SourceRules

# With no series yet for the target, the first scoring starts this far before
# the last question the trace asks: a quarter of history to judge from, not
# every week since the trace began.
FIRST_WEEKS = 13

Ask = Callable[[Sequence[Question]], Sequence[Any]]


def version(source: str) -> str:
    """A predictor's version: the first 12 hex digits of its source's sha256."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def latest(series: Iterable[ScoredWeek], *, target: str,
           predictor: str) -> dict[str, ScoredWeek]:
    """Each week's latest row for one predictor on one target, keyed by ``week``."""
    out: dict[str, ScoredWeek] = {}
    for row in series:
        if row.target != target or row.predictor != predictor:
            continue
        held = out.get(row.week)
        if held is None or row.revision > held.revision:
            out[row.week] = row
    return out


def default_since(series: Iterable[ScoredWeek], asked: Sequence[Question], *,
                  target: str, first_weeks: int = FIRST_WEEKS) -> datetime.datetime | None:
    """Where new weeks start: the target's earliest scored week, else *first_weeks* back.

    Keyed by the target, not the predictor, so a new predictor version is
    scored over the same span as the one before it and the two compare.
    """
    ats = [datetime.datetime.fromisoformat(row.at) for row in series if row.target == target]
    if ats:
        return min(ats)
    if not asked:
        return None
    return max(question.at for question in asked) - datetime.timedelta(weeks=first_weeks)


@dataclass(frozen=True)
class Plan:
    """What one scoring does: the weeks to ask, and the weeks whose actual changed."""

    new: tuple[Question, ...]                          # not yet in the series: ask these
    revised: tuple[tuple[Question, ScoredWeek], ...]   # in it, with another actual now


def plan(asked: Sequence[Question], held: dict[str, ScoredWeek], *,
         since: datetime.datetime | None) -> Plan:
    """Which questions to ask, and which held rows to revise. Pure.

    A question is new when its week is not held, it can be scored (a next
    week, an actual, no skip) and it was asked at or after *since*. A held
    week is revised when the trace's actual for it differs from the row's,
    whatever *since* says: a revision can reach any week.
    """
    new: list[Question] = []
    revised: list[tuple[Question, ScoredWeek]] = []
    for question in asked:
        row = held.get(question.week.isoformat())
        if row is None:
            if question.actual is not None and not question.skip \
                    and (since is None or question.at >= since):
                new.append(question)
            continue
        if question.skip or question.next_week is None:
            continue
        if question.actual != row.actual:
            revised.append((question, row))
    return Plan(new=tuple(new), revised=tuple(revised))


def actual_known_at(readings: Iterable[Reading], absent: Iterable[Absent], *,
                    target: str) -> dict[datetime.datetime, datetime.datetime]:
    """When each week's latest vintage of *target* became known, a value or a blanking."""
    out: dict[datetime.datetime, datetime.datetime] = {}
    stamps = [(r.series, r.event_time, r.known_at) for r in readings]
    stamps += [(a.series, a.event_time, a.known_at) for a in absent]
    for series, event_time, known_at in stamps:
        if series == target:
            held = out.get(event_time)
            out[event_time] = known_at if held is None else max(held, known_at)
    return out


def rows(the_plan: Plan, steps: Sequence[Step], *, target: str, predictor: str, capture: str,
         known: dict[datetime.datetime, datetime.datetime], held: dict[str, ScoredWeek],
         ts: str) -> list[ScoredWeek]:
    """The rows one scoring appends, in week order, with a running skill. Pure.

    *steps* are the scored answers to ``the_plan.new``, one each. A step with
    no error (the predictor failed, or answered no number) is not written, so
    the week is asked again next time. A revision keeps the held prediction.
    """
    if len(steps) != len(the_plan.new):
        raise ValueError(f"{len(steps)} steps for {len(the_plan.new)} new questions")
    latest_rows = dict(held)
    out: list[ScoredWeek] = []

    def when(question: Question) -> str | None:
        stamp = known.get(question.next_week) if question.next_week is not None else None
        return stamp.isoformat() if stamp is not None else None

    for question, step in zip(the_plan.new, steps, strict=True):
        if step.error is None or step.predicted is None:
            continue
        assert question.next_week is not None
        out.append(ScoredWeek(
            target=target, predictor=predictor, capture=capture,
            week=question.week.isoformat(), at=question.at.isoformat(),
            next_week=question.next_week.isoformat(), predicted=step.predicted,
            actual=step.actual, actual_known_at=when(question), error=step.error,
            climatology=step.climatology, climatology_error=step.climatology_error,
            skill=None, revision=0, note="", ts=ts))
    for question, row in the_plan.revised:
        actual = question.actual
        base = question.climatology
        out.append(ScoredWeek(
            target=target, predictor=predictor, capture=capture, week=row.week, at=row.at,
            next_week=row.next_week, predicted=row.predicted, actual=actual,
            actual_known_at=when(question),
            error=row.predicted - actual if actual is not None else None,
            climatology=base,
            climatology_error=base - actual if base is not None and actual is not None
            else None,
            skill=None, revision=row.revision + 1,
            note="revised" if actual is not None else "withdrawn", ts=ts))
    out.sort(key=lambda item: (item.week, item.revision))
    # The running skill: over the latest row of every week up to and
    # including this one, as the series stood once it was written.
    finished: list[ScoredWeek] = []
    for item in out:
        latest_rows[item.week] = item
        upto = [r for week, r in latest_rows.items() if week <= item.week]
        finished.append(_with_skill(item, upto))
    return finished


def _with_skill(row: ScoredWeek, upto: Sequence[ScoredWeek]) -> ScoredWeek:
    pairs = [(abs(r.error), abs(r.climatology_error)) for r in upto
             if r.error is not None and r.climatology_error is not None]
    model = sum(m for m, _ in pairs) / len(pairs) if pairs else None
    base = sum(c for _, c in pairs) / len(pairs) if pairs else None
    return dataclasses.replace(row, skill=skill(model, base))


def as_steps(series: Iterable[ScoredWeek], *, target: str, predictor: str) -> list[Step]:
    """The series as :class:`~sis.prediction_error.Step` s, the latest row of each week.

    In week order: what :mod:`sis.model_error`'s triggers take. A withdrawn
    week is a step with no error, which the triggers never count.
    """
    held = latest(series, target=target, predictor=predictor)
    out: list[Step] = []
    for week in sorted(held):
        row = held[week]
        out.append(Step(week=datetime.datetime.fromisoformat(row.week),
                        at=datetime.datetime.fromisoformat(row.at), predicted=row.predicted,
                        actual=row.actual, error=row.error, note=row.note,
                        climatology=row.climatology, climatology_error=row.climatology_error))
    return out


@dataclass(frozen=True)
class Scored:
    """What one scoring did, for a log line."""

    predictor: str
    capture: str
    asked: int                 # questions put to the predictor
    rows: tuple[ScoredWeek, ...]

    @property
    def new(self) -> int:
        return sum(1 for row in self.rows if row.revision == 0)

    @property
    def revised(self) -> int:
        return len(self.rows) - self.new

    def report(self) -> str:
        """One line: never silent, even when nothing was new."""
        text = (f"scored predictor {self.predictor} after {self.capture}: "
                f"{self.new} new week(s), {self.revised} revision(s), "
                f"{self.asked} question(s) asked")
        last = next((row for row in reversed(self.rows) if row.skill is not None), None)
        if last is not None:
            text += f"; skill to date {last.skill:+.3f}"
        return text


def score_new(readings: Sequence[Reading], absent: Sequence[Absent], *, source: str,
              ask: Ask, store: EpisodicStore, capture: str, target: str = CALIFORNIA,
              unit: str = "", since: datetime.datetime | None = None,
              telemetry: Telemetry | None = None, clock: Clock | None = None) -> Scored:
    """Score the predictor *source* on what is new since the last scoring, and keep it.

    *ask* puts questions to the predictor (in the sandbox:
    :func:`sis.prediction_error.ask_in_sandbox` bound to the source and its
    entry). *since* defaults to :func:`default_since`. Each appended row is
    also emitted as ``prediction.error`` to *telemetry*, with its predictor,
    capture and revision, and the scoring as ``prediction.scored``.
    """
    predictor = version(source)
    series = store.scored()
    held = latest(series, target=target, predictor=predictor)
    asked = questions(readings, absent, target=target)
    start = since if since is not None else default_since(series, asked, target=target)
    the_plan = plan(asked, held, since=start)
    answers = ask(the_plan.new) if the_plan.new else []
    steps = score(the_plan.new, list(answers), target=target, unit=unit).steps
    known = actual_known_at(readings, absent, target=target)
    new_rows = rows(the_plan, steps, target=target, predictor=predictor, capture=capture,
                    known=known, held=held, ts=now_iso(clock))
    store.append_scored(new_rows)
    if telemetry is not None:
        for row in new_rows:
            telemetry.emit("prediction.error", ref=capture, target=target, unit=unit,
                           predictor=predictor, capture=capture, revision=row.revision,
                           week=row.week, at=row.at, predicted=row.predicted,
                           actual=row.actual, error=row.error, climatology=row.climatology,
                           climatology_error=row.climatology_error, skill=row.skill,
                           note=row.note)
        telemetry.emit("prediction.scored", ref=capture, target=target, predictor=predictor,
                       capture=capture, asked=len(the_plan.new),
                       new=sum(1 for row in new_rows if row.revision == 0),
                       revised=sum(1 for row in new_rows if row.revision > 0))
    return Scored(predictor=predictor, capture=capture, asked=len(the_plan.new),
                  rows=tuple(new_rows))


def live_predictor() -> pathlib.Path:
    """The predictor scored after a capture: the built one if any, else the shipped stub."""
    from sis.contract import CA_PRICE_PERSISTENCE
    from sis.paths import PROJECT_ROOT

    built = PROJECT_ROOT / CA_PRICE_PERSISTENCE.entry_module
    if built.exists():
        return built
    assert CA_PRICE_PERSISTENCE.stub_candidate_path is not None
    return PROJECT_ROOT / CA_PRICE_PERSISTENCE.stub_candidate_path


def last_capture(lines: Sequence[dict[str, Any]]) -> str:
    """The id of a trace's last capture: what a row was scored after."""
    captures = [line for line in lines if line.get("kind") == "capture"]
    return str(captures[-1]["capture"]) if captures else "no-capture"


def score_trace(trace: pathlib.Path, *, rules: SourceRules, predictor: pathlib.Path,
                store: EpisodicStore, since: datetime.datetime | None = None,
                timeout_s: float = 120.0, telemetry: Telemetry | None = None,
                target: str = CALIFORNIA) -> Scored:
    """Read *trace* back through the door and :func:`score_new` the *predictor* file on it.

    The predictor runs in a sandbox worker. Raises
    :class:`~sis.sensor_trace.TraceRefused` for a trace that is missing or
    does not read back, and ``ValueError`` for a store that keeps nothing.
    """
    from sis import sensor_trace
    from sis.contract import CA_PRICE_PERSISTENCE
    from sis.episodic import NullEpisodicStore
    from sis.prediction_error import ask_in_sandbox

    if isinstance(store, NullEpisodicStore):
        raise ValueError("episodic.store is none, so there is no series to keep")
    lines = sensor_trace.load(trace)
    if not lines:
        raise sensor_trace.TraceRefused(
            f"no trace at {trace}: capture one first (scripts/capture_sensor.py)")
    readings, absent = sensor_trace.read_back(lines, rules)
    source = predictor.read_text(encoding="utf-8")
    units = {r.unit for r in readings if r.series == target}

    def ask(asked: Sequence[Question]) -> list[Any]:
        return ask_in_sandbox(source, CA_PRICE_PERSISTENCE.entry, asked, timeout_s=timeout_s)

    return score_new(readings, absent, source=source, ask=ask, store=store,
                     capture=last_capture(lines), target=target, unit=", ".join(sorted(units)),
                     since=since, telemetry=telemetry)


__all__ = ["FIRST_WEEKS", "Plan", "Scored", "actual_known_at", "as_steps", "default_since",
           "last_capture", "latest", "live_predictor", "plan", "rows", "score_new",
           "score_trace", "version"]
