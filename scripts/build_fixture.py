"""scripts/build_fixture.py — build a fixture from the EIA trace, and stage it for review.

    poetry run python scripts/build_fixture.py
    poetry run python scripts/build_fixture.py --fixture ca_price_wholesale [--last 2026-10-19]
    poetry run python scripts/build_fixture.py --trace PATH --calendar PATH

Reads the trace through the sanitising door, checks every reconstructed
``known_at`` a fixture shows against the release calendar (which a human must
have checked: ``checked_by``, ``checked_on``), and builds one held-out episode
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
import pathlib
import sys
from dataclasses import dataclass, replace

# Allow running as a plain script: put the repo root (parent of scripts/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sis import sensor_trace  # noqa: E402
from sis.adapters_real import EIA_PUBLIC_SERIES, EIA_RULES, EIA_SERIES, EIA_TRACE  # noqa: E402
from sis.clock import parse_event_time  # noqa: E402
from sis.contract_author import stage  # noqa: E402
from sis.fixture_builder import FixtureRefused, Question, ReleaseCalendar, build  # noqa: E402

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
    # never a licensed series such as the LA spot price (OMNI-163).
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
        calendar = ReleaseCalendar.parse(
            (args.calendar or fixture.calendar).read_text(encoding="utf-8"))
        last, note = args.last or fixture.last, None
        if last is None:
            weeks = _answered(lines, fixture.question.target)
            beyond = _past_the_calendar(lines, calendar, fixture.question)
            covered = [week for week in weeks if beyond is None or week < beyond]
            if not covered or covered[-1] < fixture.question.first:
                raise FixtureRefused(
                    f"the release calendar ends before {fixture.question.first}, the "
                    "first week asked about: extend it, and have it checked")
            last = covered[-1]
            if covered[-1] != weeks[-1]:
                note = (f"last week: {last}, the last before {beyond}, which the release "
                        f"calendar does not reach; the trace answers to {weeks[-1]}. "
                        "Extend the calendar (and have it checked) to build further.")
        question = replace(fixture.question, last=last)
        draft = build(lines, EIA_RULES, calendar, question, name=args.fixture,
                      spec_ref=fixture.spec_ref, private=fixture.private)
    except (FixtureRefused, sensor_trace.TraceRefused) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if note:
        print(note)
    staged = stage(draft)
    episodes = sum(1 for name in staged.files if name.endswith("_fixture.json"))
    print(f"staged {staged.name}: {episodes} episodes in {staged.directory}")
    if fixture.private:
        print(f"private: after promotion, upload specs/{staged.name}/private/ to the "
              f"artifacts bucket (docs/SENSOR_RUNBOOK.md); git keeps only "
              f"specs/{staged.name}/private.json and the public files")
    print(f"digest: {staged.digest}")
    print(f"Review it: python -m sis.contract_author review {staged.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
