"""The release-calendar draft (OMNI-163): what it reads from each saved source.

``scripts/draft_release_calendar.py`` drafts the dates a human then checks, so
a misreading here would put a wrong date in front of the checker. Each parser
is pinned on a few lines shaped like its source.
"""

from __future__ import annotations

import datetime
import importlib.util
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


def test_a_saved_csv_missing_a_vintage_it_was_asked_for_is_refused(
        tmp_path: pathlib.Path) -> None:
    # ALFRED draws at most 12 lines in a graph and silently drops the rest.
    vintages = [D(2022, 1, 5) + datetime.timedelta(weeks=n) for n in range(3)]
    (tmp_path / "wpsr.html").write_text(_row(2022, "2022_01_12", 12, "7"), encoding="utf-8")
    (tmp_path / "gdu.txt").write_text("Release Dates:\n2022-01-10\n", encoding="utf-8")
    (tmp_path / "spot_vintages.html").write_text(
        "".join(f'<option value="{v}">{v}' for v in vintages), encoding="utf-8")
    names = [name for name, _ in calendar.urls(tmp_path, D(2022, 1, 10), D(2022, 1, 10))]
    assert names == ["wpsr.html", "gdu.txt", "spot_vintages.html", "spot_000.csv"]
    (tmp_path / "spot_000.csv").write_text(
        "observation_date,DGASUSGULF_20220105,DGASUSGULF_20220112\n2022-01-10,,1.0\n",
        encoding="utf-8")
    with pytest.raises(calendar.SourceRefused, match="2022-01-05, 2022-01-19"):
        calendar.draft(tmp_path, D(2022, 1, 10), D(2022, 1, 10))


def test_the_draft_leaves_the_check_to_a_human(tmp_path: pathlib.Path) -> None:
    (tmp_path / "wpsr.html").write_text(_row(2022, "2022_01_12", 12, "7"), encoding="utf-8")
    (tmp_path / "gdu.txt").write_text("Release Dates:\n2022-01-10\n", encoding="utf-8")
    (tmp_path / "spot_vintages.html").write_text('<option value="2022-01-12">',
                                                 encoding="utf-8")
    (tmp_path / "spot_000.csv").write_text(
        "observation_date,DGASUSGULF_20220112\n2022-01-07,1.0\n2022-01-10,1.0\n",
        encoding="utf-8")
    drafted = calendar.draft(tmp_path, D(2022, 1, 7), D(2022, 1, 10))
    assert drafted["checked_by"] == "" and drafted["checked_on"] == ""
    assert drafted["reports"]["wpsr"]["releases"] == {"2022-01-07": "2022-01-12"}
    assert drafted["reports"]["gdu"]["releases"] == {"2022-01-10": "2022-01-10"}
    assert drafted["reports"]["spot"]["releases"] == {"2022-01-07": "2022-01-12",
                                                      "2022-01-10": "2022-01-12"}
