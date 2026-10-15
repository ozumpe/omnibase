"""scripts/build_fixture.py — build OMNI-35's fixture from the EIA trace, and stage it for review.

    poetry run python scripts/build_fixture.py
    poetry run python scripts/build_fixture.py --trace PATH --calendar PATH

Reads the trace through the sanitising door, checks every reconstructed
``known_at`` a fixture shows against the release calendar (which a human must
have checked: ``checked_by``, ``checked_on``), and builds one held-out episode
per week from 8 August to 28 November 2022: given everything known when
California's price for a week became known, its price the next week. The draft
is staged under ``runtime/contract_staging/ca_price_next_week/``.

Nothing reaches ``specs/`` from here. A human reads the staged files, then runs
``python -m sis.contract_author promote ca_price_next_week --digest <digest>
--reason "..."`` with the digest printed below (OMNI-68).

Exit 1 if the fixture cannot be built honestly (an unchecked calendar, a
reading it does not cover, a trace that does not read back), with the reason.
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys

# Allow running as a plain script: put the repo root (parent of scripts/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sis import sensor_trace  # noqa: E402
from sis.adapters_real import EIA_RULES, EIA_TRACE  # noqa: E402
from sis.contract_author import stage  # noqa: E402
from sis.fixture_builder import FixtureRefused, Question, ReleaseCalendar, build  # noqa: E402

CALENDAR = pathlib.Path(__file__).resolve().parent / "data" / "eia_release_calendar_2021_2022.json"
NAME = "ca_price_next_week"
QUESTION = Question(target="PET.EMM_EPMRR_PTE_SCA_DPG.W",
                    first=datetime.date(2022, 8, 8), last=datetime.date(2022, 11, 28))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/build_fixture.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trace", type=pathlib.Path, default=EIA_TRACE)
    parser.add_argument("--calendar", type=pathlib.Path, default=CALENDAR)
    args = parser.parse_args(argv)
    try:
        lines = sensor_trace.load(args.trace)
        if not lines:
            raise FixtureRefused(f"no trace at {args.trace}: capture one first "
                                 "(scripts/capture_sensor.py)")
        calendar = ReleaseCalendar.parse(args.calendar.read_text(encoding="utf-8"))
        draft = build(lines, EIA_RULES, calendar, QUESTION, name=NAME, spec_ref="OMNI-35")
    except (FixtureRefused, sensor_trace.TraceRefused) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    staged = stage(draft)
    episodes = sum(1 for name in staged.files if name.endswith("_fixture.json"))
    print(f"staged {staged.name}: {episodes} episodes in {staged.directory}")
    print(f"digest: {staged.digest}")
    print(f"Review it: python -m sis.contract_author review {staged.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
