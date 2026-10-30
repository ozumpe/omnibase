"""scripts/draft_release_calendar.py — draft the release calendar a fixture is checked against.

    poetry run python scripts/draft_release_calendar.py urls --sources DIR
    poetry run python scripts/draft_release_calendar.py draft --sources DIR \
        --first 2018-12-01 --last 2026-10-28

A fixture built from backfilled history shows each reading no earlier than the
day it was actually released (``sis/fixture_builder.py``), and that day comes
from a calendar a human has checked. This drafts one for OMNI-163's fixture,
from three public sources (no key):

- **Stocks** (``PET.WGTSTP51.W``): EIA's Weekly Petroleum Status Report
  archive, whose links name each release and whose second column names the
  week it ended.
- **Retail prices** (the four ``EMM_EPMRR`` series): ALFRED's release dates for
  release 183, the Gasoline and Diesel Fuel Update. A Monday's price maps to
  the first release on or after it.
- **The LA spot price** (``PET.EER_EPMRR_PF4_Y05LA_DPG.D``): ALFRED's vintages
  of ``DGASUSGULF``, the Gulf Coast spot price from the same EIA spot release
  (ALFRED does not carry the LA series). A day maps to the first vintage that
  holds it. Only dates are kept: the values are Thomson Reuters' and are
  discarded as they are read.

**It reads saved copies, never the network.** ``urls`` prints what to save into
*DIR* and the file name for each (``curl -sSL -o DIR/<name> '<url>'``); run it
again once ``spot_vintages.html`` is there, for the spot CSVs. ``draft`` reads
them. The checker then reads the same files the dates were drafted from.

The draft has ``checked_by`` and ``checked_on`` empty, so the builder refuses
it until a person has checked the dates against the sources and filled both in.

Exit 1 if a source cannot be read or says something this script does not
expect (a week that does not end on a Friday, a period with no release), with
the reason.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import io
import json
import pathlib
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

DEFAULT_OUT = (pathlib.Path(__file__).resolve().parent / "data"
               / "eia_release_calendar_2018_2026.json")
WPSR_ARCHIVE = "https://www.eia.gov/petroleum/supply/weekly/archive/"
GDU_DATES = "https://alfred.stlouisfed.org/release/downloaddates?rid=183&ff=txt"
SPOT_VINTAGES = "https://alfred.stlouisfed.org/series/downloaddata?seid=DGASUSGULF"
SPOT_CSV = "https://alfred.stlouisfed.org/graph/alfredgraph.csv"
SPOT_PROXY = "DGASUSGULF"
_PER_REQUEST = 12       # ALFRED draws at most 12 lines in one graph, so 12 per CSV

STOCKS = ("PET.WGTSTP51.W",)
RETAIL = ("PET.EMM_EPMRR_PTE_SCA_DPG.W", "PET.EMM_EPMRR_PTE_Y05LA_DPG.W",
          "PET.EMM_EPMRR_PTE_Y05SF_DPG.W", "PET.EMM_EPMRR_PTE_NUS_DPG.W")
SPOT = ("PET.EER_EPMRR_PF4_Y05LA_DPG.D",)

_WPSR_ROW = re.compile(
    r'archive/(?P<year>\d{4})/(?P<dir>\d{4}_\d\d_\d\d)(?P<data>_data)?/[^"]*">'
    r"(?P<day>\d{1,2})</a></td>\s*<td>(?P<ending>\d{1,2}(?:/\d{1,2})?)</td>")
_ISO = re.compile(r"^\d{4}-\d\d-\d\d$")


class SourceRefused(ValueError):
    """A source said something this script does not expect."""


def _on_or_before(day: datetime.date, *, month: int | None, dom: int) -> datetime.date:
    """The latest date no later than *day* with day-of-month *dom* (and *month*)."""
    candidate = day
    for _ in range(400):
        if candidate.day == dom and (month is None or candidate.month == month):
            return candidate
        candidate -= datetime.timedelta(days=1)
    raise SourceRefused(f"no {month}/{dom} within a year before {day}")


def wpsr_releases(html: str) -> dict[datetime.date, datetime.date]:
    """{week ending (a Friday): release date}, from the WPSR archive page's HTML.

    A row links the release as ``archive/<year>/<YYYY_MM_DD>/…`` with the
    release day as its text, then names the week's last day (``D`` or ``M/D``).
    A delayed release is linked by the week instead (``<YYYY_MM_DD>_data``), so
    its release is the first date on or after that week with the linked day.
    """
    out: dict[datetime.date, datetime.date] = {}
    for row in _WPSR_ROW.finditer(html):
        named = datetime.date.fromisoformat(row["dir"].replace("_", "-"))
        day = int(row["day"])
        if row["data"]:
            released = named
            while released.day != day:
                released += datetime.timedelta(days=1)
                if released - named > datetime.timedelta(days=40):
                    raise SourceRefused(f"WPSR: no day {day} after {named}")
        else:
            released = named
            if named.day != day:
                raise SourceRefused(f"WPSR: {row['dir']} is linked as day {day}")
        month, _, dom = row["ending"].rpartition("/")
        ending = _on_or_before(released - datetime.timedelta(days=1),
                               month=int(month) if month else None, dom=int(dom))
        if ending.weekday() != 4:
            raise SourceRefused(f"WPSR: the week released {released} ends on {ending}, "
                                "not a Friday")
        if ending in out and out[ending] != released:
            raise SourceRefused(f"WPSR: the week ending {ending} is released twice")
        out[ending] = released
    if not out:
        raise SourceRefused("WPSR: no releases found in the archive page")
    return out


def alfred_release_dates(text: str) -> list[datetime.date]:
    """Every date under ``Release Dates:`` in ALFRED's text download."""
    _, found, rest = text.partition("Release Dates:")
    if not found:
        raise SourceRefused("ALFRED: no 'Release Dates:' section")
    dates = sorted({datetime.date.fromisoformat(line.strip())
                    for line in rest.splitlines() if _ISO.match(line.strip())})
    if not dates:
        raise SourceRefused("ALFRED: no release dates")
    return dates


def monday_releases(releases: Sequence[datetime.date], first: datetime.date,
                    last: datetime.date) -> dict[datetime.date, datetime.date]:
    """{Monday: the first release on or after it}, for every Monday in [first, last]."""
    out: dict[datetime.date, datetime.date] = {}
    monday = first + datetime.timedelta(days=(0 - first.weekday()) % 7)
    ordered = sorted(releases)
    while monday <= last:
        after = [day for day in ordered if day >= monday]
        if not after or after[0] - monday > datetime.timedelta(days=6):
            raise SourceRefused(f"GDU: no release within a week of Monday {monday}")
        out[monday] = after[0]
        monday += datetime.timedelta(weeks=1)
    return out


def vintage_dates(html: str) -> list[datetime.date]:
    """The vintage dates ALFRED's download page offers for a series."""
    found = sorted({datetime.date.fromisoformat(value) for value
                    in re.findall(r'<option value="(\d{4}-\d\d-\d\d)"', html)})
    if not found:
        raise SourceRefused("ALFRED: the download page lists no vintages")
    return found


def last_observations(text: str) -> dict[datetime.date, datetime.date]:
    """{vintage: the last day it holds a value for}, from an ALFRED graph CSV.

    One column per vintage (``<SERIES>_<YYYYMMDD>``); a day the vintage did not
    yet hold is empty. The values themselves are read only to test emptiness.
    """
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or rows[0][0] != "observation_date":
        raise SourceRefused("ALFRED: not a graph CSV")
    out: dict[datetime.date, datetime.date] = {}
    for column, name in enumerate(rows[0][1:], start=1):
        vintage = datetime.datetime.strptime(name.rpartition("_")[2], "%Y%m%d").date()
        held = [datetime.date.fromisoformat(row[0]) for row in rows[1:]
                if len(row) > column and row[column].strip() not in ("", ".")]
        if held:
            out[vintage] = max(held)
    return out


def daily_releases(last: Mapping[datetime.date, datetime.date], first: datetime.date,
                   until: datetime.date) -> dict[datetime.date, datetime.date]:
    """{weekday: the first vintage that holds it}, for weekdays in [first, until]."""
    out: dict[datetime.date, datetime.date] = {}
    held_through = datetime.date.min
    for vintage in sorted(last):
        through = last[vintage]
        day = max(first, held_through + datetime.timedelta(days=1))
        while day <= min(through, until):
            if day.weekday() < 5:
                out[day] = vintage
            day += datetime.timedelta(days=1)
        held_through = max(held_through, through)
    return out


def _windows(vintages: Sequence[datetime.date]) -> Iterable[Sequence[datetime.date]]:
    for start in range(0, len(vintages), _PER_REQUEST):
        yield vintages[start:start + _PER_REQUEST]


def _spot_csv_url(vintages: Sequence[datetime.date]) -> str:
    # A month before each vintage is enough to find the last day it holds.
    def joined(values: Iterable[str]) -> str:
        return ",".join(values)
    return (f"{SPOT_CSV}?id={joined(SPOT_PROXY for _ in vintages)}"
            f"&vintage_date={joined(v.isoformat() for v in vintages)}"
            f"&cosd={joined((v - datetime.timedelta(days=31)).isoformat() for v in vintages)}"
            f"&coed={joined(v.isoformat() for v in vintages)}")


def _iso(mapping: Mapping[datetime.date, datetime.date]) -> dict[str, str]:
    return {period.isoformat(): released.isoformat()
            for period, released in sorted(mapping.items())}


def _read(sources: pathlib.Path, name: str) -> str:
    path = sources / name
    if not path.exists():
        raise SourceRefused(f"{path} is missing: save it first (the `urls` command)")
    return path.read_text(encoding="utf-8", errors="replace")


def _vintages(sources: pathlib.Path, first: datetime.date,
              last: datetime.date) -> list[datetime.date]:
    margin = datetime.timedelta(days=14)
    return [v for v in vintage_dates(_read(sources, "spot_vintages.html"))
            if first - margin <= v <= last + margin]


def urls(sources: pathlib.Path, first: datetime.date,
         last: datetime.date) -> list[tuple[str, str]]:
    """(file name, url) for every source ``draft`` reads; spot CSVs once vintages are saved."""
    out = [("wpsr.html", WPSR_ARCHIVE), ("gdu.txt", GDU_DATES),
           ("spot_vintages.html", SPOT_VINTAGES)]
    if (sources / "spot_vintages.html").exists():
        out += [(f"spot_{index:03d}.csv", _spot_csv_url(window)) for index, window
                in enumerate(_windows(_vintages(sources, first, last)))]
    return out


def draft(sources: pathlib.Path, first: datetime.date, last: datetime.date) -> dict[str, Any]:
    """The unchecked calendar for [first, last], from the copies saved in *sources*."""
    stocks = {week: day for week, day in wpsr_releases(_read(sources, "wpsr.html")).items()
              if first <= week <= last}
    retail = monday_releases(alfred_release_dates(_read(sources, "gdu.txt")), first, last)
    held: dict[datetime.date, datetime.date] = {}
    for (name, _), window in zip(urls(sources, first, last)[3:],
                                 _windows(_vintages(sources, first, last)), strict=True):
        try:
            found = last_observations(_read(sources, name))
        except SourceRefused as exc:
            raise SourceRefused(f"{name}: {exc}") from None
        # A vintage missing from its CSV would silently move its days to a later one.
        if missing := sorted(set(window) - set(found)):
            raise SourceRefused(f"{name}: holds no day for vintage(s) "
                                f"{', '.join(v.isoformat() for v in missing)}")
        held.update(found)
    if not held:
        raise SourceRefused("no spot vintage holds any day: save the spot CSVs")
    spot = daily_releases(held, first, min(last, max(held.values())))
    return {
        "schema": 1,
        "timezone": "America/New_York",
        "checked_by": "",
        "checked_on": "",
        "description": (
            f"When each EIA period from {first} to {last} was actually released, for "
            "OMNI-163's fixture's release check (sis/fixture_builder.py). Keys are the "
            "period's date (Friday week-ending for stocks, Monday for retail prices, the "
            "day for the spot price); values the release date, both in America/New_York. "
            f"Drafted {datetime.date.today()} by scripts/draft_release_calendar.py; a "
            "human checks the dates against the sources and fills in checked_by and "
            "checked_on before any fixture is built."),
        "reports": {
            "wpsr": {"name": "Weekly Petroleum Status Report", "series": list(STOCKS),
                     "source": f"{WPSR_ARCHIVE} (each link names the release; the next "
                               "column names the week's last day)",
                     "releases": _iso(stocks)},
            "gdu": {"name": "Gasoline and Diesel Fuel Update", "series": list(RETAIL),
                    "source": f"{GDU_DATES} (each Monday's price maps to the first release "
                              "date on or after it)",
                    "note": "ALFRED shows a release on 2022-06-21 for Monday 2022-06-20; "
                            "CLAUDE.md says those prices came out on 2022-06-29. Check it.",
                    "releases": _iso(retail)},
            "spot": {"name": "Spot prices (weekly release)", "series": list(SPOT),
                     "source": f"{SPOT_VINTAGES} and {SPOT_CSV}: the vintages of "
                               f"{SPOT_PROXY}, the Gulf Coast price from the same EIA spot "
                               "release; a day maps to the first vintage holding it. Only "
                               "the dates were kept.",
                     "note": "ALFRED has no vintage on 2022-06-22 (EIA's June 2022 "
                             "outage); the days it would have held map to 2022-06-29.",
                     "releases": _iso(spot)},
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/draft_release_calendar.py",
                                     description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("urls", "draft"))
    parser.add_argument("--sources", type=pathlib.Path, required=True,
                        help="the directory the sources are saved in")
    parser.add_argument("--first", type=datetime.date.fromisoformat,
                        default=datetime.date(2018, 12, 1))
    parser.add_argument("--last", type=datetime.date.fromisoformat,
                        default=datetime.date.today())
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    if args.action == "urls":
        for name, url in urls(args.sources, args.first, args.last):
            print(f"{name}\t{url}")
        return 0
    try:
        calendar = draft(args.sources, args.first, args.last)
    except SourceRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(calendar, indent=1) + "\n", encoding="utf-8")
    counts = {name: len(body["releases"]) for name, body in calendar["reports"].items()}
    print(f"drafted {args.out}: {counts}. A person checks it and fills in checked_by "
          "and checked_on before a fixture is built from it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
