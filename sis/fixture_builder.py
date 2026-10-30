"""sis.fixture_builder — backtest fixtures from a sensor trace (OMNI-35).

A trace (:mod:`sis.sensor_trace`) holds readings; a fixture asks a question of
them. This module turns the one into the other, for the shape the backtest gate
consumes (``specs/README.md``, :mod:`sis.backtest` schema 1): one episode per
question, its inputs in a ``_fixture.json`` and the recorded answer apart in an
``_expect.json``, so "show the inputs, never the answers" stays a file rule.

**The question.** At the moment a target series' value for week *w* became
known, given everything known then, what is its value for week *w* + 1? The
inputs are the as-of view at that moment (:func:`sis.replay.as_of`): every
series, latest vintage per period, nothing not yet known, back to a lookback.
The answer is the value for *w* + 1, as the trace holds it now. A question may
wait (``ask_after``) for an input published later in the week: the wholesale
price comes out the day after the retail one (OMNI-163).

**A private fixture** (``build(private=True)``) shows data that may be
modelled, never redistributed, such as the licensed LA spot price (OMNI-163).
Its episodes go under ``private/``, which is gitignored under ``specs/``, and
``private.json`` lists each file with its sha256: that index is committed, and
the backtest gate refuses a file that differs from it
(:func:`sis.backtest.private_backtests`).

**A reconstructed ``known_at`` is checked against release evidence first.**
Backfilled history carries times estimated from a publication schedule, and an
irregular delay makes an estimate too early: the stocks for the week ending
2022-06-17 came out on 2022-06-29. So a fixture is built only with a release
calendar a human has checked (``checked_by``, ``checked_on``), and every
reconstructed reading it includes gets ``known_at`` = the later of the trace's
and the end of its release day in the calendar's timezone. That is never earlier
than the truth, whatever hour the release came out. A reconstructed reading the
calendar does not cover refuses the build.

**What a fixture holds is the publisher's content as published**, and what it
does not know it says: values are the vintage the trace holds, which for
backfilled history is the source's current one (a value revised before the
first capture appears at its revised value; see ``docs/SENSOR_RUNBOOK.md``).

Deterministic: the same trace and calendar give byte-identical files.

Pure: no I/O. Guardrail code (POLICY-FORBIDDEN): it writes the exam's inputs.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sis.backtest import FIXTURE_SCHEMA, PRIVATE_DIR, PRIVATE_INDEX, PRIVATE_INDEX_SCHEMA
from sis.clock import parse_event_time
from sis.contract_author import ContractDraft
from sis.ports import Reading
from sis.replay import as_of
from sis.sensor_input import SIMULATED_SOURCE, Absent, SourceRules, is_simulated
from sis.sensor_trace import read_back

CALENDAR_SCHEMA = 1
CALENDAR_FILE = "release_calendar.json"


class FixtureRefused(ValueError):
    """The fixture cannot be built honestly from this trace and calendar."""


@dataclass(frozen=True)
class ReleaseCalendar:
    """When each period of each series was actually released, as a human checked it.

    ``releases`` maps a series to {period date: release date}, both ISO dates in
    ``timezone``. Parsed from the JSON a human reviews, which travels into the
    fixture's directory as evidence.
    """

    timezone: ZoneInfo
    checked_by: str
    checked_on: str
    releases: Mapping[str, Mapping[str, str]]
    raw: str

    @classmethod
    def parse(cls, raw: str) -> ReleaseCalendar:
        try:
            data = json.loads(raw)
        except ValueError:
            raise FixtureRefused("the release calendar is not JSON") from None
        if not isinstance(data, dict) or data.get("schema") != CALENDAR_SCHEMA:
            raise FixtureRefused(f"the release calendar is not schema {CALENDAR_SCHEMA}")
        checked_by, checked_on = data.get("checked_by"), data.get("checked_on")
        if not (isinstance(checked_by, str) and checked_by.strip()
                and isinstance(checked_on, str) and checked_on.strip()):
            raise FixtureRefused(
                "the release calendar has not been checked by a human (checked_by and "
                "checked_on are empty): its dates decide what a model is shown, so a "
                "person verifies them against the sources before anything is built")
        releases: dict[str, dict[str, str]] = {}
        for report, body in dict(data.get("reports", {})).items():
            dates = {str(k): str(v) for k, v in dict(body.get("releases", {})).items()}
            for period, released in dates.items():
                if datetime.date.fromisoformat(released) < datetime.date.fromisoformat(period):
                    raise FixtureRefused(f"{report}: {period} released {released}, before "
                                         "the period it describes")
            for series in body.get("series", []):
                releases.setdefault(str(series), {}).update(dates)
        return cls(timezone=ZoneInfo(str(data.get("timezone", "UTC"))),
                   checked_by=checked_by, checked_on=checked_on, releases=releases, raw=raw)

    def released(self, series: str, period: datetime.datetime) -> datetime.datetime | None:
        """The end of *period*'s release day, or None if the calendar does not say."""
        day = self.releases.get(series, {}).get(period.astimezone(self.timezone).date().isoformat())
        if day is None:
            return None
        return datetime.datetime.combine(datetime.date.fromisoformat(day),
                                         datetime.time(23, 59, 59), tzinfo=self.timezone)


@dataclass(frozen=True)
class Question:
    """What the fixture asks: the target series, the weeks predicted, the lookback."""

    target: str
    first: datetime.date          # the first week predicted
    last: datetime.date           # the last week predicted, included
    lookback: datetime.timedelta = datetime.timedelta(weeks=52)
    horizon: datetime.timedelta = datetime.timedelta(weeks=1)
    # The series an episode shows, the target always among them. A fixture
    # committed under specs/ is public, so it names only series that may be
    # redistributed (OMNI-163); None shows every series in the trace.
    inputs: frozenset[str] | None = None
    # How long after the week's value became known the question is asked. A
    # question that also shows a series released later waits for it: the LA
    # spot price for Monday is out on Wednesday, after the retail price
    # (OMNI-163). The answer must still be unknown then (M32).
    ask_after: datetime.timedelta = datetime.timedelta(0)


def _key(item: Reading | Absent) -> tuple[str, datetime.datetime, datetime.datetime, str]:
    return (item.series, item.event_time, item.known_at,
            "reading" if isinstance(item, Reading) else "absent")


def _reconstructed(lines: Sequence[Mapping[str, Any]]) -> set[tuple[str, datetime.datetime,
                                                                     datetime.datetime, str]]:
    out = set()
    for line in lines:
        if line["kind"] != "capture" and line.get("reconstructed") is True:
            out.add((str(line["series"]),
                     parse_event_time(str(line["event_time"]), where="event_time"),
                     parse_event_time(str(line["known_at"]), where="known_at"),
                     str(line["kind"])))
    return out


def _adjusted(item: Reading | Absent, calendar: ReleaseCalendar,
              reconstructed: bool) -> Reading | Absent:
    """*item* with ``known_at`` no earlier than its checked release."""
    if not reconstructed:
        return item       # known at the fetch that saw it: already an upper bound
    released = calendar.released(item.series, item.event_time)
    if released is None:
        raise FixtureRefused(
            f"{item.series} {item.event_time.date().isoformat()}: a reconstructed "
            "known_at the release calendar does not cover")
    if released <= item.known_at:
        return item
    if isinstance(item, Reading):
        return Reading(source=item.source, series=item.series, value=item.value,
                       unit=item.unit, event_time=item.event_time, known_at=released)
    return Absent(item.source, item.series, item.event_time, released, item.marker)


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=1, allow_nan=False) + "\n"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _absent_record(item: Absent) -> dict[str, Any]:
    return {"source": item.source, "series": item.series, "marker": item.marker,
            "event_time": item.event_time.isoformat(), "known_at": item.known_at.isoformat()}


def build(lines: Sequence[Mapping[str, Any]], rules: SourceRules, calendar: ReleaseCalendar,
          question: Question, *, name: str, spec_ref: str,
          private: bool = False) -> ContractDraft:
    """The fixture *question* asks of the trace *lines*, as a draft to stage.

    The trace is read back through the door first (:func:`sis.sensor_trace.read_back`),
    so a trace that was edited or cut short refuses here, not in a gate. With
    *private*, the episodes go under ``private/`` and ``private.json`` indexes
    them by digest (module docstring).
    """
    if is_simulated(rules.source):
        # D4: a fixture is acceptance evidence, a scenario only refutes (OMNI-34).
        raise FixtureRefused(f"source {SIMULATED_SOURCE!r} is a generated scenario; a "
                             "backtest fixture is built from recorded data only")
    readings, absent = read_back(lines, rules)
    flagged = _reconstructed(lines)
    # Only what some episode can include needs release evidence: a reading
    # outside every lookback, or after the last week asked about, is never shown.
    first_shown = question.first - question.horizon - question.lookback
    every: list[Reading | Absent] = [*readings, *absent]
    inputs = None if question.inputs is None else question.inputs | {question.target}
    shown = [item for item in every
             if (inputs is None or item.series in inputs)
             and first_shown <= item.event_time.astimezone(calendar.timezone).date()
             <= question.last]
    adjusted = [_adjusted(item, calendar, _key(item) in flagged) for item in shown]
    targets = sorted((r for r in adjusted if isinstance(r, Reading)
                      and r.series == question.target), key=lambda r: (r.event_time, r.known_at))
    by_week: dict[datetime.date, list[Reading]] = {}
    for reading in targets:
        by_week.setdefault(reading.event_time.astimezone(calendar.timezone).date(),
                           []).append(reading)

    files: dict[str, str] = {}
    episodes: list[str] = []
    week = question.first
    while week <= question.last:
        asked = week - question.horizon
        if asked not in by_week or week not in by_week:
            raise FixtureRefused(f"{question.target}: no value for {asked} or {week} in "
                                 "the trace; the window has a gap")
        # When the value for the week before became known (its first vintage),
        # the question is asked; the answer is the value for this week as the
        # trace holds it now (its latest vintage). No vintage of this week may
        # be known by then: the view would show it (M32, OMNI-164).
        at = by_week[asked][0].known_at + question.ask_after
        answer = by_week[week][-1]
        if by_week[week][0].known_at <= at:
            raise FixtureRefused(f"{question.target} {week}: known before the question "
                                 "about it was asked")
        view = as_of([r for r in adjusted if isinstance(r, Reading)],
                     [a for a in adjusted if isinstance(a, Absent)], at)
        floor = asked - question.lookback
        state = {
            "as_of": at.isoformat(),
            "readings": [r.as_record() for r in view.readings
                         if r.event_time.astimezone(calendar.timezone).date() >= floor],
            "absent": [_absent_record(a) for a in view.absent
                       if a.event_time.astimezone(calendar.timezone).date() >= floor],
        }
        stem = f"{PRIVATE_DIR}/backtests/{week.isoformat()}" if private \
            else f"backtests/{week.isoformat()}"
        files[f"{stem}_fixture.json"] = _dump(
            {"schema": FIXTURE_SCHEMA, "event_time": at.isoformat(), "args": [state]})
        files[f"{stem}_expect.json"] = _dump({"schema": FIXTURE_SCHEMA, "value": answer.value})
        episodes.append(week.isoformat())
        week += question.horizon
    if private:
        index = []
        for week_name in episodes:
            fixture = f"{PRIVATE_DIR}/backtests/{week_name}_fixture.json"
            expect = f"{PRIVATE_DIR}/backtests/{week_name}_expect.json"
            index.append({"name": week_name, "fixture": fixture, "expect": expect,
                          "fixture_sha256": _digest(files[fixture]),
                          "expect_sha256": _digest(files[expect])})
        files[PRIVATE_INDEX] = _dump({"schema": PRIVATE_INDEX_SCHEMA, "episodes": index})
    files[CALENDAR_FILE] = calendar.raw
    files["README.md"] = _readme(lines, calendar, question, name=name, spec_ref=spec_ref,
                                 episodes=episodes, private=private)
    return ContractDraft(name=name, spec_ref=spec_ref, files=files)


def _readme(lines: Sequence[Mapping[str, Any]], calendar: ReleaseCalendar, question: Question,
            *, name: str, spec_ref: str, episodes: Sequence[str], private: bool) -> str:
    captures = [line for line in lines if line["kind"] == "capture"]
    provenance = []
    for line in captures:
        source = dict(line.get("provenance") or {})
        provenance.append(f"- capture `{line.get('capture')}`, fetched {line.get('fetched_at')}: "
                          f"sha256 `{source.get('sha256')}`, {source.get('url')}")
    attributions = sorted({str(dict(line.get("provenance") or {}).get("attribution"))
                           for line in captures} - {"None"})
    trace = hashlib.sha256("".join(_dump(dict(line)) for line in lines).encode()).hexdigest()
    return "".join([
        f"# `{name}`: recorded fixture ({spec_ref})\n\n",
        f"Built by `scripts/build_fixture.py` from a sensor trace. **{len(episodes)} "
        "episodes**, all held out (`Split.HOLDOUT`).\n\n",
        f"**The question.** {_when(question)}, given everything known then (",
        "every series" if question.inputs is None
        else ", ".join(f"`{series}`" for series in sorted(question.inputs | {question.target})),
        f"; latest vintage per period, back {question.lookback.days} days), what is "
        f"`{question.target}`'s value for the next week? Each "
        f"`{PRIVATE_DIR + '/' if private else ''}backtests/<week>_fixture.json` holds that "
        "as-of view in `args[0]`; each `<week>_expect.json` holds the answer, the value "
        "for `<week>`.\n\n",
        *([f"**Private.** The episodes show data that may be modelled but not "
           f"redistributed, so `{PRIVATE_DIR}/` is gitignored and kept in the private "
           f"artifacts bucket; `{PRIVATE_INDEX}` lists each file's sha256, is committed, "
           "and the backtest gate refuses a file that differs from it.\n\n"]
          if private else []),
        "**Replay rule** (`sis/replay.py`): by `known_at`; `event_time` is data; mixed "
        "anchors (Friday stocks, Monday prices) are not aligned.\n\n",
        f"**Release check.** Reconstructed times were checked against `{CALENDAR_FILE}` "
        f"(checked by {calendar.checked_by} on {calendar.checked_on}): each reading's "
        "`known_at` is the later of the trace's and the end of its release day.\n\n",
        "**Vintages.** Values are the vintage the trace holds; backfilled history is the "
        "source's current vintage, so a value revised before the first capture appears "
        "revised.\n\n",
        "## Provenance\n\n",
        *[f"{line}\n" for line in provenance],
        f"- trace lines, canonical sha256 `{trace}`\n",
        *[f"- {text}\n" for text in attributions],
        "\nEIA's content is reproduced as published: nothing here is derived from it "
        "and labelled EIA's.\n\n",
        "## Episodes\n\n",
        "".join(f"- {week}\n" for week in episodes),
    ])


def _when(question: Question) -> str:
    became = f"`{question.target}`'s value for a week became known"
    if not question.ask_after:
        return f"When {became}"
    return f"{question.ask_after.total_seconds() / 3600:g} hours after {became}"


__all__ = ["CALENDAR_FILE", "FixtureRefused", "Question", "ReleaseCalendar", "build"]
