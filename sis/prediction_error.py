"""sis.prediction_error — how wrong a predictor was, week by week (OMNI-36).

The Phase-A milestone: drive a predictor from a recorded trace or a generated
scenario, compute its error, and report it. **Nothing acts on the number.** An
error budget and skill against climatology are Phase B's triggers (D9); this
module starts no cycle, files nothing and pages no one, and imports nothing
that could (a test holds it to that).

**Climatology** (OMNI-195, the baseline D9's skill trigger measures against)
is asked every question too, on the host from the same as-of view, never in a
sandbox: the latest value plus the mean change from the same ISO week to the
next in up to :data:`CLIMATOLOGY_YEARS` earlier years (:func:`climatology`).
A seasonal change rather than a seasonal level, so a market that moved a
dollar since last year is not forecast to move back. A week with no earlier
year to learn from has no climatology, and no skill is claimed for it. The
skill, ``1 - MAE(model) / MAE(climatology)`` over the weeks both were scored
on, is in the summary next to bias, MAE and RMSE: above 0 the model beats
climatology, at 0 it is no better, below 0 worse.

**The question** is the fixture builder's (:mod:`sis.fixture_builder`): at the
moment the target series' value for week *w* first became known, given
everything known then, what is its value for week *w* + 1? The predictor is
shown the as-of view at that moment (:func:`sis.replay.as_of`), so it is never
handed a reading before its ``known_at``. Its answer is compared with the value
for *w* + 1 as the trace holds it now, its latest vintage. A week with no value
for *w* + 1 (missing, or withdrawn by now) has nothing to compare with and is
counted as skipped, never scored as zero. Nor is a week whose next value was
already known when the question was asked, any vintage of it: a capture that
skipped a week, or a late release, brings two weeks in at one moment, and the
view would show the predictor its own answer (M34, OMNI-170; M32 in the
fixture builder). It is still asked, so the answers line up, and skipped.

**The error is signed**, prediction minus actual, in the series' unit ($/gal
for a price), so a model that is always late in a rising market shows as
biased, not merely noisy.

**The predictor is untrusted code.** It is a SOFT target the loop may rewrite,
so it runs in a :class:`~sis.sandbox_worker.SandboxWorker`, never in this
process; only JSON crosses. :func:`score` takes any callable for the pure
part, which is what the tests give it.

Guardrail code (POLICY-FORBIDDEN): it decides what a predictor is shown and
how it is judged.
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sis.clock import ReplayClock
from sis.ports import Reading, Telemetry
from sis.replay import as_of
from sis.sensor_input import Absent

CALIFORNIA = "PET.EMM_EPMRR_PTE_SCA_DPG.W"
DEFAULT_LOOKBACK = datetime.timedelta(weeks=52)
HORIZON = datetime.timedelta(weeks=1)
# How many earlier years climatology averages the seasonal change over: enough
# to smooth one odd year, few enough to follow a market whose seasons changed.
CLIMATOLOGY_YEARS = 5
# A climatology MAE at or below this is perfect: the rest is float rounding.
_PERFECT = 1e-9
# A week whose next value is dated more than this after it is not "next week":
# a gap in the series, skipped rather than scored against a later week.
_NEXT_WEEK_SLACK = datetime.timedelta(days=3)


@dataclass(frozen=True)
class Question:
    """One step: what the predictor is shown at ``at``, and what came true."""

    week: datetime.datetime            # the last week of the target it is shown
    at: datetime.datetime              # when that week's value first became known
    state: dict[str, Any]              # the as-of view, shaped as a fixture argument
    next_week: datetime.datetime | None
    actual: float | None               # the next week's value, latest vintage; None: skipped
    skip: str = ""                     # why a week with a next value is not scored
    climatology: float | None = None   # the baseline's forecast; None: no earlier year


@dataclass(frozen=True)
class Step:
    """One scored week."""

    week: datetime.datetime
    at: datetime.datetime
    predicted: float | None
    actual: float | None
    error: float | None                # predicted - actual
    note: str = ""                     # why a step was not scored
    climatology: float | None = None   # the baseline's forecast for the same week
    climatology_error: float | None = None  # climatology - actual, when both exist


@dataclass(frozen=True)
class Report:
    """A predictor's errors over a replay, and their summary."""

    target: str
    unit: str
    source: str
    steps: tuple[Step, ...]

    @property
    def scored(self) -> tuple[Step, ...]:
        return tuple(step for step in self.steps if step.error is not None)

    def summary(self) -> dict[str, Any]:
        """Count, bias (mean signed error), MAE and RMSE over the scored weeks.

        And climatology's MAE and the model's skill against it, both over the
        weeks the model and climatology were each scored on (``skill_weeks``):
        a week only one of them answered says nothing about which is better.
        """
        errors = [step.error for step in self.scored if step.error is not None]
        n = len(errors)
        pairs = [(abs(step.error), abs(step.climatology_error)) for step in self.steps
                 if step.error is not None and step.climatology_error is not None]
        model_mae = sum(m for m, _ in pairs) / len(pairs) if pairs else None
        climatology_mae = sum(c for _, c in pairs) / len(pairs) if pairs else None
        return {
            "target": self.target, "unit": self.unit, "source": self.source,
            "weeks": len(self.steps), "scored": n, "skipped": len(self.steps) - n,
            "bias": sum(errors) / n if n else None,
            "mae": sum(abs(e) for e in errors) / n if n else None,
            "rmse": math.sqrt(sum(e * e for e in errors) / n) if n else None,
            "skill_weeks": len(pairs),
            "climatology_mae": climatology_mae,
            "skill": skill(model_mae, climatology_mae),
        }


def skill(model_mae: float | None, climatology_mae: float | None) -> float | None:
    """``1 - model / climatology``; None when either is missing or climatology was perfect.

    A perfect climatology (up to float rounding, :data:`_PERFECT`) leaves no
    headroom to measure against, so no skill is claimed rather than an
    infinity, a division error or a ratio of rounding noise.
    """
    if model_mae is None or climatology_mae is None or climatology_mae <= _PERFECT:
        return None
    return 1.0 - model_mae / climatology_mae


def climatology(values: dict[datetime.datetime, float], week: datetime.datetime, *,
                years: int = CLIMATOLOGY_YEARS) -> float | None:
    """The climatology forecast for the week after *week*, from *values* alone.

    *values* is the target series as a model may see it (an as-of view):
    ``event_time -> value``. The forecast is *week*'s value plus the mean
    change from the same ISO week to the following one over up to *years*
    earlier years that have both. None when *week* has no value or no earlier
    year has the pair. Pure: what it may see is the caller's to limit.
    """
    current = values.get(week)
    if current is None:
        return None
    by_iso = {(day.isocalendar().year, day.isocalendar().week): day for day in values}
    ordered = sorted(values)
    position = {day: i for i, day in enumerate(ordered)}
    iso = week.isocalendar()
    changes: list[float] = []
    for back in range(1, years + 1):
        then = by_iso.get((iso.year - back, iso.week))
        if then is None or position[then] + 1 >= len(ordered):
            continue
        after = ordered[position[then] + 1]
        if after - then > HORIZON + _NEXT_WEEK_SLACK or after >= week:
            continue
        changes.append(values[after] - values[then])
    if not changes:
        return None
    return current + sum(changes) / len(changes)


def _record(reading: Reading) -> dict[str, Any]:
    return reading.as_record()


def _absent_record(item: Absent) -> dict[str, Any]:
    return {"source": item.source, "series": item.series, "marker": item.marker,
            "event_time": item.event_time.isoformat(), "known_at": item.known_at.isoformat()}


def questions(readings: Iterable[Reading], absent: Iterable[Absent], *, target: str = CALIFORNIA,
              start: datetime.datetime | None = None, end: datetime.datetime | None = None,
              lookback: datetime.timedelta = DEFAULT_LOOKBACK,
              clock: ReplayClock | None = None) -> list[Question]:
    """Every question a replay of these vintages asks, in the order it asks them.

    One per week of *target* whose first vintage became known in ``[start,
    end)``, ordered by that moment. *clock*, if given, is advanced to each one
    as it is asked, as :func:`sis.ports.replay` does for a reading. The state
    holds what was known then, back to *lookback* before the week asked about.
    """
    readings, absent = tuple(readings), tuple(absent)
    first: dict[datetime.datetime, datetime.datetime] = {}
    for reading in readings:
        if reading.series == target:
            known = first.get(reading.event_time)
            first[reading.event_time] = reading.known_at if known is None \
                else min(known, reading.known_at)
    weeks = sorted(first)
    latest = as_of(readings, absent, datetime.datetime.max.replace(tzinfo=datetime.UTC))
    now = {r.event_time: r.value for r in latest.readings if r.series == target}
    out: list[Question] = []
    for week, at in sorted(first.items(), key=lambda item: (item[1], item[0])):
        if (start is not None and at < start) or (end is not None and at >= end):
            continue
        if clock is not None:
            clock.advance_to(at)
        view = as_of(readings, absent, at)
        seen = {r.event_time: r.value for r in view.readings if r.series == target}
        floor = week - lookback
        state = {"as_of": at.isoformat(),
                 "readings": [_record(r) for r in view.readings if r.event_time >= floor],
                 "absent": [_absent_record(a) for a in view.absent if a.event_time >= floor]}
        later = [w for w in weeks if w > week]
        next_week = later[0] if later else None
        actual: float | None = None
        skip = ""
        if next_week is not None and next_week - week <= HORIZON + _NEXT_WEEK_SLACK:
            actual = now.get(next_week)     # None: withdrawn by now
            if first[next_week] <= at:
                # Its first vintage, not only its latest: any vintage known by
                # then is in the view (M34, OMNI-170).
                actual, skip = None, "the next week was already known when asked"
        else:
            next_week = None
        out.append(Question(week=week, at=at, state=state, next_week=next_week, actual=actual,
                            skip=skip, climatology=climatology(seen, week)))
    return out


Predict = Callable[[dict[str, Any]], Any]


def score(asked: Sequence[Question], answers: Sequence[Any | BaseException], *,
          target: str = CALIFORNIA, unit: str = "", source: str = "") -> Report:
    """The report for *answers* to *asked*, one each, an exception for a failed call."""
    if len(answers) != len(asked):
        raise ValueError(f"{len(answers)} answers to {len(asked)} questions")
    steps: list[Step] = []
    for question, answer in zip(asked, answers, strict=True):
        # Climatology is scored whatever the predictor answered: its errors are
        # the same series whichever model is judged against them.
        base = question.climatology
        base_error = base - question.actual \
            if base is not None and question.actual is not None else None
        if isinstance(answer, BaseException):
            steps.append(Step(question.week, question.at, None, question.actual, None,
                              f"the predictor failed: {answer}", base, base_error))
            continue
        if isinstance(answer, bool) or not isinstance(answer, int | float) \
                or not math.isfinite(answer):
            steps.append(Step(question.week, question.at, None, question.actual, None,
                              f"not a number: {str(answer)[:40]!r}", base, base_error))
            continue
        predicted = float(answer)
        if question.actual is None:
            steps.append(Step(question.week, question.at, predicted, None, None,
                              question.skip or "no value for the next week to compare with",
                              base, None))
            continue
        steps.append(Step(question.week, question.at, predicted, question.actual,
                          predicted - question.actual, "", base, base_error))
    return Report(target=target, unit=unit, source=source, steps=tuple(steps))


def ask(predict: Predict, asked: Sequence[Question]) -> list[Any | BaseException]:
    """Call a trusted *predict* in this process. For tests and trusted code only."""
    answers: list[Any | BaseException] = []
    for question in asked:
        try:
            answers.append(predict(question.state))
        except Exception as exc:  # noqa: BLE001 - a failed step is a reported outcome
            answers.append(exc)
    return answers


def ask_in_sandbox(source: str, entry: str, asked: Sequence[Question], *,
                   timeout_s: float = 120.0) -> list[Any | BaseException]:
    """Ask an untrusted predictor, *source*, through a sandbox worker: one batch."""
    from sis.sandbox_worker import SandboxWorker

    if not asked:
        return []
    with SandboxWorker(source, entry, call_timeout_s=timeout_s).start() as worker:
        reply = worker.call([[question.state] for question in asked])
    return [result.value if result.ok else RuntimeError(result.error or "failed")
            for result in reply.results]


class Recorder(Protocol):
    """Where a summary is kept: ``SelfModel.record``'s shape (``kind``, ``ref``, details)."""

    def __call__(self, kind: str, ref: str, **detail: Any) -> object: ...


def publish(report: Report, telemetry: Telemetry, *, ref: str,
            record: Recorder | None = None) -> None:
    """Emit the error series to *telemetry*, and its summary to *record* if given.

    One ``prediction.error`` event per week and one ``prediction.summary``.
    *record* is the SelfModel's provenance (``self_model.record.remote``), the
    home the SLO metrics have. Neither is read by anything that acts: that is
    Phase B.
    """
    for step in report.steps:
        telemetry.emit("prediction.error", ref=ref, target=report.target,
                       week=step.week.isoformat(), at=step.at.isoformat(),
                       predicted=step.predicted, actual=step.actual, error=step.error,
                       climatology=step.climatology,
                       climatology_error=step.climatology_error,
                       unit=report.unit, note=step.note)
    summary = report.summary()
    telemetry.emit("prediction.summary", ref=ref, **summary)
    if record is not None:
        record("prediction_error", ref, **summary)


def table(report: Report) -> str:
    """The per-week errors and the summary, as lines for a terminal."""
    lines = [f"{'week':<10}  {'known at':<16}  {'predicted':>9}  {'actual':>9}  {'error':>8}"
             f"  {'climatology':>11}  {'clim err':>8}"]
    for step in report.steps:
        def cell(value: float | None, width: int) -> str:
            return f"{value:>{width}.3f}" if value is not None else f"{'-':>{width}}"
        lines.append(f"{step.week:%Y-%m-%d}  {step.at:%Y-%m-%d %H:%M}  "
                     f"{cell(step.predicted, 9)}  {cell(step.actual, 9)}  "
                     f"{cell(step.error, 8)}  {cell(step.climatology, 11)}  "
                     f"{cell(step.climatology_error, 8)}"
                     + (f"  {step.note}" if step.note else ""))
    s = report.summary()

    def num(value: float | None, sign: str = "") -> str:
        return "-" if value is None else f"{value:{sign}.3f}"

    lines.append(f"{s['scored']} of {s['weeks']} weeks scored ({report.unit}): "
                 f"bias {num(s['bias'], '+')}, MAE {num(s['mae'])}, RMSE {num(s['rmse'])}")
    if s["skill_weeks"]:
        lines.append(f"against climatology over {s['skill_weeks']} weeks: "
                     f"climatology MAE {num(s['climatology_mae'])}, "
                     f"skill {num(s['skill'], '+')} (above 0 beats climatology)")
    else:
        lines.append("against climatology: no week had an earlier year to learn from")
    return "\n".join(lines)


__all__ = ["CALIFORNIA", "CLIMATOLOGY_YEARS", "Question", "Report", "Step", "ask",
           "ask_in_sandbox", "climatology", "publish", "questions", "score", "skill", "table"]
