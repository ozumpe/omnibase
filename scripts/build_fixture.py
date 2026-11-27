"""scripts/build_fixture.py — build a fixture from the EIA trace, and stage it for review.

    poetry run python scripts/build_fixture.py
    poetry run python scripts/build_fixture.py --fixture ca_price_wholesale [--last 2026-10-19]
    poetry run python scripts/build_fixture.py --trace PATH --calendar PATH

Reads the trace through the sanitising door, checks every reconstructed
``known_at`` a fixture shows against the release calendar (which must be
checked: ``checked_by``, ``checked_on``), and builds one held-out episode
per week. Two fixtures:

- ``ca_price_next_week`` (OMNI-35, the default): 8 August to 28 November 2022.
  Given everything known when California's price for a week became known, its
  price the next week. Public series only, committed under ``specs/``.
- ``ca_price_wholesale`` (OMNI-163): every week from 6 January 2020 to the last
  the trace answers and the release calendar covers (or ``--last``; the
  output says when the calendar cut it short), asked a day later, on Thursday
  at 17:00 ET when Monday's LA spot price is out, and showing it. The spot price is
  licensed (may be modelled, never redistributed), so the episodes are
  private: under ``private/``, gitignored, with ``private.json`` listing their
  digests for git (``docs/SENSOR_RUNBOOK.md``).

``--training`` builds the fixture's training split instead (OMNI-205): the
public series known just before its first held-out question, from the first
week the calendar covers (or ``--first``), into ``training.json``. The
proposer sees it; no gate reads it. With ``--events`` (OMNI-227) it also shows
the refinery-outage event series from ``runtime/sensor_traces/events.jsonl``
(``sis.events``; ``--events-trace`` for another path), in the split only.

``--rotate`` replaces a private fixture's held-out set (OMNI-225, D5): the
weeks after the committed set's last one become the new held-out episodes,
and every week before them, the old held-out set included, goes into a new
training split. Both are staged as one draft, so one review and one promotion
move them together. It refuses until there are
``brakes.holdout_rotation_min_weeks`` such weeks, and says when there will be.
A new held-out fixture has a new digest, so its holdout budget starts at zero
(``sis/holdout.py``); the old weeks are never judged again.

Once a private fixture is promoted, a plain build or ``--training`` keeps its
committed held-out weeks (``specs/<fixture>/private.json``) unless ``--last``
says otherwise: adding fresh weeks to a judged set would restart its count on
data the loop has seen judged, so only ``--rotate`` takes them.

A calendar signed by CI (``checked_by`` ``CI, sources sha256:…``, OMNI-190) is
checked again here, offline, against the saved sources it names
(``scripts/draft_release_calendar.py check``): a claim that does not hold is
refused, so it cannot simply be typed in.

The draft is staged under ``runtime/contract_staging/<fixture>/``. Nothing
reaches ``specs/`` from here. A human reads the staged files, then runs
``python -m sis.contract_author promote <fixture> --digest <digest> --reason
"..."`` with the digest printed below (OMNI-68).

Exit 1 if the fixture cannot be built honestly (an unchecked calendar, a
reading it does not cover, a trace that does not read back), with the reason.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
from dataclasses import dataclass, replace
from typing import Any

# Allow running as a plain script: put the repo root (parent of scripts/) on the
# path, and scripts/ itself for the calendar check.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import draft_release_calendar  # noqa: E402

from sis import (  # noqa: E402
    config,
    holdout,
    sensor_trace,  # noqa: E402
)
from sis.adapters_real import EIA_PUBLIC_SERIES, EIA_RULES, EIA_SERIES, EIA_TRACE  # noqa: E402
from sis.backtest import PRIVATE_INDEX_SCHEMA, TRAINING_FILE  # noqa: E402
from sis.clock import parse_event_time  # noqa: E402
from sis.contract_author import ContractDraft, stage  # noqa: E402
from sis.fixture_builder import (  # noqa: E402
    FixtureRefused,
    Question,
    ReleaseCalendar,
    build,
    build_training,
)
from sis.paths import SPECS_DIR  # noqa: E402
from sis.sensor_input import SourceRules  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parent / "data"
CALIFORNIA = "PET.EMM_EPMRR_PTE_SCA_DPG.W"


@dataclass(frozen=True)
class Fixture:
    question: Question
    calendar: pathlib.Path
    spec_ref: str
    private: bool = False
    # None: the last week the trace has an answer for.
    last: datetime.date | None = None


FIXTURES = {
    # Committed under specs/, so it shows only what may be redistributed:
    # never a licensed series such as the LA spot price (OMNI-163). The build
    # refuses one in a public fixture in any case (OMNI-187).
    "ca_price_next_week": Fixture(
        Question(target=CALIFORNIA, first=datetime.date(2022, 8, 8),
                 last=datetime.date(2022, 11, 28), inputs=EIA_PUBLIC_SERIES),
        DATA / "eia_release_calendar_2021_2022.json", "OMNI-35",
        last=datetime.date(2022, 11, 28)),
    # The spot price for Monday comes out on Wednesday and is taken as known on
    # Thursday at 17:00 ET (sis.adapters_real.eia_published), a day after the
    # retail price: the question waits for it.
    "ca_price_wholesale": Fixture(
        Question(target=CALIFORNIA, first=datetime.date(2020, 1, 6),
                 last=datetime.date(2020, 1, 6), inputs=frozenset(EIA_SERIES),
                 ask_after=datetime.timedelta(days=1)),
        DATA / "eia_release_calendar_2018_2026.json", "OMNI-163", private=True),
}


# Where each training split begins (OMNI-205): the first week the release
# calendar covers, since every reading shown is checked against it. Extend the
# calendar back (draft_release_calendar.py) for a longer split, then pass --first.
TRAINING_FIRST = {"ca_price_wholesale": datetime.date(2018, 12, 3)}


# Where promoted fixtures are: a private one's held-out weeks are read from
# its committed index (tests point this elsewhere).
SPECS = SPECS_DIR


def _committed_weeks(name: str) -> tuple[datetime.date, ...]:
    """The held-out weeks *name*'s committed private index lists, or () if none."""
    path = SPECS / name / "private.json"
    if not path.exists():
        return ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict) and data.get("schema") == PRIVATE_INDEX_SCHEMA
        return tuple(sorted(datetime.date.fromisoformat(str(e["name"]))
                            for e in data["episodes"]))
    except (OSError, ValueError, KeyError, TypeError, AssertionError) as exc:
        raise FixtureRefused(f"{path}: not a readable private index whose episodes are "
                             f"named for their weeks ({type(exc).__name__})") from None


def _answered(lines: list[dict[str, object]], target: str) -> list[datetime.date]:
    """Every week of *target* the trace holds a value for, in order."""
    weeks = {parse_event_time(str(line["event_time"]), where="event_time").date()
             for line in lines if line["kind"] == "reading" and line.get("series") == target}
    if not weeks:
        raise FixtureRefused(f"the trace holds no {target}")
    return sorted(weeks)


def _past_the_calendar(lines: list[dict[str, object]], calendar: ReleaseCalendar,
                       question: Question) -> datetime.date | None:
    """The first day after the calendar's last entry for its series that a
    reconstructed value the question shows describes, or None if there is none.

    A trace captured from scratch marks all its history reconstructed, and the
    build refuses a reconstructed value the calendar does not cover. A calendar
    ends somewhere, so without ``--last`` every trace captured after its end was
    refused (OMNI-178, L63). Only the end counts here: a gap inside the calendar,
    or a series it does not list at all, still refuses in the build.
    """
    inputs = None if question.inputs is None else question.inputs | {question.target}
    ends = {series: max(datetime.date.fromisoformat(period) for period in periods)
            for series, periods in calendar.releases.items() if periods}
    first: datetime.date | None = None
    for line in lines:
        if line["kind"] == "capture" or line.get("reconstructed") is not True:
            continue
        series = str(line["series"])
        if (inputs is not None and series not in inputs) or series not in ends:
            continue
        day = parse_event_time(str(line["event_time"]), where="event_time") \
            .astimezone(calendar.timezone).date()
        if day > ends[series] and (first is None or day < first):
            first = day
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/build_fixture.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixture", choices=sorted(FIXTURES), default="ca_price_next_week")
    parser.add_argument("--trace", type=pathlib.Path, default=EIA_TRACE)
    parser.add_argument("--calendar", type=pathlib.Path, default=None)
    parser.add_argument("--training", action="store_true",
                        help="build the fixture's training split instead (OMNI-205): public "
                             "series known before its first held-out question")
    parser.add_argument("--first", type=datetime.date.fromisoformat, default=None,
                        help="with --training or --rotate, the earliest period shown (default: the "
                             "first week the release calendar covers)")
    parser.add_argument("--events", action="store_true",
                        help="with --training or --rotate, also show the refinery-outage "
                             "event series in the training split (OMNI-227)")
    parser.add_argument("--events-trace", type=pathlib.Path, default=None,
                        help="the event trace --events reads (default: "
                             "runtime/sensor_traces/events.jsonl)")
    parser.add_argument("--rotate", action="store_true",
                        help="replace a private fixture's held-out weeks with the fresh "
                             "weeks after them, and move the old ones into its training "
                             "split, as one draft (OMNI-225)")
    parser.add_argument("--last", type=datetime.date.fromisoformat, default=None,
                        help="the last week predicted (default: the fixture's own, or the "
                             "last week the trace answers that the release calendar "
                             "still covers)")
    args = parser.parse_args(argv)
    fixture = FIXTURES[args.fixture]
    try:
        lines = sensor_trace.load(args.trace)
        if not lines:
            raise FixtureRefused(f"no trace at {args.trace}: capture one first "
                                 "(scripts/capture_sensor.py)")
        calendar_path = args.calendar or fixture.calendar
        calendar = ReleaseCalendar.parse(calendar_path.read_text(encoding="utf-8"))
        if draft_release_calendar.is_ci_claim(calendar.checked_by):
            try:
                draft_release_calendar.check(calendar_path)
            except draft_release_calendar.CalendarRefused as exc:
                raise FixtureRefused(f"the release calendar claims CI's check, which it "
                                     f"does not pass: {exc}") from None
        committed = _committed_weeks(args.fixture) if fixture.private else ()
        if args.rotate:
            return _stage_rotation(args, lines, calendar, fixture, committed)
        if committed:
            # The promoted split, not the one this file started with (OMNI-225).
            fixture = replace(fixture, question=replace(fixture.question, first=committed[0]),
                              last=fixture.last or committed[-1])
        if args.training:
            return _stage_training(args, lines, calendar, fixture)
        last, note = args.last or fixture.last, None
        if last is None:
            weeks = _answered(lines, fixture.question.target)
            beyond = _past_the_calendar(lines, calendar, fixture.question)
            covered = [week for week in weeks if beyond is None or week < beyond]
            if not covered or covered[-1] < fixture.question.first:
                raise FixtureRefused(
                    f"the release calendar ends before {fixture.question.first}, the "
                    "first week asked about: extend it (draft_release_calendar.py)")
            last = covered[-1]
            if covered[-1] != weeks[-1]:
                note = (f"last week: {last}, the last before {beyond}, which the release "
                        f"calendar does not reach; the trace answers to {weeks[-1]}. "
                        "Extend the calendar (draft_release_calendar.py) to build further.")
        question = replace(fixture.question, last=last)
        draft = build(lines, EIA_RULES, calendar, question, name=args.fixture,
                      spec_ref=fixture.spec_ref, private=fixture.private)
    except (FixtureRefused, sensor_trace.TraceRefused) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if note:
        print(note)
    staged = stage(draft)
    built = {name.rsplit("/", 1)[-1].removesuffix("_fixture.json")
             for name in staged.files if name.endswith("_fixture.json")}
    asked = [question.first + n * question.horizon
             for n in range((question.last - question.first) // question.horizon + 1)]
    skipped = [week.isoformat() for week in asked if week.isoformat() not in built]
    print(f"staged {staged.name}: {len(built)} episodes in {staged.directory}")
    if skipped:
        print(f"skipped {len(skipped)} week(s), each already known when the question about "
              f"it would be asked (listed in the README): {', '.join(skipped)}")
    if fixture.private:
        print(f"private: after promotion, upload specs/{staged.name}/private/ to the "
              f"artifacts bucket (docs/SENSOR_RUNBOOK.md); git keeps only "
              f"specs/{staged.name}/private.json and the public files")
    print(f"digest: {staged.digest}")
    print(f"Review it: python -m sis.contract_author review {staged.name}")
    return 0


def _covered(lines: list[dict[str, object]], calendar: ReleaseCalendar,
             question: Question) -> list[datetime.date]:
    """Every week the trace answers that the release calendar still covers."""
    weeks = _answered(lines, question.target)
    beyond = _past_the_calendar(lines, calendar, question)
    return [week for week in weeks if beyond is None or week < beyond]


def _stage_rotation(args: argparse.Namespace, lines: list[dict[str, object]],
                    calendar: ReleaseCalendar, fixture: Fixture,
                    committed: tuple[datetime.date, ...]) -> int:
    """Stage a new held-out set of fresh weeks and the training split before it (OMNI-225)."""
    if not fixture.private:
        raise FixtureRefused(f"{args.fixture} is public: only a private fixture's held-out "
                             "set is budgeted and rotated")
    first = args.first or TRAINING_FIRST.get(args.fixture)
    if first is None:
        raise FixtureRefused(f"{args.fixture} has no training split: pass --first")
    if not committed:
        raise FixtureRefused(f"no promoted held-out set for {args.fixture} "
                             f"({SPECS / args.fixture / 'private.json'}): build and promote "
                             "one first")
    available = _covered(lines, calendar, fixture.question)
    if args.last is not None:
        available = [week for week in available if week <= args.last]
    plan = holdout.plan_rotation(committed, available,
                                 int(config.get("brakes.holdout_rotation_min_weeks")),
                                 fixture.question.horizon)
    if not plan.ready or plan.last is None:
        raise FixtureRefused(plan.describe())
    question = replace(fixture.question, first=plan.first, last=plan.last)
    held = build(lines, EIA_RULES, calendar, question, name=args.fixture,
                 spec_ref=fixture.spec_ref, private=True)
    training = build_training(lines, EIA_RULES, calendar, question, first=first,
                              name=args.fixture, spec_ref=fixture.spec_ref,
                              context=_context(args))
    clash = set(held.files) & set(training.files)
    if clash:
        raise FixtureRefused(f"the held-out and training drafts both write {sorted(clash)}")
    staged = stage(ContractDraft(name=args.fixture, spec_ref=fixture.spec_ref,
                                 files={**held.files, **training.files}))
    built = sum(1 for name in staged.files if name.endswith("_fixture.json"))
    print(plan.describe())
    print(f"staged {staged.name}: {built} held-out episodes and the training split from "
          f"{first}, in {staged.directory}")
    print("After promotion the new fixture's holdout budget starts at zero; the old "
          "held-out weeks are never judged again.")
    print(f"private: after promotion, upload specs/{staged.name}/private/ to the artifacts "
          f"bucket (docs/SENSOR_RUNBOOK.md); git keeps specs/{staged.name}/private.json, "
          f"{TRAINING_FILE}, README.md and the calendar")
    print(f"digest: {staged.digest}")
    print(f"Review it: python -m sis.contract_author review {staged.name}")
    return 0


def _context(args: argparse.Namespace) -> list[tuple[list[dict[str, Any]], SourceRules]]:
    """The other sources a training split shows (OMNI-227): the event trace with --events."""
    if not args.events:
        return []
    from sis.events import EVENT_RULES, EVENTS_TRACE

    path = args.events_trace or EVENTS_TRACE
    lines = sensor_trace.load(path)
    if not lines:
        raise FixtureRefused(f"no event trace at {path}: run scripts/capture_sensor.py first")
    return [(lines, EVENT_RULES)]


def _stage_training(args: argparse.Namespace, lines: list[dict[str, object]],
                    calendar: ReleaseCalendar, fixture: Fixture) -> int:
    """Build, stage and report the training split *fixture*'s exam is held out from."""
    first = args.first or TRAINING_FIRST.get(args.fixture)
    if first is None:
        raise FixtureRefused(f"{args.fixture} has no training split: pass --first")
    draft = build_training(lines, EIA_RULES, calendar, fixture.question, first=first,
                           name=args.fixture, spec_ref=fixture.spec_ref,
                           context=_context(args))
    staged = stage(draft)
    print(f"staged {staged.name}: training split from {first}, known before the first "
          f"held-out question ({fixture.question.first}), in {staged.directory}")
    print(f"digest: {staged.digest}")
    print(f"Review it: python -m sis.contract_author review {staged.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
