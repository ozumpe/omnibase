"""OMNI-31: the Sensor port, a reading with two times, and the simulated adapter.

No network, no wall clock, no Ray: everything here is a reading, a canned
trace and a replay clock.
"""

from __future__ import annotations

import datetime
import json

import pytest

from sis import config, policy
from sis.adapters import SimSensor, make_sensor, replay
from sis.clock import ReplayClock
from sis.ports import Reading, Sensor, TimeAxis

UTC = datetime.UTC


def _reading(event: str, known: str, value: float = 5.0, series: str = "price") -> Reading:
    return Reading(source="eia", series=series, value=value, unit="USD/gal",
                   event_time=event, known_at=known)  # type: ignore[arg-type]


# California, autumn 2022: weekly prices are for a Monday and out the same day;
# weekly stocks are for a Friday and out the following Wednesday.
_TRACE = [
    _reading("2022-09-23T00:00:00+00:00", "2022-09-28T14:30:00+00:00", 27.1, "stocks"),
    _reading("2022-09-26T00:00:00+00:00", "2022-09-26T21:00:00+00:00", 5.82),
    _reading("2022-09-30T00:00:00+00:00", "2022-10-05T14:30:00+00:00", 26.4, "stocks"),
    _reading("2022-10-03T00:00:00+00:00", "2022-10-03T21:00:00+00:00", 6.38),
]
_START = datetime.datetime(2022, 9, 1, tzinfo=UTC)
_END = datetime.datetime(2022, 11, 1, tzinfo=UTC)


# --- a reading ------------------------------------------------------------------


def test_a_reading_carries_two_times_and_both_have_a_timezone() -> None:
    reading = _reading("2022-09-30T00:00:00-07:00", "2022-10-05T10:30:00-04:00")
    assert reading.event_time.utcoffset() == datetime.timedelta(hours=-7)
    assert reading.known_at.utcoffset() == datetime.timedelta(hours=-4)
    assert reading.at(TimeAxis.EVENT) == reading.event_time
    assert reading.at(TimeAxis.KNOWN) == reading.known_at


@pytest.mark.parametrize("field", ["event_time", "known_at"])
def test_a_reading_without_a_timezone_on_either_time_is_refused(field: str) -> None:
    # "10:30, somewhere" cannot be replayed, and it cannot be fixed afterwards.
    times = {"event_time": "2022-09-30T00:00:00+00:00", "known_at": "2022-10-05T14:30:00+00:00"}
    times[field] = "2022-10-05T14:30:00"
    with pytest.raises(ValueError, match=f"{field}.*no timezone"):
        Reading(source="eia", series="stocks", value=1.0, unit="MMbbl", **times)  # type: ignore[arg-type]
    naive = datetime.datetime(2022, 10, 5, 14, 30)
    with pytest.raises(ValueError, match="no timezone"):
        Reading(source="eia", series="stocks", value=1.0, unit="MMbbl",
                **{**times, field: naive})  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "5.82", None, True])
def test_a_value_that_is_not_a_finite_number_is_not_a_reading(value: object) -> None:
    # A source's "withheld" or "not available" is for the sanitising boundary
    # to deal with (OMNI-32), not to arrive here as a quiet nan.
    with pytest.raises(ValueError, match="finite number"):
        _reading("2022-09-30T00:00:00+00:00", "2022-10-05T14:30:00+00:00", value)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["source", "series", "unit"])
def test_a_reading_names_its_source_its_series_and_its_unit(field: str) -> None:
    fields = {"source": "eia", "series": "price", "unit": "USD/gal", field: " "}
    with pytest.raises(ValueError, match=f"{field} must be a non-empty string"):
        Reading(value=1.0, event_time="2022-09-30T00:00:00+00:00",  # type: ignore[arg-type]
                known_at="2022-10-05T14:30:00+00:00", **fields)  # type: ignore[arg-type]


def test_a_reading_survives_json_with_both_times_and_their_offsets() -> None:
    # What a fixture stores, and what crosses to a sandbox worker.
    reading = _reading("2022-09-30T00:00:00-07:00", "2022-10-05T10:30:00-04:00", 26.4)
    record = json.loads(json.dumps(reading.as_record()))
    assert record["event_time"] == "2022-09-30T00:00:00-07:00"
    assert Reading.from_record(record) == reading


def test_a_record_missing_a_field_is_an_error_never_a_default() -> None:
    record = _TRACE[0].as_record()
    del record["known_at"]
    with pytest.raises(ValueError, match="trace record 3: missing known_at"):
        Reading.from_record(record, where="trace record 3")


# --- the simulated sensor --------------------------------------------------------


def test_the_simulated_sensor_is_a_sensor_and_needs_nothing() -> None:
    assert isinstance(SimSensor(), Sensor)
    assert SimSensor().read(_START, _END, by=TimeAxis.EVENT) == []


def test_the_two_axes_order_the_same_trace_differently() -> None:
    sensor = SimSensor(reversed(_TRACE))
    by_event = [r.series for r in sensor.read(_START, _END, by=TimeAxis.EVENT)]
    by_known = [r.series for r in sensor.read(_START, _END, by=TimeAxis.KNOWN)]
    assert by_event == ["stocks", "price", "stocks", "price"]
    # Monday's price is out before the Friday stocks of the week before it.
    assert by_known == ["price", "stocks", "price", "stocks"]


def test_a_window_is_half_open_and_on_the_axis_that_was_named() -> None:
    sensor = SimSensor(_TRACE)
    monday = datetime.datetime(2022, 10, 3, tzinfo=UTC)
    # By event time, Monday 3 October is in the window that starts on it...
    assert [r.value for r in sensor.read(monday, _END, by=TimeAxis.EVENT)] == [6.38]
    # ...and not in the one that ends on it.
    assert 6.38 not in [r.value for r in sensor.read(_START, monday, by=TimeAxis.EVENT)]
    # What had been published by then leaves out the Friday stocks, out on Wednesday.
    known = sensor.read(_START, monday, by=TimeAxis.KNOWN)
    assert [(r.series, r.value) for r in known] == [("price", 5.82), ("stocks", 27.1)]


def test_a_window_without_a_timezone_is_refused() -> None:
    with pytest.raises(ValueError, match="no timezone"):
        SimSensor(_TRACE).read(datetime.datetime(2022, 9, 1), _END, by=TimeAxis.EVENT)


def test_readings_at_the_same_time_come_in_one_order_however_they_were_given() -> None:
    same = [_reading("2022-10-03T00:00:00+00:00", "2022-10-03T21:00:00+00:00", 1.0, series)
            for series in ("b", "c", "a")]
    forward = SimSensor(same).read(_START, _END, by=TimeAxis.KNOWN)
    backward = SimSensor(reversed(same)).read(_START, _END, by=TimeAxis.KNOWN)
    assert [r.series for r in forward] == [r.series for r in backward] == ["a", "b", "c"]


def test_a_trace_loads_from_plain_records() -> None:
    sensor = SimSensor.from_records(json.loads(json.dumps([r.as_record() for r in _TRACE])))
    assert sensor.read(_START, _END, by=TimeAxis.EVENT) == SimSensor(_TRACE).read(
        _START, _END, by=TimeAxis.EVENT)


# --- replay: event time comes from the trace --------------------------------------


def _replayed(by: TimeAxis) -> list[tuple[Reading, datetime.datetime]]:
    clock = ReplayClock(_START)
    return [(reading, clock.now()) for reading in replay(SimSensor(_TRACE), clock, _START, _END,
                                                         by=by)]


@pytest.mark.parametrize("by", [TimeAxis.EVENT, TimeAxis.KNOWN])
def test_the_same_trace_replayed_twice_gives_the_same_readings_and_times(by: TimeAxis) -> None:
    first, second = _replayed(by), _replayed(by)
    assert first == second and len(first) == len(_TRACE)
    # The clock stood at each reading's own time when it was handed over.
    assert all(now == reading.at(by) for reading, now in first)


def _latest_price_known(sensor: Sensor, clock: ReplayClock) -> float | None:
    """A consumer of the port: the newest price published by the clock's now."""
    known = sensor.read(_START, clock.now(), by=TimeAxis.KNOWN)
    prices = [reading for reading in known if reading.series == "price"]
    return max(prices, key=lambda reading: reading.event_time).value if prices else None


def test_a_consumer_of_the_port_needs_no_network_and_no_wall_clock() -> None:
    sensor = SimSensor(_TRACE)
    clock = ReplayClock.at("2022-09-26T12:00:00+00:00")
    assert _latest_price_known(sensor, clock) is None   # Monday's price is not out yet
    clock.advance_to(datetime.datetime(2022, 9, 27, tzinfo=UTC))
    assert _latest_price_known(sensor, clock) == 5.82
    clock.advance_to(datetime.datetime(2022, 10, 4, tzinfo=UTC))
    assert _latest_price_known(sensor, clock) == 6.38


# --- which sensor: configuration and policy -----------------------------------------


def test_the_default_sensor_is_the_simulated_one_and_needs_no_credentials() -> None:
    assert config.get("sensor.backend") == "sim"
    assert isinstance(make_sensor(), SimSensor)


def test_the_real_sensor_is_refused_until_it_exists() -> None:
    with pytest.raises(RuntimeError, match="OMNI-33"):
        make_sensor("real")
    with pytest.raises(ValueError, match="not a sensor backend"):
        make_sensor("csv")


def test_the_loop_may_not_choose_its_own_sensor() -> None:
    # Real data is the evidence a promotion needs (D4): a loop that could switch
    # to the simulator would be choosing its own exam.
    key = config.key_for("sensor.backend")
    assert key.tier is config.ConfigTier.FORBIDDEN
    assert key.choices == ("sim", "real")


@pytest.mark.parametrize("path", ["sis/ports.py", "sis/adapters.py", "sis/adapters_real.py",
                                  "sis/clock.py"])
def test_sensor_code_is_never_an_optimisation_target(
    path: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not even when someone names it as one: guardrail classification wins.
    monkeypatch.setenv("SIS_TARGET_PATHS", path)
    config.reset_config_cache()
    try:
        assert policy.classify(path) is policy.ChangeTier.FORBIDDEN
        assert not policy.authorize_change(path, checks_passed=True).allowed
    finally:
        monkeypatch.undo()
        config.reset_config_cache()
