"""OMNI-33: the EIA sensor, and the capture format its trace is kept in (D12).

No network: the bulk file is built here, in EIA's own line format, and the
download is a fake session. The values for 2022 are EIA's as published
(Source: U.S. Energy Information Administration, Oct 2026 bulk file); the
later ones are made up for the test.
"""

from __future__ import annotations

import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import threading
import time
import types
import zipfile
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import pytest

from sis import adapters_real as eia
from sis import policy, sensor_trace
from sis.adapters import SimSensor, make_sensor, replay
from sis.adapters_real import EIA_BULK_URL, EIABulk, EIAFetchError, EIAFormatError, EIASensor
from sis.clock import ReplayClock
from sis.paths import PROJECT_ROOT, SPECS_DIR
from sis.ports import TimeAxis
from sis.sensor_input import Limits
from sis.sensor_trace import TraceRefused

UTC = datetime.UTC
EASTERN = eia.EASTERN
STOCKS = "PET.WGTSTP51.W"
CA = "PET.EMM_EPMRR_PTE_SCA_DPG.W"
FIRST = datetime.datetime(2026, 10, 14, 1, 49, tzinfo=UTC)
SECOND = datetime.datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
AUTUMN_2022 = (datetime.datetime(2022, 8, 1, tzinfo=UTC),
               datetime.datetime(2022, 12, 1, tzinfo=UTC))


def _series(series: str, data: list[Any], *, units: object = None,
            updated: str = "2026-10-07T17:59:25-04:00") -> dict[str, Any]:
    if units is None:
        units = "Thousand Barrels" if series == STOCKS else "Dollars per Gallon"
    return {"series_id": series, "name": "a name EIA wrote", "units": units, "f": "W",
            "copyright": "None", "source": "EIA, U.S. Energy Information Administration",
            "last_updated": updated, "data": data}


def _stocks(*data: list[Any], **kwargs: Any) -> dict[str, Any]:
    return _series(STOCKS, list(data), **kwargs)


def _ca(*data: list[Any], **kwargs: Any) -> dict[str, Any]:
    return _series(CA, list(data), **kwargs)


_AUTUMN = [_stocks(["20221125", 29943], ["20220930", 24675], ["20220805", 29192]),
           _ca(["20221003", 6.213])]


def _zip(path: pathlib.Path, lines: list[str], *, member: str = "PET.txt") -> pathlib.Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, "".join(line + "\n" for line in lines))
    return path


def _bulk_lines(series: list[dict[str, Any]]) -> list[str]:
    # As in the real file: compact JSON, series_id first, other kinds of line between.
    return ['{"category_id":"714755","name":"Petroleum"}',
            *(json.dumps(body, separators=(",", ":")) for body in series),
            json.dumps({"series_id": "PET.UNDECLARED.W", "units": "?", "data": [["x", "y"]]},
                       separators=(",", ":"))]


def _fetch(*bulks: list[str]) -> Callable[[pathlib.Path], EIABulk]:
    """A download that returns each of *bulks* in turn, as a zip in the given directory."""
    queue = list(bulks)

    def fetch(directory: pathlib.Path) -> EIABulk:
        path = _zip(directory / "PET.zip", queue.pop(0))
        data = path.read_bytes()
        return EIABulk(path, EIA_BULK_URL, hashlib.sha256(data).hexdigest(), len(data))
    return fetch


def _sensor(tmp_path: pathlib.Path, *bulks: list[dict[str, Any]],
            raw: list[list[str]] | None = None) -> tuple[EIASensor, ReplayClock]:
    clock = ReplayClock(FIRST)
    fetch = _fetch(*(raw or []), *(_bulk_lines(bulk) for bulk in bulks))
    return EIASensor(tmp_path / "eia.jsonl", fetch=fetch, clock=clock), clock


def _lines(sensor: EIASensor, kind: str | None = None) -> list[dict[str, Any]]:
    return [line for line in sensor_trace.load(sensor.trace) if kind in (None, line["kind"])]


def _at(text: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(text)


# --- the first capture: backfill ----------------------------------------------------


def test_a_first_capture_backfills_every_value_with_a_reconstructed_known_at(
        tmp_path: pathlib.Path) -> None:
    sensor, _ = _sensor(tmp_path, _AUTUMN)
    report = sensor.capture()
    assert (report.appended, report.reconstructed, report.refused, report.missing) == (
        4, 4, 0, ("PET.EMM_EPMRR_PTE_NUS_DPG.W", "PET.EMM_EPMRR_PTE_Y05LA_DPG.W",
                  "PET.EMM_EPMRR_PTE_Y05SF_DPG.W"))
    lines = _lines(sensor)
    assert lines[0]["kind"] == "capture" and all(line["kind"] == "reading" for line in lines[1:])
    assert all(line["reconstructed"] is True for line in lines[1:])
    week = next(line for line in lines[1:] if line["event_time"].startswith("2022-09-30"))
    # The week ending Friday 30 Sep 2022 came out on Wednesday 5 Oct; it is taken
    # as out by Thursday 5 p.m. Eastern, never as known at the fetch in 2026.
    assert _at(week["event_time"]) == datetime.datetime(2022, 9, 30, tzinfo=EASTERN)
    assert _at(week["known_at"]) == datetime.datetime(2022, 10, 6, 17, tzinfo=EASTERN)
    price = next(line for line in lines[1:] if line["series"] == CA)
    assert _at(price["known_at"]) == datetime.datetime(2022, 10, 4, 17, tzinfo=EASTERN)


def test_the_autumn_2022_values_read_back_as_eia_published_them(tmp_path: pathlib.Path) -> None:
    sensor, _ = _sensor(tmp_path, _AUTUMN)
    sensor.capture()
    readings = sensor.read(*AUTUMN_2022, by=TimeAxis.EVENT)
    assert [(r.series, r.event_time.date().isoformat(), r.value, r.unit) for r in readings] == [
        (STOCKS, "2022-08-05", 29192.0, "Thousand Barrels"),
        (STOCKS, "2022-09-30", 24675.0, "Thousand Barrels"),
        (CA, "2022-10-03", 6.213, "Dollars per Gallon"),
        (STOCKS, "2022-11-25", 29943.0, "Thousand Barrels"),
    ]
    assert all(r.source == "eia" for r in readings)


def test_the_capture_line_says_where_the_data_came_from(tmp_path: pathlib.Path) -> None:
    sensor, _ = _sensor(tmp_path, _AUTUMN)
    sensor.capture()
    head = _lines(sensor, "capture")[0]
    data = (tmp_path / "check.zip")
    _zip(data, _bulk_lines(_AUTUMN))
    assert head["fetched_at"] == FIRST.isoformat()
    assert head["capture"] == f"eia-20261014T014900.000000Z-{head['provenance']['sha256'][:8]}"
    assert head["provenance"]["url"] == EIA_BULK_URL
    assert head["provenance"]["sha256"] == hashlib.sha256(data.read_bytes()).hexdigest()
    assert head["provenance"]["last_updated"][STOCKS] == "2026-10-07T17:59:25-04:00"
    # EIA's reuse policy asks for an acknowledgment that dates the publication.
    assert head["provenance"]["attribution"] == \
        "Source: U.S. Energy Information Administration (Oct 2026)"
    assert head["counts"][STOCKS] == {"received": 3, "readings": 3, "absent": 0,
                                      "rejected": 0, "appended": 3}


def test_a_reconstructed_known_at_is_never_after_the_fetch(tmp_path: pathlib.Path) -> None:
    # Fetched on the Wednesday of the release: Thursday's schedule would be later
    # than the moment the value was in hand.
    clock = ReplayClock(datetime.datetime(2022, 10, 5, 15, 0, tzinfo=UTC))
    sensor = EIASensor(tmp_path / "eia.jsonl", fetch=_fetch(_bulk_lines([_stocks(
        ["20220930", 24675])])), clock=clock)
    sensor.capture()
    assert _at(_lines(sensor, "reading")[0]["known_at"]) == clock.now()


# --- later captures: watching -------------------------------------------------------


def test_a_capture_with_nothing_new_appends_only_its_own_line(tmp_path: pathlib.Path) -> None:
    sensor, clock = _sensor(tmp_path, _AUTUMN, _AUTUMN)
    sensor.capture()
    clock.advance_to(SECOND)
    report = sensor.capture()
    assert (report.appended, report.reconstructed) == (0, 0)
    assert [line["kind"] for line in _lines(sensor)] == ["capture"] + ["reading"] * 4 + ["capture"]


def test_a_new_week_and_a_revision_are_known_at_the_fetch_that_saw_them(
        tmp_path: pathlib.Path) -> None:
    later = [_stocks(["20221202", 30001], ["20221125", 29943], ["20220930", 24700],
                     ["20220805", 29192]), _ca(["20221003", 6.213])]
    sensor, clock = _sensor(tmp_path, _AUTUMN, later)
    sensor.capture()
    clock.advance_to(SECOND)
    report = sensor.capture()
    assert (report.appended, report.reconstructed) == (2, 0)
    added = _lines(sensor)[6:]
    assert [(line["event_time"][:10], line["value"], _at(line["known_at"]), line["reconstructed"])
            for line in added] == [("2022-12-02", 30001.0, SECOND, False),
                                   ("2022-09-30", 24700.0, SECOND, False)]
    # Append-only: the first vintage is still there, and a replay by publication
    # time shows each value only from the moment it was known.
    week = [r for r in sensor.read(*AUTUMN_2022, by=TimeAxis.EVENT)
            if r.event_time.date().isoformat() == "2022-09-30"]
    assert [(r.value, r.known_at) for r in week] == [
        (24675.0, datetime.datetime(2022, 10, 6, 17, tzinfo=EASTERN)), (24700.0, SECOND)]
    known_in_2022 = sensor.read(datetime.datetime(2022, 1, 1, tzinfo=UTC),
                                datetime.datetime(2023, 1, 1, tzinfo=UTC), by=TimeAxis.KNOWN)
    assert 24700.0 not in [r.value for r in known_in_2022]


def test_a_null_is_stated_as_absent_and_never_read_as_a_value(tmp_path: pathlib.Path) -> None:
    sensor, clock = _sensor(tmp_path, [_stocks(["20220930", None])],
                            [_stocks(["20220930", 24675])])
    sensor.capture()
    assert [(line["kind"], line["marker"]) for line in _lines(sensor)[1:]] == [("absent", "")]
    assert sensor.read(*AUTUMN_2022, by=TimeAxis.EVENT) == []
    clock.advance_to(SECOND)
    assert sensor.capture().appended == 1     # the value arrived: a change, known now
    [reading] = sensor.read(*AUTUMN_2022, by=TimeAxis.EVENT)
    assert (reading.value, reading.known_at) == (24675.0, SECOND)


def test_a_value_the_source_later_blanks_shows_as_withdrawn(tmp_path: pathlib.Path) -> None:
    sensor, clock = _sensor(tmp_path, [_stocks(["20220930", 24675])],
                            [_stocks(["20220930", None])])
    sensor.capture()
    clock.advance_to(SECOND)
    assert sensor.capture().appended == 1
    everything = (datetime.datetime(2022, 1, 1, tzinfo=UTC),
                  datetime.datetime(2027, 1, 1, tzinfo=UTC))
    # The published vintage stays what it was, and the blanking has its own time.
    [reading] = sensor.read(*everything, by=TimeAxis.KNOWN)
    assert (reading.value, reading.known_at) == (
        24675.0, datetime.datetime(2022, 10, 6, 17, tzinfo=EASTERN))
    [withdrawn] = sensor.absent(*everything, by=TimeAxis.KNOWN)
    assert (withdrawn.series, withdrawn.event_time, withdrawn.known_at, withdrawn.marker) == (
        STOCKS, datetime.datetime(2022, 9, 30, tzinfo=EASTERN), SECOND, "")
    assert sensor.absent(*AUTUMN_2022, by=TimeAxis.KNOWN) == []   # not known until 2026
    with pytest.raises(ValueError, match="timezones"):
        sensor.absent(datetime.datetime(2022, 1, 1), everything[1], by=TimeAxis.KNOWN)


def test_a_capture_not_after_the_last_one_is_refused_and_appends_nothing(
        tmp_path: pathlib.Path) -> None:
    # OMNI-160 (L54): at one instant, a withdrawal and a restoration could not be
    # ordered, and the trace's own view of the period was decided by a tie.
    withdrawn = [_stocks(["20220930", None])]
    sensor, clock = _sensor(tmp_path, [_stocks(["20220930", 24675])], withdrawn, withdrawn)
    sensor.capture()
    before = sensor.trace.read_bytes()
    with pytest.raises(TraceRefused, match="not after the trace's last capture"):
        sensor.capture()                                        # the same instant
    assert sensor.trace.read_bytes() == before
    earlier = EIASensor(sensor.trace, clock=ReplayClock(FIRST - datetime.timedelta(hours=1)),
                        fetch=_fetch(_bulk_lines([_stocks(["20220930", None])])))
    with pytest.raises(TraceRefused, match="clock is behind"):
        earlier.capture()                                       # a clock stepped back
    assert sensor.trace.read_bytes() == before
    clock.advance_to(SECOND)
    assert sensor.capture().appended == 1                       # later is fine


def test_a_capture_is_stamped_under_the_traces_lock(tmp_path: pathlib.Path) -> None:
    # Stamped before the lock, a capture that downloaded first but reached the
    # lock second was refused as if the clock had gone back (OMNI-160).
    trace = tmp_path / "eia.jsonl"
    held: list[bool] = []

    class _Probe:
        def now(self) -> datetime.datetime:
            with open(trace, "rb") as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append(False)
                except BlockingIOError:
                    held.append(True)
            return FIRST

    EIASensor(trace, fetch=_fetch(_bulk_lines(_AUTUMN)), clock=_Probe()).capture()
    assert held == [True]


def test_a_trace_whose_captures_are_out_of_order_fails_the_read(tmp_path: pathlib.Path) -> None:
    sensor, clock = _sensor(tmp_path, _AUTUMN, _AUTUMN)
    sensor.capture()
    clock.advance_to(SECOND)
    sensor.capture()                                            # nothing new: its line alone
    second = len(sensor.trace.read_text(encoding="utf-8").splitlines()) - 1
    _edit(sensor, lambda lines: _with(lines, second, fetched_at=FIRST.isoformat()))
    with pytest.raises(TraceRefused, match="not after the one before it"):
        sensor.read(*AUTUMN_2022, by=TimeAxis.EVENT)


def test_a_value_withdrawn_and_restored_reads_as_restored(tmp_path: pathlib.Path) -> None:
    third, fourth = SECOND + datetime.timedelta(days=1), SECOND + datetime.timedelta(days=2)
    sensor, clock = _sensor(tmp_path, *([_stocks(["20220930", value])]
                                        for value in (24675, None, 24675, 24675)))
    for when in (None, SECOND, third, fourth):
        if when is not None:
            clock.advance_to(when)
        report = sensor.capture()
    assert report.appended == 0                     # the restored value, unchanged
    everything = (datetime.datetime(2022, 1, 1, tzinfo=UTC),
                  datetime.datetime(2027, 1, 1, tzinfo=UTC))
    assert [r.known_at for r in sensor.read(*everything, by=TimeAxis.KNOWN)] == [
        datetime.datetime(2022, 10, 6, 17, tzinfo=EASTERN), third]


# --- what the door refuses ----------------------------------------------------------


def test_what_the_door_refuses_is_counted_and_never_written(tmp_path: pathlib.Path) -> None:
    sensor, _ = _sensor(tmp_path, [_stocks(["20220930", 24675], ["20220923", 2_467_500],
                                           ["2022-09-16", 24600], ["20220909", "7..1"])])
    report = sensor.capture()
    assert (report.appended, report.refused) == (1, 3)
    head = _lines(sensor, "capture")[0]
    assert head["counts"][STOCKS] == {"received": 4, "readings": 1, "absent": 0, "rejected": 3,
                                      "appended": 1, "rejected.number": 1, "rejected.range": 1,
                                      "rejected.time": 1}
    assert sorted(r["reason"] for r in head["rejections"]) == ["number", "range", "time"]
    assert [line["value"] for line in _lines(sensor, "reading")] == [24675.0]


def test_a_changed_unit_is_refused_for_the_whole_series(tmp_path: pathlib.Path) -> None:
    sensor, _ = _sensor(tmp_path, [_stocks(["20220930", 24.675], units="Million Barrels")])
    report = sensor.capture()
    assert (report.appended, report.refused) == (0, 1)
    assert _lines(sensor, "capture")[0]["counts"][STOCKS]["rejected.unit"] == 1


@pytest.mark.parametrize("second", [24676, "7..1", 2_467_500])
def test_a_period_sent_twice_is_refused_every_time_valid_or_not(
        tmp_path: pathlib.Path, second: object) -> None:
    sensor, _ = _sensor(tmp_path, [_stocks(["20220930", 24675], ["20220930", second],
                                           ["20220923", 24812])])
    report = sensor.capture()
    assert (report.appended, report.refused) == (1, 2)
    counts = _lines(sensor, "capture")[0]["counts"][STOCKS]
    assert (counts["received"], counts["readings"], counts["rejected.duplicate"]) == (3, 1, 2)
    assert [line["event_time"][:10] for line in _lines(sensor, "reading")] == ["2022-09-23"]


def test_a_period_off_the_calendar_is_refused_not_a_crash(tmp_path: pathlib.Path) -> None:
    sensor, _ = _sensor(tmp_path, [_stocks(["99991231", 24000], ["20220930", 24675])])
    report = sensor.capture()
    assert (report.appended, report.refused) == (1, 1)
    assert _lines(sensor, "capture")[0]["counts"][STOCKS]["rejected.known_before_period"] == 1


def test_refusals_are_all_counted_and_only_some_kept_word_for_word(
        tmp_path: pathlib.Path) -> None:
    bad = [[f"2022{week:02d}01", 1] for week in range(1, 13)] + \
          [[f"2021{week:02d}01", 1] for week in range(1, 13)]
    sensor, _ = _sensor(tmp_path, [_stocks(*bad)])
    assert sensor.capture().refused == 24
    head = _lines(sensor, "capture")[0]
    assert len(head["rejections"]) == sensor_trace.MAX_REJECTIONS_KEPT < 24
    assert head["counts"][STOCKS]["rejected.range"] == 24


def test_a_period_the_source_stops_sending_is_counted_as_vanished(
        tmp_path: pathlib.Path) -> None:
    sensor, clock = _sensor(tmp_path, [_stocks(["20221007", 24800], ["20220930", 24675])],
                            [_stocks(["20221007", 24800])])
    sensor.capture()
    clock.advance_to(SECOND)
    report = sensor.capture()
    assert (report.appended, report.vanished) == (0, 1)
    assert "1 periods no longer sent" in report.report()
    assert _lines(sensor, "capture")[1]["counts"][STOCKS]["vanished"] == 1


def test_each_capture_compares_with_the_latest_vintage_not_the_first(
        tmp_path: pathlib.Path) -> None:
    values = [24675, 24700, 24700, 24675]
    clock = ReplayClock(FIRST)
    sensor = EIASensor(tmp_path / "eia.jsonl", clock=clock, fetch=_fetch(
        *(_bulk_lines([_stocks(["20220930", value])]) for value in values)))
    appended = []
    for day, _ in enumerate(values):
        clock.advance_to(FIRST + datetime.timedelta(days=day))
        appended.append(sensor.capture().appended)
    assert appended == [1, 1, 0, 1]
    assert [line["value"] for line in _lines(sensor, "reading")] == [24675.0, 24700.0, 24675.0]


def test_a_week_with_no_new_release_is_normal(tmp_path: pathlib.Path) -> None:
    sensor, clock = _sensor(tmp_path, _AUTUMN, _AUTUMN)
    sensor.capture()
    clock.advance_to(SECOND)
    report = sensor.capture()
    assert "0 new or changed" in report.report() and report.refused == 0


# --- replay -----------------------------------------------------------------------


def test_a_captured_window_replays_through_the_simulated_sensor_unchanged(
        tmp_path: pathlib.Path) -> None:
    later = [_stocks(["20221202", 30001], ["20221125", 29943], ["20220930", 24700],
                     ["20220805", 29192]), _ca(["20221003", 6.213])]
    sensor, clock = _sensor(tmp_path, _AUTUMN, later)
    sensor.capture()
    clock.advance_to(SECOND)
    sensor.capture()
    window = (datetime.datetime(2022, 1, 1, tzinfo=UTC), datetime.datetime(2027, 1, 1, tzinfo=UTC))
    captured = sensor.read(*window, by=TimeAxis.KNOWN)
    simulated = SimSensor.from_records(_lines(sensor, "reading"))
    replay_clock = ReplayClock(window[0])
    replayed = list(replay(simulated, replay_clock, *window, by=TimeAxis.KNOWN))
    assert replayed == captured and len(replayed) == 6
    assert replay_clock.now() == SECOND


# --- the trace is runtime data: read back through the door -------------------------


def _edit(sensor: EIASensor, change: Callable[[list[str]], list[str]]) -> None:
    text = sensor.trace.read_text(encoding="utf-8")
    sensor.trace.write_text("".join(line + "\n" for line in change(text.splitlines())),
                            encoding="utf-8")


def _captured(tmp_path: pathlib.Path) -> EIASensor:
    sensor, _ = _sensor(tmp_path, _AUTUMN)
    sensor.capture()
    return sensor


def _with(lines: list[str], index: int, **fields: object) -> list[str]:
    line = json.loads(lines[index])
    line.update(fields)
    return [*lines[:index], json.dumps(line), *lines[index + 1:]]


@pytest.mark.parametrize("change", [
    pytest.param(lambda lines: _with(lines, 1, value=1e9), id="value out of range"),
    pytest.param(lambda lines: _with(lines, 1, series="PET.UNDECLARED.W"), id="undeclared"),
    pytest.param(lambda lines: _with(lines, 1, known_at="2022-09-30T00:00:00"), id="no tz"),
    pytest.param(lambda lines: _with(lines, 1, kind="absent", marker="24675"),
                 id="absent holding a value"),
    pytest.param(lambda lines: _with(lines, 1, value=None), id="reading holding no value"),
    pytest.param(lambda lines: _with(lines, 1, unit="Million Barrels"), id="unit"),
    pytest.param(lambda lines: _with(lines, 1, known_at="2026-10-14T02:00:00+00:00"),
                 id="known after its capture"),
    pytest.param(lambda lines: _with(lines, 1, reconstructed=False), id="not reconstructed"),
    pytest.param(lambda lines: _with(lines, 1, capture="no-such-capture"), id="other capture"),
    pytest.param(lambda lines: [*lines[:1], *lines[2:]], id="block cut short"),
    pytest.param(lambda lines: [*lines, json.dumps({
        "kind": "reading", "source": "eia", "series": CA, "value": 4.10,
        "unit": "Dollars per Gallon", "event_time": "2022-10-03T00:00:00-04:00",
        "known_at": "2022-10-03T00:00:00-04:00", "reconstructed": True,
        "capture": json.loads(lines[0])["capture"]})], id="forged line"),
    pytest.param(lambda lines: _with(lines, 1, source="cec"), id="other source"),
    pytest.param(lambda lines: [*lines, "{not json"], id="not json"),
    pytest.param(lambda lines: [*lines, '{"kind": "outcome"}'], id="unknown kind"),
])
def test_an_edited_trace_fails_the_read(tmp_path: pathlib.Path,
                                        change: Callable[[list[str]], list[str]]) -> None:
    sensor = _captured(tmp_path)
    _edit(sensor, change)
    with pytest.raises(TraceRefused):
        sensor.read(*AUTUMN_2022, by=TimeAxis.EVENT)


def test_a_cut_short_trace_is_neither_read_nor_appended_to(tmp_path: pathlib.Path) -> None:
    sensor, clock = _sensor(tmp_path, _AUTUMN, _AUTUMN)
    sensor.capture()
    cut = sensor.trace.read_text(encoding="utf-8")[:-10]
    sensor.trace.write_text(cut, encoding="utf-8")
    with pytest.raises(TraceRefused, match="incomplete"):
        sensor.read(*AUTUMN_2022, by=TimeAxis.EVENT)
    clock.advance_to(SECOND)
    with pytest.raises(TraceRefused, match="incomplete"):
        sensor.capture()
    assert sensor.trace.read_text(encoding="utf-8") == cut


def test_a_block_that_cannot_be_written_whole_is_cut_back_off(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sensor, clock = _sensor(tmp_path, _AUTUMN, [_stocks(["20221202", 30001])])
    sensor.capture()
    before = sensor.trace.read_bytes()

    class _DiskFills:                      # writes half of the first call, then fails
        def __getattr__(self, name: str) -> Any:
            return getattr(os, name)

        @staticmethod
        def write(fd: int, data: Any) -> int:
            os.write(fd, bytes(data[:len(data) // 2]))
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(sensor_trace, "os", _DiskFills())
    clock.advance_to(SECOND)
    with pytest.raises(OSError, match="No space"):
        sensor.capture()
    monkeypatch.undo()
    assert sensor.trace.read_bytes() == before
    assert len(sensor.read(*AUTUMN_2022, by=TimeAxis.EVENT)) == 4


def test_two_captures_at_once_backfill_once(tmp_path: pathlib.Path) -> None:
    trace = tmp_path / "eia.jsonl"
    started, seen = threading.Event(), []

    def second() -> None:
        started.set()
        sensor_trace.append(trace, lambda prior: seen.append(len(prior)) or [])

    def first(prior: list[dict[str, Any]]) -> list[dict[str, Any]]:
        worker.start()
        started.wait(5)
        time.sleep(0.2)                   # the second append is now waiting for the lock
        return [{"kind": "capture", "capture": "c1", "fetched_at": FIRST.isoformat(),
                 "counts": {}}]

    worker = threading.Thread(target=second)
    sensor_trace.append(trace, first)
    worker.join(5)
    assert seen == [1]                    # it read the first block, not an empty file


def test_a_read_during_a_capture_waits_for_its_whole_block(tmp_path: pathlib.Path) -> None:
    # OMNI-159 (M29): load() read without the writer's lock, so a read that
    # landed mid-block saw a cut-short trace and called it edited.
    later = [_stocks(["20221202", 30001], ["20221125", 29943], ["20220930", 24700],
                     ["20220805", 29192]), _ca(["20221003", 6.213])]
    whole, clock = _sensor(tmp_path / "whole", _AUTUMN, later)
    whole.capture()
    first = whole.trace.read_bytes()
    clock.advance_to(SECOND)
    whole.capture()
    block = whole.trace.read_bytes()[len(first):]
    expected = whole.read(*AUTUMN_2022, by=TimeAxis.KNOWN)

    sensor = EIASensor(tmp_path / "eia.jsonl")
    sensor.trace.write_bytes(first)
    outcome: list[object] = []

    def read() -> None:
        try:
            outcome.append(sensor.read(*AUTUMN_2022, by=TimeAxis.KNOWN))
        except Exception as exc:  # noqa: BLE001 - the test inspects it
            outcome.append(exc)

    reader = threading.Thread(target=read)
    with open(sensor.trace, "ab") as writer:     # a capture, halfway through its block
        fcntl.flock(writer, fcntl.LOCK_EX)
        writer.write(block[:len(block) // 2])
        writer.flush()
        reader.start()
        time.sleep(0.3)
        assert outcome == [], "the read went ahead of a capture still writing"
        writer.write(block[len(block) // 2:])
        writer.flush()
    reader.join(10)
    assert outcome == [expected]


def test_a_series_larger_than_one_batch_is_captured_and_read_in_batches(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A daily series has more values than the door takes at once (WTI daily:
    # 10,264). Here a batch holds two, and the stocks series has three.
    monkeypatch.setattr(sensor_trace, "Limits", lambda: Limits(max_batch=2))
    sensor, _ = _sensor(tmp_path, _AUTUMN)
    report = sensor.capture()
    assert (report.appended, report.refused) == (4, 0)
    assert len(sensor.read(*AUTUMN_2022, by=TimeAxis.EVENT)) == 4


def test_a_refusal_in_a_later_batch_names_its_own_record(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sensor_trace, "Limits", lambda: Limits(max_batch=2))
    sensor, _ = _sensor(tmp_path, [_stocks(["20221007", 24800], ["20220930", 24675],
                                           ["20220923", 2_467_500])])
    assert sensor.capture().refused == 1
    [refusal] = _lines(sensor, "capture")[0]["rejections"]
    assert (refusal["index"], refusal["reason"]) == (2, "range")


def test_nothing_captured_yet_reads_as_nothing(tmp_path: pathlib.Path) -> None:
    sensor, _ = _sensor(tmp_path, _AUTUMN)
    assert sensor.read(*AUTUMN_2022, by=TimeAxis.EVENT) == []
    assert not sensor.trace.exists()


# --- never under specs/ ---------------------------------------------------------------


def test_no_trace_is_written_under_specs(tmp_path: pathlib.Path) -> None:
    assert SPECS_DIR.resolve() not in eia.EIA_TRACE.resolve().parents
    with pytest.raises(PermissionError, match="specs/"):
        EIASensor(SPECS_DIR / "eia.jsonl")
    link = tmp_path / "exam"
    link.symlink_to(SPECS_DIR, target_is_directory=True)
    with pytest.raises(PermissionError, match="specs/"):
        EIASensor(link / "eia.jsonl")
    with pytest.raises(PermissionError, match="specs/"):
        sensor_trace.append(SPECS_DIR / "roman" / "trace.jsonl", lambda prior: [])
    assert not (SPECS_DIR / "eia.jsonl").exists()
    assert not (SPECS_DIR / "roman" / "trace.jsonl").exists()


def test_specs_is_recognised_however_it_is_spelt(tmp_path: pathlib.Path) -> None:
    shouted = SPECS_DIR.parent / "SPECS"
    if not shouted.exists():
        pytest.skip("a case-sensitive file system: SPECS/ is another directory")
    with pytest.raises(PermissionError, match="specs/"):
        EIASensor(shouted / "roman" / "eia.jsonl")
    assert not (SPECS_DIR / "roman" / "eia.jsonl").exists()


def test_a_trace_with_a_second_name_is_not_written(tmp_path: pathlib.Path) -> None:
    trace = tmp_path / "eia.jsonl"
    trace.write_text("", encoding="utf-8")
    os.link(trace, tmp_path / "elsewhere.jsonl")
    with pytest.raises(PermissionError, match="hard link"):
        sensor_trace.append(trace, lambda prior: [{"kind": "capture"}])
    assert trace.read_text(encoding="utf-8") == ""


# --- the bulk file ------------------------------------------------------------------


def _capture_raw(tmp_path: pathlib.Path, lines: list[str]) -> EIASensor:
    sensor, _ = _sensor(tmp_path, raw=[lines])
    return sensor


@pytest.mark.parametrize("lines, match", [
    pytest.param([json.dumps(_stocks(["20220930", 24675]), separators=(",", ":"))[:-1]],
                 "not JSON", id="cut line"),
    pytest.param(['{"series_id":"PET.WGTSTP51.W","data":{"20220930":24675}}'],
                 "list of values", id="data not a list"),
    pytest.param([json.dumps(_stocks(["20220930", 24675]), separators=(",", ":"))] * 2,
                 "twice", id="series twice"),
])
def test_a_misshapen_bulk_file_captures_nothing(tmp_path: pathlib.Path, lines: list[str],
                                                match: str) -> None:
    sensor = _capture_raw(tmp_path, lines)
    with pytest.raises(EIAFormatError, match=match):
        sensor.capture()
    assert not sensor.trace.exists()


def test_a_zip_without_its_member_or_not_a_zip_captures_nothing(tmp_path: pathlib.Path) -> None:
    other = _zip(tmp_path / "other.zip", ["{}"], member="NG.txt")
    with pytest.raises(EIAFormatError, match="no PET.txt"):
        eia.read_eia_bulk(other)
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"<html>Service Unavailable</html>")
    with pytest.raises(EIAFormatError, match="not a readable zip"):
        eia.read_eia_bulk(junk)


def test_only_deflate_is_unpacked(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "PET.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_BZIP2) as archive:
        archive.writestr("PET.txt", "".join(line + "\n" for line in _bulk_lines(_AUTUMN)))
    with pytest.raises(EIAFormatError, match="not deflate"):
        eia.read_eia_bulk(path)


def test_size_limits_hold_before_anything_is_unpacked(tmp_path: pathlib.Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    path = _zip(tmp_path / "PET.zip", _bulk_lines(_AUTUMN))
    monkeypatch.setattr(eia, "_EIA_MAX_UNZIPPED", 100)
    with pytest.raises(EIAFormatError, match="would unpack"):
        eia.read_eia_bulk(path)
    monkeypatch.setattr(eia, "_EIA_MAX_UNZIPPED", 10**9)
    monkeypatch.setattr(eia, "_EIA_MAX_LINE", 60)
    long_decoy = '{"series_id":"PET.UNDECLARED.W","data":[' + '["20220930",1],' * 20 + "[]]}"
    fine = _zip(tmp_path / "fine.zip", [long_decoy, '{"series_id":"PET.WGTSTP51.W","data":[]}'])
    assert [f.series for f in eia.read_eia_bulk(fine)[0]] == [STOCKS]   # the decoy is skipped
    with pytest.raises(EIAFormatError, match="over 60 bytes"):
        eia.read_eia_bulk(path)


def test_eia_periods_are_placed_at_midnight_eastern_or_left_for_the_door() -> None:
    assert eia.eia_period("20220930") == datetime.datetime(2022, 9, 30, tzinfo=EASTERN)
    assert eia.eia_period("20221345") == "20221345"
    assert eia.eia_period("2022-09-30") == "2022-09-30"
    assert eia.eia_period(20220930) == 20220930


def test_the_release_schedule_is_after_the_regular_release() -> None:
    friday = datetime.datetime(2022, 9, 2, tzinfo=EASTERN)     # Labor Day week: out Thu 8 Sep
    assert eia.eia_published(STOCKS, friday) == datetime.datetime(2022, 9, 8, 17, tzinfo=EASTERN)
    monday = datetime.datetime(2022, 10, 3, tzinfo=EASTERN)
    assert eia.eia_published(CA, monday) == datetime.datetime(2022, 10, 4, 17, tzinfo=EASTERN)


# --- the download ---------------------------------------------------------------------


class _Response:
    def __init__(self, status: int = 200, body: bytes = b"zip", url: str = EIA_BULK_URL,
                 headers: Mapping[str, str] | None = None) -> None:
        self.status_code, self.url, self.headers, self._body = status, url, headers or {}, body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]


class _Session:
    def __init__(self, *outcomes: _Response | Exception) -> None:
        self._outcomes, self.calls = list(outcomes), 0

    def get(self, url: str, **kwargs: Any) -> _Response:
        assert url == EIA_BULK_URL and kwargs["stream"] is True and kwargs["timeout"] > 0
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_the_download_retries_what_may_pass(tmp_path: pathlib.Path) -> None:
    waits: list[float] = []
    session = _Session(ConnectionError("reset"), _Response(503), _Response(429),
                       _Response(body=b"PK-bytes"))
    bulk = eia.fetch_eia_bulk(tmp_path, session=session, sleep=waits.append, attempts=4)
    assert (session.calls, waits) == (4, [2.0, 4.0, 8.0])
    assert bulk.path.read_bytes() == b"PK-bytes" and bulk.size == 8
    assert bulk.sha256 == hashlib.sha256(b"PK-bytes").hexdigest()


def test_the_download_gives_up_after_its_attempts(tmp_path: pathlib.Path) -> None:
    session = _Session(*(TimeoutError("slow") for _ in range(3)))
    with pytest.raises(EIAFetchError, match="failed 3 times"):
        eia.fetch_eia_bulk(tmp_path, session=session, sleep=lambda _: None)


@pytest.mark.parametrize("response, match", [
    pytest.param(_Response(404), "HTTP 404", id="not found"),
    pytest.param(_Response(url="https://eia.example.net/PET.zip"), "off EIA", id="redirected"),
    pytest.param(_Response(headers={"Content-Length": str(10**12)}), "over", id="declared size"),
])
def test_the_download_does_not_retry_what_will_not_improve(
        tmp_path: pathlib.Path, response: _Response, match: str) -> None:
    session = _Session(response)
    with pytest.raises(EIAFetchError, match=match):
        eia.fetch_eia_bulk(tmp_path, session=session, sleep=lambda _: None)
    assert session.calls == 1


def test_a_local_disk_that_will_not_take_the_download_is_not_blamed_on_eia(
        tmp_path: pathlib.Path) -> None:
    session = _Session(_Response(body=b"PK-bytes"))
    with pytest.raises(EIAFetchError, match="cannot write the download"):
        eia.fetch_eia_bulk(tmp_path / "gone", session=session, sleep=lambda _: None)
    assert session.calls == 1


def test_the_acknowledgment_is_dated_by_the_data_not_the_fetch() -> None:
    november = datetime.datetime(2026, 11, 20, tzinfo=UTC)
    assert eia.eia_attribution({STOCKS: "2026-10-07T17:59:25-04:00", CA: None}, november) == \
        "Source: U.S. Energy Information Administration (Oct 2026)"
    assert eia.eia_attribution({}, november) == \
        "Source: U.S. Energy Information Administration (Nov 2026)"


def test_a_download_larger_than_it_declared_is_stopped(tmp_path: pathlib.Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eia, "_EIA_MAX_ZIPPED", 4)
    with pytest.raises(EIAFetchError, match="over 4 bytes"):
        eia.fetch_eia_bulk(tmp_path, session=_Session(_Response(body=b"0123456789")),
                           sleep=lambda _: None)


# --- wiring and policy ------------------------------------------------------------------


def test_the_real_backend_is_the_eia_sensor_and_reads_without_a_network() -> None:
    sensor = make_sensor("real")
    assert isinstance(sensor, EIASensor) and sensor.trace == eia.EIA_TRACE.resolve()


def test_the_preflight_reports_the_sensor(tmp_path: pathlib.Path,
                                          monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    sensor, _ = _sensor(tmp_path, _AUTUMN)
    sensor.capture()
    _edit(sensor, lambda lines: [*lines, json.dumps({
        "kind": "capture", "capture": "x", "fetched_at": "\x1b[2J\x1b[31mALL OK", "counts": {}})])

    class _Head:
        headers = {"Content-Length": "56038579"}

        @staticmethod
        def raise_for_status() -> None:
            return None

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(
        head=lambda url, **kwargs: _Head()))
    monkeypatch.setattr(eia, "EIA_TRACE", sensor.trace)
    spec = importlib.util.spec_from_file_location(
        "check_connections", PROJECT_ROOT / "scripts" / "check_connections.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.check_sensor() is True
    out = capsys.readouterr().out
    assert "56038579 bytes" in out and "4 readings (4 with a reconstructed known_at)" in out
    assert "\x1b" not in out and "last fetched ?" in out


def test_how_a_trace_is_captured_is_guardrail_code() -> None:
    for path in ("sis/sensor_trace.py", "sis/adapters_real.py", "sis/sensor_input.py"):
        assert policy.classify(path) is policy.ChangeTier.FORBIDDEN, path
