"""scripts/draft_release_calendar.py — draft, save and check the release calendar.

    poetry run python scripts/draft_release_calendar.py urls --sources DIR
    poetry run python scripts/draft_release_calendar.py save --sources DIR \
        --first 2018-12-01 --last 2026-10-28
    poetry run python scripts/draft_release_calendar.py check
    poetry run python scripts/draft_release_calendar.py sign

A fixture built from backfilled history shows each reading no earlier than the
day it was actually released (``sis/fixture_builder.py``), and that day comes
from a checked calendar. This drafts one for OMNI-163's fixture, from public
sources (no key):

- **Stocks** (``PET.WGTSTP51.W``): EIA's Weekly Petroleum Status Report
  archive, whose links name each release and whose second column names the
  week it ended.
- **Retail prices** (the four ``EMM_EPMRR`` series), from two sources: ALFRED's
  release dates for release 183, the Gasoline and Diesel Fuel Update (a
  Monday's price maps to the first release on or after it), and ALFRED's
  vintages of ``GASREGW``, EIA's US regular retail price from the same release
  (a Monday maps to the first vintage that holds it).
- **The LA spot price** (``PET.EER_EPMRR_PF4_Y05LA_DPG.D``): ALFRED's vintages
  of ``DGASUSGULF``, the Gulf Coast spot price from the same EIA spot release
  (ALFRED does not carry the LA series). A day maps to the first vintage that
  holds it.

**Where two sources disagree, the later date wins** (:func:`later_wins`). A
date that is too late only withholds a value a little longer; one that is too
early shows a model the future.

**It reads saved copies, never the network.** ``urls`` prints what to save into
*DIR* and the file name for each (``curl -sSL -o DIR/<name> '<url>'``); run it
again once the ``*_vintages.html`` files are there, for the CSVs. ``draft``
reads them and writes an unchecked calendar.

**The check is CI's, not a person's** (OMNI-190). ``save`` reduces the raw
copies in *DIR* to what the parsers read and writes them, each pinned by
sha256 in ``sources.json``, to ``scripts/data/release_calendar_sources/``,
which is committed. The reduction keeps dates only: every value in a CSV
becomes ``x``, because the spot prices are Thomson Reuters' data under licence
to EIA and may never appear in a public place. ``save`` refuses a reduction
that would draft a different calendar from the raw copies. ``check`` verifies
every copy against its hash, re-drafts the calendar from them, and fails on
any difference from the committed one (a date earlier than the sources is
named as such). It runs in CI (``tests/test_draft_release_calendar.py``) and
in ``scripts/build_fixture.py``. ``sign`` runs the same check and fills in
``checked_by`` as ``CI, sources sha256:<sha256 of sources.json>`` with
``checked_on``. A claim of that form is only accepted where the check passes,
so it cannot be typed in; CI also attests the calendar with a Sigstore
signature on every push to ``develop`` or ``main``
(``.github/workflows/release-calendar.yml``).

Exit 1 if a source cannot be read or says something this script does not
expect (a week that does not end on a Friday, a period with no release), or if
the check fails, with the reason.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
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
RETAIL_VINTAGES = "https://alfred.stlouisfed.org/series/downloaddata?seid=GASREGW"
RETAIL_PROXY = "GASREGW"
SOURCES = pathlib.Path(__file__).resolve().parent / "data" / "release_calendar_sources"
MANIFEST = "sources.json"
CLAIM = "CI, sources sha256:"
_PER_REQUEST = 12       # ALFRED draws at most 12 lines in one graph, so 12 per CSV

STOCKS = ("PET.WGTSTP51.W",)
RETAIL = ("PET.EMM_EPMRR_PTE_SCA_DPG.W", "PET.EMM_EPMRR_PTE_Y05LA_DPG.W",
          "PET.EMM_EPMRR_PTE_Y05SF_DPG.W", "PET.EMM_EPMRR_PTE_NUS_DPG.W")
SPOT = ("PET.EER_EPMRR_PF4_Y05LA_DPG.D",)

_WPSR_ROW = re.compile(
    r'archive/(?P<year>\d{4})/(?P<dir>\d{4}_\d\d_\d\d)(?P<data>_data)?/[^"]*">'
    r"(?P<day>\d{1,2})</a></td>\s*<td>(?P<ending>\d{1,2}(?:/\d{1,2})?)</td>")
_ISO = re.compile(r"^\d{4}-\d\d-\d\d$")

# Mondays whose price came out later than either retail source says (OMNI-163).
# Each date is the latest with support, since a late date only hides a value
# and an early one shows a model a price before it was published. Applied only
# to move a release later, never earlier. (2026-09-14 was here until OMNI-190:
# GASREGW's vintages now give its 2026-09-22 themselves.)
RETAIL_CORRECTIONS: dict[datetime.date, tuple[datetime.date, str]] = {
    datetime.date(2022, 6, 20): (
        datetime.date(2022, 6, 29),
        "ALFRED lists a release on 2022-06-21 and GASREGW first holds 2022-06-20 in "
        "its 2022-06-27 vintage, but EIA's June 2022 outage held those prices until "
        "2022-06-29 (CLAUDE.md): the later date is kept."),
}


def later_wins(*sources: Mapping[datetime.date, datetime.date]
               ) -> dict[datetime.date, datetime.date]:
    """Every period any source dates, at the latest date any source gives it.

    A release dated too late withholds a value a little longer; one dated too
    early hands a backtest the future. So a disagreement is settled by the later.
    """
    out: dict[datetime.date, datetime.date] = {}
    for source in sources:
        for period, released in source.items():
            if period not in out or released > out[period]:
                out[period] = released
    return out


def corrected(retail: Mapping[datetime.date, datetime.date]) -> dict[datetime.date, datetime.date]:
    """*retail* with :data:`RETAIL_CORRECTIONS` applied where they move a release later."""
    return later_wins(retail, {monday: released for monday, (released, _)
                               in RETAIL_CORRECTIONS.items() if monday in retail})


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


# The two ALFRED series read by vintage: (file prefix, series, vintages page).
_GRAPHED = (("spot", SPOT_PROXY, SPOT_VINTAGES), ("retail", RETAIL_PROXY, RETAIL_VINTAGES))


def _graph_csv_url(series: str, vintages: Sequence[datetime.date]) -> str:
    # A month before each vintage is enough to find the last day it holds.
    def joined(values: Iterable[str]) -> str:
        return ",".join(values)
    return (f"{SPOT_CSV}?id={joined(series for _ in vintages)}"
            f"&vintage_date={joined(v.isoformat() for v in vintages)}"
            f"&cosd={joined((v - datetime.timedelta(days=31)).isoformat() for v in vintages)}"
            f"&coed={joined(v.isoformat() for v in vintages)}")


def _spot_csv_url(vintages: Sequence[datetime.date]) -> str:
    return _graph_csv_url(SPOT_PROXY, vintages)


def _iso(mapping: Mapping[datetime.date, datetime.date]) -> dict[str, str]:
    return {period.isoformat(): released.isoformat()
            for period, released in sorted(mapping.items())}


def _read(sources: pathlib.Path, name: str) -> str:
    path = sources / name
    if not path.exists():
        raise SourceRefused(f"{path} is missing: save it first (the `urls` command)")
    return path.read_text(encoding="utf-8", errors="replace")


def _vintages(sources: pathlib.Path, first: datetime.date,
              last: datetime.date, prefix: str = "spot") -> list[datetime.date]:
    margin = datetime.timedelta(days=14)
    return [v for v in vintage_dates(_read(sources, f"{prefix}_vintages.html"))
            if first - margin <= v <= last + margin]


def _csvs(sources: pathlib.Path, first: datetime.date, last: datetime.date,
          prefix: str, series: str) -> list[tuple[str, str, Sequence[datetime.date]]]:
    """(file name, url, vintages) for each graph CSV of *series*."""
    return [(f"{prefix}_{index:03d}.csv", _graph_csv_url(series, window), window)
            for index, window in enumerate(_windows(_vintages(sources, first, last, prefix)))]


def urls(sources: pathlib.Path, first: datetime.date,
         last: datetime.date) -> list[tuple[str, str]]:
    """(file name, url) for every source ``draft`` reads; the CSVs once vintages are saved."""
    out = [("wpsr.html", WPSR_ARCHIVE), ("gdu.txt", GDU_DATES)]
    out += [(f"{prefix}_vintages.html", page) for prefix, _, page in _GRAPHED]
    for prefix, series, _ in _GRAPHED:
        if (sources / f"{prefix}_vintages.html").exists():
            out += [(name, url) for name, url, _ in
                    _csvs(sources, first, last, prefix, series)]
    return out


def _held(sources: pathlib.Path, first: datetime.date, last: datetime.date,
          prefix: str, series: str) -> dict[datetime.date, datetime.date]:
    """{vintage: the last day it holds}, from every saved CSV of *series*."""
    held: dict[datetime.date, datetime.date] = {}
    for name, _, window in _csvs(sources, first, last, prefix, series):
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
        raise SourceRefused(f"no {series} vintage holds any day: save its CSVs")
    return held


def draft(sources: pathlib.Path, first: datetime.date, last: datetime.date) -> dict[str, Any]:
    """The unchecked calendar for [first, last], from the copies saved in *sources*."""
    stocks = {week: day for week, day in wpsr_releases(_read(sources, "wpsr.html")).items()
              if first <= week <= last}
    alfred = monday_releases(alfred_release_dates(_read(sources, "gdu.txt")), first, last)
    vintaged = daily_releases(_held(sources, first, last, "retail", RETAIL_PROXY), first, last)
    if unheld := sorted(monday for monday in alfred if monday not in vintaged):
        raise SourceRefused(f"{RETAIL_PROXY}: no vintage holds Monday(s) "
                            f"{', '.join(m.isoformat() for m in unheld)}: save later "
                            "vintages, or draft to an earlier --last")
    retail = corrected(later_wins(alfred, {monday: vintaged[monday] for monday in alfred}))
    held = _held(sources, first, last, "spot", SPOT_PROXY)
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
            "Drafted by scripts/draft_release_calendar.py from the saved sources in "
            "scripts/data/release_calendar_sources/; its `check` re-drafts it from them "
            "and `sign` fills in checked_by and checked_on (OMNI-190)."),
        "reports": {
            "wpsr": {"name": "Weekly Petroleum Status Report", "series": list(STOCKS),
                     "source": f"{WPSR_ARCHIVE} (each link names the release; the next "
                               "column names the week's last day)",
                     "releases": _iso(stocks)},
            "gdu": {"name": "Gasoline and Diesel Fuel Update", "series": list(RETAIL),
                    "source": f"{GDU_DATES} (each Monday's price maps to the first release "
                              f"date on or after it) and {RETAIL_VINTAGES} (the first "
                              f"vintage of {RETAIL_PROXY} holding the Monday); where they "
                              "differ, the later date",
                    "note": " ".join(
                        f"{monday}: released {released}, later than both sources. {why}"
                        for monday, (released, why) in sorted(RETAIL_CORRECTIONS.items())
                        if first <= monday <= last),
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


# --- Saved copies and the CI check (OMNI-190) --------------------------------------

class CalendarRefused(ValueError):
    """The calendar does not pass its check against the saved sources."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def reduce(name: str, text: str) -> str:
    """*text*, the raw copy of source *name*, cut to what its parser reads.

    Dates only: every value a graph CSV holds becomes ``x`` (the parser asks
    only whether a cell is empty), because the spot prices are licensed and may
    never be published. The WPSR archive keeps its release rows, a vintages
    page its ``<option>`` dates; ALFRED's release-date list is kept whole.
    """
    if name == "wpsr.html":
        return "".join(row[0] + "\n" for row in _WPSR_ROW.finditer(text))
    if name.endswith("_vintages.html"):
        return "".join(f'<option value="{day}">\n' for day in vintage_dates(text))
    if name.endswith(".csv"):
        rows = list(csv.reader(io.StringIO(text)))
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        for number, row in enumerate(rows):
            writer.writerow(row if number == 0 else
                            [row[0], *(cell if cell.strip() in ("", ".") else "x"
                                       for cell in row[1:])])
        return out.getvalue()
    if name == "gdu.txt":
        return text
    raise SourceRefused(f"{name}: not a source this script reads")


def save(raw: pathlib.Path, out: pathlib.Path, first: datetime.date, last: datetime.date,
         *, fetched_on: str) -> str:
    """Reduce the raw copies in *raw* into *out* with their manifest; its sha256.

    Refused if the reduced copies would draft a different calendar than the
    raw ones: the reduction may drop what no parser reads, nothing else.
    """
    wanted = urls(raw, first, last)
    from_raw = draft(raw, first, last)
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.iterdir():
        stale.unlink()
    files: dict[str, dict[str, str]] = {}
    for name, url in wanted:
        text = _read(raw, name)
        reduced = reduce(name, text).encode("utf-8")
        (out / name).write_bytes(reduced)
        files[name] = {"sha256": _sha256(reduced), "url": url,
                       "raw_sha256": _sha256((raw / name).read_bytes())}
    if (from_reduced := draft(out, first, last))["reports"] != from_raw["reports"]:
        raise SourceRefused("the reduced copies draft a different calendar than the raw "
                            "ones: " + "; ".join(differences(from_raw, from_reduced)[:5]))
    manifest = {"schema": 1, "first": first.isoformat(), "last": last.isoformat(),
                "fetched_on": fetched_on,
                "note": ("Saved copies of the sources the release calendar is drafted from, "
                         "reduced to the dates their parsers read (every value in a CSV is "
                         "x: the spot prices are licensed). raw_sha256 is the copy as "
                         "fetched, which is kept out of git. See "
                         "scripts/draft_release_calendar.py (OMNI-190)."),
                "files": files}
    data = (json.dumps(manifest, indent=1) + "\n").encode("utf-8")
    (out / MANIFEST).write_bytes(data)
    return _sha256(data)


def verified_sources(sources: pathlib.Path) -> tuple[str, datetime.date, datetime.date]:
    """(sha256 of the manifest, first, last), once every copy matches its hash.

    A copy that changed, is missing, or is not in the manifest refuses the
    check: the calendar is never re-drafted from anything but what was pinned.
    """
    path = sources / MANIFEST
    if not path.is_file():
        raise CalendarRefused(f"no {MANIFEST} in {sources}: save the sources first")
    data = path.read_bytes()
    try:
        manifest = json.loads(data)
        files = {str(name): str(entry["sha256"])
                 for name, entry in dict(manifest["files"]).items()}
        first = datetime.date.fromisoformat(str(manifest["first"]))
        last = datetime.date.fromisoformat(str(manifest["last"]))
    except (ValueError, KeyError, TypeError):
        raise CalendarRefused(f"{path} is not a sources manifest") from None
    problems = []
    for name, expected in sorted(files.items()):
        copy = sources / name
        if not copy.is_file():
            problems.append(f"{name} is missing")
        elif _sha256(copy.read_bytes()) != expected:
            problems.append(f"{name} does not match its sha256 in {MANIFEST}")
    problems += [f"{p.name} is not in {MANIFEST}" for p in sorted(sources.iterdir())
                 if p.name != MANIFEST and p.name not in files]
    if problems:
        raise CalendarRefused("the saved sources changed: " + "; ".join(problems))
    return _sha256(data), first, last


def differences(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    """Every way *actual* differs from the calendar the sources give, *expected*.

    ``checked_by``, ``checked_on`` and the prose ``description`` are not
    compared. A release earlier than the sources is named as the danger it is.
    """
    out = [f"{key}: {actual.get(key)!r}, the sources give {expected.get(key)!r}"
           for key in ("schema", "timezone") if actual.get(key) != expected.get(key)]
    want, have = dict(expected.get("reports", {})), dict(actual.get("reports", {}))
    for report in sorted(set(want) | set(have)):
        if report not in have:
            out.append(f"{report}: missing")
            continue
        if report not in want:
            out.append(f"{report}: not a report the sources give")
            continue
        w, h = dict(want[report]), dict(have[report])
        out += [f"{report}.{key} differs from the draft" for key in sorted(set(w) | set(h))
                if key != "releases" and w.get(key) != h.get(key)]
        w_rel, h_rel = dict(w.get("releases", {})), dict(h.get("releases", {}))
        for period in sorted(set(w_rel) | set(h_rel)):
            source, committed = w_rel.get(period), h_rel.get(period)
            if source == committed:
                continue
            if committed is None:
                out.append(f"{report} {period}: missing (the sources say {source})")
            elif source is None:
                out.append(f"{report} {period}: {committed}, a period the sources do not date")
            elif str(committed) < str(source):
                out.append(f"{report} {period}: {committed} is EARLIER than the sources' "
                           f"{source}, which would show a backtest the future")
            else:
                out.append(f"{report} {period}: {committed}, later than the sources' {source}")
    return out


def claim(digest: str) -> str:
    """``checked_by`` for a calendar that passed the check against sources *digest*."""
    return f"{CLAIM}{digest}"


def _load(calendar: pathlib.Path) -> dict[str, Any]:
    try:
        data = json.loads(calendar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CalendarRefused(f"{calendar} cannot be read as JSON: {exc}") from None
    if not isinstance(data, dict):
        raise CalendarRefused(f"{calendar} is not a calendar")
    return data


def check(calendar: pathlib.Path, sources: pathlib.Path = SOURCES, *,
          signed: bool = True) -> str:
    """The sources' digest, once *calendar* is what they draft (and, if *signed*,
    names them in ``checked_by`` with a ``checked_on`` date). Raises
    :class:`CalendarRefused` with every difference otherwise. No network."""
    digest, first, last = verified_sources(sources)
    data = _load(calendar)
    try:
        expected = draft(sources, first, last)
    except SourceRefused as exc:
        raise CalendarRefused(f"the saved sources do not draft: {exc}") from None
    problems = differences(expected, data)
    if signed:
        if data.get("checked_by") != claim(digest):
            problems.append(f"checked_by is {data.get('checked_by')!r}, not "
                            f"{claim(digest)!r} (run `sign`)")
        try:
            datetime.date.fromisoformat(str(data.get("checked_on")))
        except ValueError:
            problems.append(f"checked_on is {data.get('checked_on')!r}, not a date")
    if problems:
        shown = problems[:20] + ([f"... and {len(problems) - 20} more"]
                                 if len(problems) > 20 else [])
        raise CalendarRefused(f"{calendar} does not pass its check against "
                              f"{sources}:\n  " + "\n  ".join(shown))
    return digest


def is_ci_claim(checked_by: str) -> bool:
    """Whether *checked_by* claims the CI check rather than a person's."""
    return checked_by.startswith(CLAIM)


def sign(calendar: pathlib.Path, sources: pathlib.Path = SOURCES, *,
         on: datetime.date) -> str:
    """Check *calendar*, then fill in ``checked_by`` and ``checked_on``; the claim."""
    digest = check(calendar, sources, signed=False)
    data = _load(calendar)
    data["checked_by"], data["checked_on"] = claim(digest), on.isoformat()
    calendar.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    return claim(digest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/draft_release_calendar.py",
                                     description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("urls", "draft", "save", "check", "sign"))
    parser.add_argument("--sources", type=pathlib.Path, default=None,
                        help="urls/draft/save: the directory the raw copies are saved in; "
                             "check/sign: the reduced copies (default: "
                             "scripts/data/release_calendar_sources)")
    parser.add_argument("--first", type=datetime.date.fromisoformat,
                        default=datetime.date(2018, 12, 1))
    parser.add_argument("--last", type=datetime.date.fromisoformat,
                        default=datetime.date.today())
    parser.add_argument("--out", type=pathlib.Path, default=None,
                        help="draft: the calendar to write; save: the directory for the "
                             "reduced copies (defaults: the committed ones)")
    parser.add_argument("--calendar", type=pathlib.Path, default=DEFAULT_OUT,
                        help="check/sign: the calendar to check")
    parser.add_argument("--fetched-on", default=datetime.date.today().isoformat(),
                        help="save: the day the raw copies were fetched")
    args = parser.parse_args(argv)
    if args.action in ("check", "sign"):
        sources = args.sources or SOURCES
        try:
            if args.action == "check":
                digest = check(args.calendar, sources)
                print(f"{args.calendar} passes its check against the sources "
                      f"(sha256:{digest})")
            else:
                signed = sign(args.calendar, sources,
                              on=datetime.datetime.now(datetime.UTC).date())
                print(f"signed {args.calendar}: checked_by {signed!r}")
        except CalendarRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.sources is None:
        parser.error(f"{args.action} needs --sources DIR (the raw copies)")
    if args.action == "urls":
        for name, url in urls(args.sources, args.first, args.last):
            print(f"{name}\t{url}")
        return 0
    try:
        if args.action == "save":
            out = args.out or SOURCES
            digest = save(args.sources, out, args.first, args.last,
                          fetched_on=args.fetched_on)
            print(f"saved the reduced sources to {out} (sha256:{digest}). Draft the "
                  f"calendar from them (draft --sources {out}), then run `sign`.")
            return 0
        calendar = draft(args.sources, args.first, args.last)
    except SourceRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    out_path = args.out or DEFAULT_OUT
    out_path.write_text(json.dumps(calendar, indent=1) + "\n", encoding="utf-8")
    counts = {name: len(body["releases"]) for name, body in calendar["reports"].items()}
    print(f"drafted {out_path}: {counts}. Unchecked: run `check`, then `sign`, before a "
          "fixture is built from it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
