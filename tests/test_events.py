"""OMNI-227: the refinery-outage event series, derived from EIA's PADD 5 utilisation.

The derivation first, then a capture from a synthetic EIA trace into the
event trace (append-only, honest ``known_at``), a replay that never shows a
flag before it was known, and the training split that shows it as context.
No network.
"""

from __future__ import annotations

import datetime
import json
import pathlib
from typing import Any

import pytest

from sis import events, policy, proposer, sensor_trace
from sis.adapters import replay
from sis.adapters_real import EASTERN, EIA_RULES, EIA_SERIES
from sis.backtest import TRAINING_FILE, TrainingSplit
from sis.clock import ReplayClock
from sis.events import EVENT_RULES, PADD5_UTILISATION, REFINERY_OUTAGE, EventSensor, outages
from sis.fixture_builder import FixtureRefused, build_training
from sis.ports import Reading, TimeAxis
from sis.replay import as_of
from sis.sensor_input import Absent, SourceRules
from tests.test_eia_sensor import FIRST, SECOND, _bulk_lines, _fetch, _series
from tests.test_fixture_builder import WHOLESALE, _spot_calendar, _spot_trace

UTC = datetime.UTC


def _week(day: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(day).replace(tzinfo=EASTERN)


def _util(day: str, value: float, known: str) -> Reading:
    return Reading(source="eia", series=PADD5_UTILISATION, value=value, unit="Percent",
                   event_time=_week(day), known_at=datetime.datetime.fromisoformat(known))


# --- the derivation -------------------------------------------------------------


def test_a_fall_of_four_points_in_a_week_is_an_outage() -> None:
    flags = outages([
        _util("2019-09-06", 94.7, "2019-09-12T17:00:00-04:00"),
        _util("2019-09-13", 88.4, "2019-09-19T17:00:00-04:00"),   # -6.3
        _util("2019-09-20", 87.0, "2019-09-26T17:00:00-04:00"),   # -1.4
        _util("2019-09-27", 83.0, "2019-10-03T17:00:00-04:00"),   # -4.0, exactly
        _util("2019-10-11", 70.0, "2019-10-17T17:00:00-04:00"),   # no week before it
    ])
    assert [(f.week.date().isoformat(), f.value) for f in flags] == [
        ("2019-09-13", 1.0), ("2019-09-20", 0.0), ("2019-09-27", 1.0)]
    # Known when the later of its two values was.
    assert flags[0].known_at == datetime.datetime.fromisoformat("2019-09-19T17:00:00-04:00")


def test_the_latest_vintage_counts_and_a_blanked_week_has_no_flag() -> None:
    readings = [_util("2019-09-06", 94.7, "2019-09-12T17:00:00-04:00"),
                _util("2019-09-13", 93.0, "2019-09-19T17:00:00-04:00"),
                _util("2019-09-13", 88.0, "2019-10-01T12:00:00-04:00")]   # revised down
    flag = outages(readings)[0]
    assert flag.value == 1.0
    assert flag.known_at == datetime.datetime.fromisoformat("2019-10-01T12:00:00-04:00")
    blank = Absent("eia", PADD5_UTILISATION, _week("2019-09-13"),
                   datetime.datetime.fromisoformat("2019-10-02T12:00:00-04:00"), "NA")
    assert outages(readings, [blank]) == []


def test_the_series_is_declared_and_its_rules_are_strict() -> None:
    assert PADD5_UTILISATION in EIA_SERIES and EIA_SERIES[PADD5_UTILISATION].redistributable
    assert EVENT_RULES.series[REFINERY_OUTAGE].unit == "flag"
    assert not EVENT_RULES.licensed
    assert policy.classify("sis/events.py") is policy.ChangeTier.FORBIDDEN


# --- the capture ----------------------------------------------------------------


def _util_series(*data: list[Any]) -> dict[str, Any]:
    return _series(PADD5_UTILISATION, list(data), units="Percent")


_FIRST_BULK = [_util_series(["20190906", 94.7], ["20190913", 88.4], ["20190920", 87.0])]
_SECOND_BULK = [_util_series(["20190906", 94.7], ["20190913", 92.0], ["20190920", 87.0],
                             ["20190927", 80.0])]


def _eia_trace(tmp_path: pathlib.Path) -> tuple[pathlib.Path, ReplayClock]:
    from sis.adapters_real import EIASensor

    clock = ReplayClock(FIRST)
    sensor = EIASensor(tmp_path / "eia.jsonl",
                       fetch=_fetch(_bulk_lines(_FIRST_BULK), _bulk_lines(_SECOND_BULK)),
                       clock=clock)
    sensor.capture()
    return sensor.trace, clock


def test_a_capture_appends_the_events_with_honest_known_at(tmp_path: pathlib.Path) -> None:
    eia_trace, clock = _eia_trace(tmp_path)
    sensor = EventSensor(tmp_path / "events.jsonl", eia_trace=eia_trace, clock=clock)
    clock.advance_to(FIRST + datetime.timedelta(minutes=5))
    report = sensor.capture()
    assert (report.appended, report.reconstructed, report.refused) == (2, 2, 0)
    eia_readings, _ = sensor_trace.read_back(sensor_trace.load(eia_trace), EIA_RULES)
    utilisation = {r.event_time: r.known_at for r in eia_readings}
    flags = sensor.read(datetime.datetime(2000, 1, 1, tzinfo=UTC),
                        datetime.datetime(2100, 1, 1, tzinfo=UTC), by=TimeAxis.EVENT)
    assert [(f.event_time, f.value) for f in flags] == [
        (_week("2019-09-13"), 1.0), (_week("2019-09-20"), 0.0)]
    for flag in flags:
        # Never before either utilisation value it was made from.
        assert flag.known_at == max(utilisation[flag.event_time],
                                    utilisation[flag.event_time - datetime.timedelta(days=7)])
    # The same EIA trace again adds nothing but the capture's own line.
    clock.advance_to(FIRST + datetime.timedelta(minutes=10))
    assert sensor.capture().appended == 0


def test_a_revision_is_a_new_line_known_at_its_capture(tmp_path: pathlib.Path) -> None:
    eia_trace, clock = _eia_trace(tmp_path)
    sensor = EventSensor(tmp_path / "events.jsonl", eia_trace=eia_trace, clock=clock)
    clock.advance_to(FIRST + datetime.timedelta(minutes=5))
    sensor.capture()
    from sis.adapters_real import EIASensor

    clock.advance_to(SECOND)
    EIASensor(eia_trace, fetch=_fetch(_bulk_lines(_SECOND_BULK)), clock=clock).capture()
    later = SECOND + datetime.timedelta(minutes=5)
    clock.advance_to(later)
    report = sensor.capture()
    # 09-13 revised 88.4 -> 92.0: a fall of 2.7, no longer an outage, and 09-20's
    # fall from it (5.0) now is one; 09-27 is new.
    assert (report.appended, report.reconstructed) == (3, 0)
    lines = [line for line in sensor_trace.load(sensor.trace) if line["kind"] == "reading"]
    assert len(lines) == 5                                 # append-only: nothing rewritten
    new = [line for line in lines if datetime.datetime.fromisoformat(line["known_at"]) == later]
    assert sorted((line["event_time"][:10], line["value"]) for line in new) == [
        ("2019-09-13", 0.0), ("2019-09-20", 1.0), ("2019-09-27", 1.0)]


def test_a_replay_never_shows_a_flag_before_its_known_at(tmp_path: pathlib.Path) -> None:
    eia_trace, clock = _eia_trace(tmp_path)
    sensor = EventSensor(tmp_path / "events.jsonl", eia_trace=eia_trace, clock=clock)
    clock.advance_to(FIRST + datetime.timedelta(minutes=5))
    sensor.capture()
    start = datetime.datetime(2019, 1, 1, tzinfo=UTC)
    end = datetime.datetime(2100, 1, 1, tzinfo=UTC)
    every = sensor.read(start, end, by=TimeAxis.KNOWN)
    replayed = ReplayClock(start)
    for reading in replay(sensor, replayed, start, end, by=TimeAxis.KNOWN):
        assert replayed.now() >= reading.known_at
    for reading in every:
        just_before = reading.known_at - datetime.timedelta(seconds=1)
        shown = as_of(every, [], just_before).readings
        assert (reading.series, reading.event_time) not in {
            (r.series, r.event_time) for r in shown}
        assert reading in as_of(every, [], reading.known_at).readings


def test_the_event_trace_refuses_a_line_from_another_source(tmp_path: pathlib.Path) -> None:
    eia_trace, clock = _eia_trace(tmp_path)
    clock.advance_to(FIRST + datetime.timedelta(minutes=5))
    with pytest.raises(sensor_trace.TraceRefused):
        sensor_trace.read_back(sensor_trace.load(eia_trace), EVENT_RULES)


# --- the training split ---------------------------------------------------------


def _event_lines() -> list[dict[str, Any]]:
    weeks = [datetime.datetime(2022, 1, 7, tzinfo=EASTERN) + datetime.timedelta(weeks=i)
             for i in range(60)]
    return sensor_trace.capture_lines(
        [], [sensor_trace.Fetched(REFINERY_OUTAGE, "flag",
                                  [(w, 1.0 if i % 9 == 0 else 0.0) for i, w in enumerate(weeks)])],
        EVENT_RULES, capture="events-test", fetched_at=datetime.datetime(2026, 11, 26, tzinfo=UTC),
        published=lambda series, period: period + datetime.timedelta(days=6), provenance={})


def _split(context: list[tuple[list[dict[str, Any]], SourceRules]]) -> dict[str, Any]:
    text = build_training(_spot_trace(), EIA_RULES, _spot_calendar(), WHOLESALE,
                          first=datetime.date(2022, 1, 3), name="ca_price_wholesale",
                          spec_ref="OMNI-227", context=context).files[TRAINING_FILE]
    body: dict[str, Any] = json.loads(text)
    return body


def test_the_training_split_shows_the_events_as_context_only_before_the_exam() -> None:
    plain, with_events = _split([]), _split([(_event_lines(), EVENT_RULES)])
    shown = [r for r in with_events["state"]["readings"] if r["source"] == "events"]
    assert shown and not any(r["source"] == "events" for r in plain["state"]["readings"])
    as_of_moment = datetime.datetime.fromisoformat(with_events["state"]["as_of"])
    held_out_from = datetime.date.fromisoformat(with_events["held_out_from"])
    for record in shown:
        assert datetime.datetime.fromisoformat(record["known_at"]) <= as_of_moment
        assert datetime.date.fromisoformat(record["event_time"][:10]) < held_out_from
    # The events never become part of an episode: the held-out build is untouched,
    # and the prompt says the series is context.
    split = TrainingSplit(path=TRAINING_FILE, target=with_events["target"],
                          first=with_events["first"], state=with_events["state"])
    section = proposer._training_section(split)
    assert f"{REFINERY_OUTAGE} (flag)" in section
    assert "context only: an episode's state never shows it" in section
    line = next(row for row in section.splitlines() if row.startswith(with_events["target"]))
    assert "context only" not in line


def test_a_context_that_is_licensed_or_the_splits_own_is_refused() -> None:
    licensed = SourceRules("events", EVENT_RULES.series,
                           licensed=frozenset(EVENT_RULES.series))
    with pytest.raises(FixtureRefused, match="public"):
        _split([(_event_lines(), licensed)])
    with pytest.raises(FixtureRefused, match="own"):
        _split([(_spot_trace(), EIA_RULES)])


def test_capture_sensor_derives_the_events_after_the_eia_capture() -> None:
    import scripts.capture_sensor as capture

    assert "--no-events" in (capture.__doc__ or "")
    assert events.EVENTS_TRACE.name == "events.jsonl"
