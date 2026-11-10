"""The release calendar (OMNI-163, OMNI-190): its draft, and the check that signs it.

``scripts/draft_release_calendar.py`` drafts the dates every private fixture
is built against, so a misreading here would show a backtest the future. Each
parser is pinned on a few lines shaped like its source. Since OMNI-190 the
check is CI's: the committed calendar must be exactly what the committed,
sha256-pinned source copies draft (``test_the_committed_calendar_passes_its_check``).
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import pathlib
import sys
from typing import Any

import pytest

D = datetime.date


def _script() -> Any:
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / \
        "draft_release_calendar.py"
    spec = importlib.util.spec_from_file_location("draft_release_calendar", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


calendar = _script()


def _row(year: int, directory: str, day: int, ending: str) -> str:
    return (f'<td><a href="/petroleum/supply/weekly/archive/{year}/{directory}/'
            f'wpsr_{directory}.php">{day}</a></td>\n\t\t\t\t\t\t<td>{ending}</td>')


def test_the_wpsr_archive_gives_each_week_ending_its_release() -> None:
    html = "\n".join([
        _row(2022, "2022_06_02", 2, "5/27"),          # the week ended in the month before
        _row(2022, "2022_06_15", 15, "10"),
        _row(2022, "2022_06_17_data", 29, "17"),      # the delayed week, linked by its end
        _row(2022, "2022_06_29", 29, "24"),
        _row(2023, "2023_01_05", 5, "12/30"),         # across a year
    ])
    assert calendar.wpsr_releases(html) == {
        D(2022, 5, 27): D(2022, 6, 2), D(2022, 6, 10): D(2022, 6, 15),
        D(2022, 6, 17): D(2022, 6, 29), D(2022, 6, 24): D(2022, 6, 29),
        D(2022, 12, 30): D(2023, 1, 5)}


def test_a_week_that_does_not_end_on_a_friday_is_refused() -> None:
    with pytest.raises(calendar.SourceRefused, match="not a Friday"):
        calendar.wpsr_releases(_row(2022, "2022_06_15", 15, "11"))
    with pytest.raises(calendar.SourceRefused, match="no releases"):
        calendar.wpsr_releases("<html></html>")


def test_a_mondays_retail_price_is_released_on_the_first_release_on_or_after_it() -> None:
    text = ("Release: Gasoline and Diesel Fuel Update\n\nRelease Dates:\n"
            "2022-09-06\n2022-09-12\n2022-09-19\n")
    dates = calendar.alfred_release_dates(text)
    assert calendar.monday_releases(dates, D(2022, 9, 5), D(2022, 9, 19)) == {
        D(2022, 9, 5): D(2022, 9, 6), D(2022, 9, 12): D(2022, 9, 12),
        D(2022, 9, 19): D(2022, 9, 19)}
    with pytest.raises(calendar.SourceRefused, match="2022-09-26"):
        calendar.monday_releases(dates, D(2022, 9, 5), D(2022, 9, 26))


def test_a_spot_day_is_released_with_the_first_vintage_that_holds_it() -> None:
    csv = ("observation_date,DGASUSGULF_20220921,DGASUSGULF_20220928,DGASUSGULF_20221005\n"
           "2022-09-16,1.0,1.0,1.0\n2022-09-19,1.0,1.0,1.0\n2022-09-20,,1.0,1.0\n"
           "2022-09-26,,1.0,1.0\n2022-09-27,,,1.0\n2022-09-30,,,1.0\n")
    held = calendar.last_observations(csv)
    assert held == {D(2022, 9, 21): D(2022, 9, 19), D(2022, 9, 28): D(2022, 9, 26),
                    D(2022, 10, 5): D(2022, 9, 30)}
    released = calendar.daily_releases(held, D(2022, 9, 19), D(2022, 9, 30))
    assert released[D(2022, 9, 19)] == D(2022, 9, 21)
    assert released[D(2022, 9, 23)] == D(2022, 9, 28)      # a gap in the CSV is a weekday
    assert released[D(2022, 9, 26)] == D(2022, 9, 28)
    assert released[D(2022, 9, 27)] == D(2022, 10, 5)
    assert D(2022, 9, 24) not in released                   # Saturday


def _retail(tmp_path: pathlib.Path, vintage: str = "2022-01-10",
            mondays: tuple[str, ...] = ("2022-01-10",)) -> None:
    """A GASREGW vintages page and CSV whose one vintage holds *mondays*."""
    (tmp_path / "retail_vintages.html").write_text(f'<option value="{vintage}">',
                                                   encoding="utf-8")
    (tmp_path / "retail_000.csv").write_text(
        f"observation_date,GASREGW_{vintage.replace('-', '')}\n"
        + "".join(f"{monday},3.1\n" for monday in mondays), encoding="utf-8")


def test_a_saved_csv_missing_a_vintage_it_was_asked_for_is_refused(
        tmp_path: pathlib.Path) -> None:
    # ALFRED draws at most 12 lines in a graph and silently drops the rest.
    vintages = [D(2022, 1, 5) + datetime.timedelta(weeks=n) for n in range(3)]
    (tmp_path / "wpsr.html").write_text(_row(2022, "2022_01_12", 12, "7"), encoding="utf-8")
    (tmp_path / "gdu.txt").write_text("Release Dates:\n2022-01-10\n", encoding="utf-8")
    (tmp_path / "spot_vintages.html").write_text(
        "".join(f'<option value="{v}">{v}' for v in vintages), encoding="utf-8")
    names = [name for name, _ in calendar.urls(tmp_path, D(2022, 1, 10), D(2022, 1, 10))]
    assert names == ["wpsr.html", "gdu.txt", "spot_vintages.html", "retail_vintages.html",
                     "spot_000.csv"]
    _retail(tmp_path)
    (tmp_path / "spot_000.csv").write_text(
        "observation_date,DGASUSGULF_20220105,DGASUSGULF_20220112\n2022-01-10,,1.0\n",
        encoding="utf-8")
    with pytest.raises(calendar.SourceRefused, match="2022-01-05, 2022-01-19"):
        calendar.draft(tmp_path, D(2022, 1, 10), D(2022, 1, 10))


def _sources(tmp_path: pathlib.Path) -> pathlib.Path:
    """Raw copies of every source for 2022-01-07 to 2022-01-10."""
    (tmp_path / "wpsr.html").write_text(
        "<html><body>" + _row(2022, "2022_01_12", 12, "7") + "</body></html>",
        encoding="utf-8")
    (tmp_path / "gdu.txt").write_text("Release: GDU\n\nRelease Dates:\n2022-01-10\n",
                                      encoding="utf-8")
    (tmp_path / "spot_vintages.html").write_text(
        '<select><option value="2022-01-12">2022-01-12</option></select>', encoding="utf-8")
    (tmp_path / "spot_000.csv").write_text(
        "observation_date,DGASUSGULF_20220112\n2022-01-07,2.345\n2022-01-10,2.401\n",
        encoding="utf-8")
    _retail(tmp_path)
    return tmp_path


def test_the_draft_is_unsigned(tmp_path: pathlib.Path) -> None:
    drafted = calendar.draft(_sources(tmp_path), D(2022, 1, 7), D(2022, 1, 10))
    assert drafted["checked_by"] == "" and drafted["checked_on"] == ""
    assert drafted["reports"]["wpsr"]["releases"] == {"2022-01-07": "2022-01-12"}
    assert drafted["reports"]["gdu"]["releases"] == {"2022-01-10": "2022-01-10"}
    assert drafted["reports"]["spot"]["releases"] == {"2022-01-07": "2022-01-12",
                                                      "2022-01-10": "2022-01-12"}


def test_where_the_retail_sources_disagree_the_later_date_wins(
        tmp_path: pathlib.Path) -> None:
    sources = _sources(tmp_path)
    _retail(sources, vintage="2022-01-11")      # GASREGW first holds it a day later
    drafted = calendar.draft(sources, D(2022, 1, 7), D(2022, 1, 10))
    assert drafted["reports"]["gdu"]["releases"] == {"2022-01-10": "2022-01-11"}
    # A Monday the second source does not hold is refused, not left to the first.
    _retail(sources, mondays=("2022-01-03",))
    with pytest.raises(calendar.SourceRefused, match="no vintage holds Monday"):
        calendar.draft(sources, D(2022, 1, 7), D(2022, 1, 10))


def test_later_wins_takes_every_period_at_its_latest_date() -> None:
    assert calendar.later_wins(
        {D(2022, 1, 3): D(2022, 1, 3), D(2022, 1, 10): D(2022, 1, 11)},
        {D(2022, 1, 3): D(2022, 1, 4), D(2022, 1, 10): D(2022, 1, 10)},
        {D(2022, 1, 17): D(2022, 1, 18)}) == {
        D(2022, 1, 3): D(2022, 1, 4), D(2022, 1, 10): D(2022, 1, 11),
        D(2022, 1, 17): D(2022, 1, 18)}
    assert calendar.later_wins() == {}


# --- the CI check (OMNI-190) ------------------------------------------------------------

def _saved(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """(reduced sources, signed calendar) for the small sources above."""
    (tmp_path / "raw").mkdir(parents=True)
    raw, saved = _sources(tmp_path / "raw"), tmp_path / "saved"
    calendar.save(raw, saved, D(2022, 1, 7), D(2022, 1, 10), fetched_on="2026-11-07")
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(calendar.draft(saved, D(2022, 1, 7), D(2022, 1, 10))),
                    encoding="utf-8")
    calendar.sign(path, saved, on=D(2026, 11, 7))
    return saved, path


def test_the_saved_copies_keep_dates_and_no_values(tmp_path: pathlib.Path) -> None:
    saved, _ = _saved(tmp_path)
    assert "2.345" not in (saved / "spot_000.csv").read_text(encoding="utf-8")
    assert (saved / "spot_000.csv").read_text(encoding="utf-8") == (
        "observation_date,DGASUSGULF_20220112\n2022-01-07,x\n2022-01-10,x\n")
    assert "<html>" not in (saved / "wpsr.html").read_text(encoding="utf-8")
    manifest = json.loads((saved / calendar.MANIFEST).read_text(encoding="utf-8"))
    assert sorted(manifest["files"]) == sorted(
        p.name for p in saved.iterdir() if p.name != calendar.MANIFEST)
    assert manifest["first"] == "2022-01-07" and manifest["last"] == "2022-01-10"


def test_a_signed_calendar_passes_and_names_its_sources(tmp_path: pathlib.Path) -> None:
    saved, path = _saved(tmp_path)
    digest = calendar.check(path, saved)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["checked_by"] == f"CI, sources sha256:{digest}"
    assert data["checked_on"] == "2026-11-07"
    assert calendar.is_ci_claim(data["checked_by"]) and not calendar.is_ci_claim("Olaf")
    assert calendar.main(["check", "--calendar", str(path), "--sources", str(saved)]) == 0


def test_a_date_moved_earlier_than_the_sources_fails(tmp_path: pathlib.Path) -> None:
    saved, path = _saved(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["reports"]["spot"]["releases"]["2022-01-10"] = "2022-01-11"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(calendar.CalendarRefused,
                       match="spot 2022-01-10: 2022-01-11 is EARLIER than the sources' "
                             "2022-01-12"):
        calendar.check(path, saved)
    # Any difference fails, a later date and a missing period too.
    data["reports"]["spot"]["releases"]["2022-01-10"] = "2022-01-13"
    del data["reports"]["wpsr"]["releases"]["2022-01-07"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(calendar.CalendarRefused) as refused:
        calendar.check(path, saved)
    assert "later than the sources' 2022-01-12" in str(refused.value)
    assert "wpsr 2022-01-07: missing" in str(refused.value)


def test_a_changed_source_copy_fails_rather_than_re_drafting(tmp_path: pathlib.Path) -> None:
    saved, path = _saved(tmp_path)
    (saved / "gdu.txt").write_text("Release Dates:\n2022-01-09\n", encoding="utf-8")
    with pytest.raises(calendar.CalendarRefused, match="gdu.txt does not match its sha256"):
        calendar.check(path, saved)
    saved, path = _saved(tmp_path / "again")
    (saved / "extra.csv").write_text("observation_date\n", encoding="utf-8")
    with pytest.raises(calendar.CalendarRefused, match="extra.csv is not in sources.json"):
        calendar.check(path, saved)
    (saved / "extra.csv").unlink()
    (saved / "spot_000.csv").unlink()
    with pytest.raises(calendar.CalendarRefused, match="spot_000.csv is missing"):
        calendar.check(path, saved)


def test_an_unsigned_or_differently_signed_calendar_fails(tmp_path: pathlib.Path) -> None:
    saved, path = _saved(tmp_path)
    for by, on in (("", ""), ("Olaf Zumpe", "2026-11-07"),
                   ("CI, sources sha256:" + "0" * 64, "2026-11-07"),
                   (json.loads(path.read_text(encoding="utf-8"))["checked_by"], "")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(checked_by=by, checked_on=on)
        forged = tmp_path / "forged.json"
        forged.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(calendar.CalendarRefused):
            calendar.check(forged, saved)
        assert calendar.main(["check", "--calendar", str(forged),
                              "--sources", str(saved)]) == 1


def test_save_refuses_a_reduction_that_drafts_differently(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _sources(tmp_path)
    monkeypatch.setattr(calendar, "reduce", lambda name, text: text.replace(
        "2022-01-10,2.401", "2022-01-10,") if name == "spot_000.csv" else text)
    with pytest.raises(calendar.SourceRefused, match="draft a different calendar"):
        calendar.save(raw, tmp_path / "saved", D(2022, 1, 7), D(2022, 1, 10),
                      fetched_on="2026-11-07")


def test_the_committed_calendar_passes_its_check() -> None:
    # The CI check itself: the calendar every private fixture is built against is
    # exactly what the committed, hash-pinned source copies draft, and signed so.
    calendar.check(calendar.DEFAULT_OUT)


def test_the_committed_sources_hold_no_price() -> None:
    # The spot prices are Thomson Reuters' data under licence to EIA: never in a
    # public place. A saved CSV keeps only whether a vintage held a day.
    import csv as csv_module
    csvs = sorted(calendar.SOURCES.glob("*.csv"))
    assert csvs
    for path in csvs:
        rows = list(csv_module.reader(path.read_text(encoding="utf-8").splitlines()))
        assert all(cell in ("", ".", "x") for row in rows[1:] for cell in row[1:]), path


def test_a_correction_moves_a_retail_release_later_and_never_earlier() -> None:
    drafted = {D(2022, 6, 13): D(2022, 6, 13), D(2022, 6, 20): D(2022, 6, 21)}
    assert calendar.corrected(drafted) == {D(2022, 6, 13): D(2022, 6, 13),
                                           D(2022, 6, 20): D(2022, 6, 29)}
    # A source that already shows a later date than the correction keeps it.
    assert calendar.corrected({D(2022, 6, 20): D(2022, 7, 5)}) == {
        D(2022, 6, 20): D(2022, 7, 5)}
    # A Monday outside the draft is not added.
    assert calendar.corrected({D(2022, 6, 13): D(2022, 6, 13)}) == {
        D(2022, 6, 13): D(2022, 6, 13)}


def test_the_second_retail_source_dates_the_monday_alfred_misdates() -> None:
    # 2026-09-14 was a hand-written correction until OMNI-190; GASREGW's
    # vintages now give its later date themselves.
    released = json.loads(calendar.DEFAULT_OUT.read_text(encoding="utf-8"))
    assert D(2026, 9, 14) not in calendar.RETAIL_CORRECTIONS
    assert released["reports"]["gdu"]["releases"]["2026-09-14"] == "2026-09-22"
