"""Tests for generated fuel-market scenarios (OMNI-34).

What the story promises, each pinned here: a seeded scenario reproduces byte
for byte; it drives a twin through the same sensor, replay and as-of view as a
recorded trace; its data events exercise the replay's gaps, revisions,
withdrawals and mixed anchors; it reaches the invariant gate as an input; it is
refused as a backtest's evidence (D4); and the pinned generator is FORBIDDEN
while its working copy is SOFT (D7).
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
import json
import subprocess
import sys
import types
from dataclasses import replace
from typing import Any

import pytest
from hypothesis import given, settings

from sis import gauntlet, scenarios
from sis.adapters import SimSensor, replay
from sis.adapters_real import EIA_SERIES, eia_published
from sis.backtest import FIXTURE_SCHEMA, parse_fixture
from sis.clock import ReplayClock
from sis.contract import ROMAN
from sis.episodic import gate_from_reason
from sis.fixture_builder import FixtureRefused, Question, ReleaseCalendar, build
from sis.invariant import Invariant
from sis.paths import INVARIANTS_PATH, PROJECT_ROOT, SCENARIOS_PATH
from sis.policy import ChangeTier, classify
from sis.ports import Reading, TimeAxis
from sis.replay import as_of
from sis.sensor_input import SIMULATED_SOURCE, SeriesRule, SourceRules
from tests.test_invariant import ROMAN_OK

GEN = scenarios.pinned()
EVERY_EVENT = tuple(GEN.DATA_EVENTS)
ALL_TIME = (datetime.datetime(1900, 1, 1, tzinfo=datetime.UTC),
            datetime.datetime(2200, 1, 1, tzinfo=datetime.UTC))


def _shared() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("invariants_under_test", INVARIANTS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _series(scenario: Any, series: str) -> list[float]:
    """First-vintage values of *series*, in week order."""
    first: dict[str, float] = {}
    for record in scenario.readings:
        if record["series"] == series:
            first.setdefault(record["event_time"], record["value"])
    return [first[key] for key in sorted(first)]


# --- reproducible ---------------------------------------------------------


@pytest.mark.parametrize("kind", GEN.KINDS)
def test_a_seeded_scenario_reproduces_byte_for_byte(kind: str) -> None:
    one = GEN.generate(kind, seed=11, data_events=EVERY_EVENT)
    two = GEN.generate(kind, seed=11, data_events=EVERY_EVENT)
    assert one.to_json() == two.to_json()
    assert GEN.generate(kind, seed=12).to_json() != GEN.generate(kind, seed=11).to_json()


def test_a_scenario_reproduces_in_another_process_with_another_hash_seed() -> None:
    code = ("import hashlib, importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('s', {str(SCENARIOS_PATH)!r})\n"
            "m = importlib.util.module_from_spec(spec); sys.modules['s'] = m\n"
            "spec.loader.exec_module(m)\n"
            "print(hashlib.sha256(m.generate('supply_shock', seed=5, "
            "data_events=m.DATA_EVENTS).to_json().encode()).hexdigest())\n")
    here = hashlib.sha256(GEN.generate("supply_shock", seed=5, data_events=EVERY_EVENT)
                          .to_json().encode()).hexdigest()
    for hash_seed in ("0", "12345"):
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                             check=True, env={"PYTHONHASHSEED": hash_seed})
        assert out.stdout.strip() == here


# --- the kinds have the shapes they are named for ------------------------


def test_a_supply_shock_draws_stocks_down_and_the_california_premium_up() -> None:
    for seed in range(20):
        scenario = GEN.generate("supply_shock", seed=seed)
        stocks = _series(scenario, GEN.STOCKS)
        premium = [ca - us for ca, us in zip(_series(scenario, GEN.CALIFORNIA),
                                             _series(scenario, GEN.US), strict=True)]
        low = stocks.index(min(stocks))
        assert min(stocks) < stocks[0] * (1 - scenario.magnitude / 2)
        assert premium[low] > premium[0] + 0.1


def test_an_outage_drops_one_week_then_overshoots() -> None:
    for seed in range(20):
        scenario = GEN.generate("outage", seed=seed)
        stocks = _series(scenario, GEN.STOCKS)
        drop = stocks.index(min(stocks))
        assert stocks[drop] < stocks[0] * (1 - scenario.magnitude / 2)
        assert stocks[drop + 1] > stocks[0]


def test_a_national_move_moves_the_us_price_and_not_regional_stocks() -> None:
    for seed in range(20):
        scenario = GEN.generate("national_move", seed=seed)
        stocks, us = _series(scenario, GEN.STOCKS), _series(scenario, GEN.US)
        assert abs(us[-1] - us[0]) > scenario.magnitude / 2
        assert max(stocks) / min(stocks) < 1.05


def test_a_demand_shock_builds_stocks_and_lowers_prices() -> None:
    for seed in range(20):
        scenario = GEN.generate("demand_shock", seed=seed)
        stocks, us = _series(scenario, GEN.STOCKS), _series(scenario, GEN.US)
        assert stocks[-1] > stocks[0] * (1 + scenario.magnitude / 2)
        assert us[-1] < us[0]


# --- inside what the real series have shown ------------------------------


@pytest.mark.parametrize("kind", GEN.KINDS)
def test_values_stay_inside_the_observed_levels_and_the_sensors_ranges(kind: str) -> None:
    bounds = {GEN.STOCKS: GEN.OBSERVED["stocks"]}
    for seed in range(30):
        scenario = GEN.generate(kind, seed=seed, data_events=EVERY_EVENT)
        for record in scenario.readings:
            rule = EIA_SERIES[record["series"]].rule
            assert record["unit"] == rule.unit
            assert rule.minimum <= record["value"] <= rule.maximum
            low, high = bounds.get(record["series"], (0.0, float("inf")))
            assert low * 0.99 <= record["value"] <= high * 1.01
        assert not scenario.stress


def test_a_magnitude_outside_what_was_observed_is_refused_unless_a_stress_test() -> None:
    with pytest.raises(ValueError, match="stress=True"):
        GEN.generate("outage", seed=1, magnitude=0.6)
    stress = GEN.generate("outage", seed=1, magnitude=0.6, stress=True)
    assert stress.stress
    assert '"stress":true' in stress.to_json()


@pytest.mark.parametrize(("kwargs", "match"), [
    ({"kind": "meteor"}, "not a scenario kind"),
    ({"kind": "outage", "data_events": ["flood"]}, "not a data event"),
    ({"kind": "outage", "weeks": 3}, "at least 6 weeks"),
    ({"kind": "outage", "start": datetime.date(2019, 1, 8)}, "Monday"),
])
def test_bad_arguments_are_refused(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        GEN.generate(seed=1, **kwargs)


def test_every_reading_is_known_when_the_eia_sensor_says_its_kind_is() -> None:
    scenario = GEN.generate("supply_shock", seed=3)
    for record in scenario.readings:
        reading = Reading.from_record(record)
        assert reading.known_at == eia_published(reading.series, reading.event_time)


def test_stocks_and_prices_keep_their_own_weekdays() -> None:
    scenario = GEN.generate("national_move", seed=4)
    for record in scenario.readings:
        weekday = datetime.datetime.fromisoformat(record["event_time"]).weekday()
        assert weekday == (4 if record["series"] == GEN.STOCKS else 0)


# --- data events through the replay (OMNI-35's as_of) --------------------


def _event_targets(generated: scenarios.Generated) -> dict[str, tuple[str, datetime.datetime]]:
    """Which (series, period) each data event landed on."""
    out: dict[str, tuple[str, datetime.datetime]] = {}
    for absent in generated.absent:
        out["missing_week" if absent.marker == "NA" else "withdrawn"] = (absent.series,
                                                                         absent.event_time)
    periods: dict[tuple[str, datetime.datetime], int] = {}
    for reading in generated.readings:
        periods[(reading.series, reading.event_time)] = periods.get(
            (reading.series, reading.event_time), 0) + 1
    revised = [key for key, count in periods.items() if count == 2]
    assert len(revised) == 1
    out["republished"] = revised[0]
    return out


@pytest.mark.parametrize("seed", range(10))
def test_data_events_reach_the_as_of_view_as_a_recorded_trace_would(seed: int) -> None:
    generated = scenarios.generate("supply_shock", seed=seed, data_events=EVERY_EVENT)
    targets = _event_targets(generated)
    readings, absent = generated.readings, generated.absent

    def shown(at: datetime.datetime, key: tuple[str, datetime.datetime]) -> list[Any]:
        view = as_of(readings, absent, at)
        return [item for item in (*view.readings, *view.absent)
                if (item.series, item.event_time) == key]

    # A missing week: stated absent, never a value, and never zero.
    key = targets["missing_week"]
    stated = next(a for a in absent if (a.series, a.event_time) == key)
    assert [type(item).__name__ for item in shown(stated.known_at, key)] == ["Absent"]
    assert not any((r.series, r.event_time) == key for r in readings)

    # A re-publication replaces the first vintage only from when it was known.
    key = targets["republished"]
    first, second = sorted((r for r in readings if (r.series, r.event_time) == key),
                           key=lambda r: r.known_at)
    assert second.known_at > first.known_at and second.value != first.value
    assert shown(second.known_at - datetime.timedelta(seconds=1), key) == [first]
    assert shown(second.known_at, key) == [second]

    # A withdrawal: the value is seen until it is withdrawn, then not.
    key = targets["withdrawn"]
    value = next(r for r in readings if (r.series, r.event_time) == key)
    withdrawal = next(a for a in absent if (a.series, a.event_time) == key)
    assert shown(withdrawal.known_at - datetime.timedelta(seconds=1), key) == [value]
    assert shown(withdrawal.known_at, key) == [withdrawal]


@pytest.mark.parametrize("kind", GEN.KINDS)
def test_the_pinned_state_is_what_the_replay_shows(kind: str) -> None:
    """``state_at`` restates ``sis.replay.as_of`` for the sandbox; they must agree."""
    for seed in range(5):
        generated = scenarios.generate(kind, seed=seed, data_events=EVERY_EVENT)
        for at in generated.decision_times():
            view = as_of(generated.readings, generated.absent, at)
            state = GEN.state_at(generated.scenario, at)
            assert state["readings"] == [r.as_record() for r in view.readings]
            assert [(a["series"], a["event_time"], a["marker"]) for a in state["absent"]] == [
                (a.series, a.event_time.isoformat(), a.marker) for a in view.absent]


# --- driven like a recorded trace ----------------------------------------


def test_a_scenario_sensor_reads_as_the_same_records_replayed_from_a_trace() -> None:
    generated = scenarios.generate("outage", seed=8, data_events=EVERY_EVENT)
    from_trace = SimSensor.from_records(generated.scenario.readings)
    for axis in TimeAxis:
        assert generated.sensor().read(*ALL_TIME, by=axis) == from_trace.read(*ALL_TIME, by=axis)


def test_a_scenario_replays_on_known_at_and_moves_the_clock() -> None:
    generated = scenarios.generate("demand_shock", seed=2)
    clock = ReplayClock(datetime.datetime(2018, 1, 1, tzinfo=datetime.UTC))
    seen = list(replay(generated.sensor(), clock, *ALL_TIME, by=TimeAxis.KNOWN))
    assert [r.known_at for r in seen] == sorted(r.known_at for r in seen)
    assert clock.now() == seen[-1].known_at
    assert all(r.source == SIMULATED_SOURCE for r in seen)


def test_a_scenario_sensor_states_its_absences() -> None:
    generated = scenarios.generate("national_move", seed=6, data_events=("missing_week",))
    absent = generated.sensor().absent(*ALL_TIME, by=TimeAxis.KNOWN)
    assert [a.marker for a in absent] == ["NA"]


# --- the gauntlet's input generator --------------------------------------


@settings(max_examples=40, deadline=None)
@given(_shared().fuel_scenario_states())
def test_the_strategy_draws_a_state_shaped_as_a_backtest_argument(args: tuple[Any, ...]) -> None:
    (state,) = args
    assert set(state) == {"as_of", "readings", "absent"}
    at = datetime.datetime.fromisoformat(state["as_of"])
    for record in (*state["readings"], *state["absent"]):
        assert record["source"] == SIMULATED_SOURCE
        assert datetime.datetime.fromisoformat(record["known_at"]) <= at
    json.dumps(state)  # crosses to a sandbox worker as JSON


def test_the_invariant_gate_draws_scenarios_inside_the_exam() -> None:
    """The strategy resolves and generates in the gate's exam directory.

    ``to_roman`` cannot take a state, so the law is broken by the candidate's
    own exception: a counted violation, which only a generated input reaches.
    A missing ``scenarios.py`` would be a ``harness:`` strategy error instead.
    """
    contract = replace(ROMAN, invariants=(
        Invariant(name="scenario", strategy="fuel_scenario_states", check="non_negative"),))
    result = gauntlet.validate(ROMAN_OK, contract=contract, seed=3)
    assert not result.passed
    assert gate_from_reason(result.reason) != "harness", result.reason
    assert result.reason.startswith("invariant violated"), result.reason


# --- D4: refutation, never acceptance ------------------------------------


def test_a_backtest_fixture_holding_a_scenario_is_refused() -> None:
    scenario = GEN.generate("supply_shock", seed=1)
    state = GEN.state_at(scenario, GEN.decision_times(scenario)[5])
    raw = json.dumps({"schema": FIXTURE_SCHEMA, "args": [state]})
    with pytest.raises(ValueError, match="never be the evidence"):
        parse_fixture(raw, where="sim.json")
    real = json.loads(raw.replace(f'"source": "{SIMULATED_SOURCE}"', '"source": "eia"'))
    assert parse_fixture(json.dumps(real), where="eia.json").args == real["args"]


def test_the_fixture_builder_refuses_a_simulated_source() -> None:
    rules = SourceRules(SIMULATED_SOURCE, {GEN.CALIFORNIA: SeriesRule("Dollars per Gallon",
                                                                        0.5, 15.0)})
    calendar = ReleaseCalendar.parse(json.dumps({
        "schema": 1, "timezone": "America/New_York", "checked_by": "a human",
        "checked_on": "2026-10-25", "releases": {}}))
    question = Question(target=GEN.CALIFORNIA, first=datetime.date(2019, 2, 4),
                        last=datetime.date(2019, 3, 4))
    with pytest.raises(FixtureRefused, match="generated scenario"):
        build([], rules, calendar, question, name="sim", spec_ref="none")


def test_the_engine_refuses_a_generator_writing_another_source(monkeypatch: Any,
                                                               tmp_path: Any) -> None:
    forged = tmp_path / "scenarios.py"
    forged.write_text(SCENARIOS_PATH.read_text(encoding="utf-8").replace(
        'SOURCE = "sim"', 'SOURCE = "eia"'), encoding="utf-8")
    monkeypatch.setattr(scenarios, "SCENARIOS_PATH", forged)
    monkeypatch.delitem(sys.modules, "_pinned_scenarios", raising=False)
    with pytest.raises(ImportError, match="could not tell it from real data"):
        scenarios.pinned()
    monkeypatch.delitem(sys.modules, "_pinned_scenarios", raising=False)


# --- D7: pinned and working copies ---------------------------------------


def test_the_pinned_generator_and_its_loader_are_forbidden() -> None:
    assert classify("specs/scenarios.py") is ChangeTier.FORBIDDEN
    assert classify("sis/scenarios.py") is ChangeTier.FORBIDDEN


def test_the_working_copy_is_soft_and_nothing_judges_with_it() -> None:
    assert classify("runtime/scenarios.py") is ChangeTier.SOFT
    assert (PROJECT_ROOT / "runtime" / "scenarios.py").exists()
    assert SCENARIOS_PATH == PROJECT_ROOT / "specs" / "scenarios.py"
    assert scenarios.pinned().__file__ == str(SCENARIOS_PATH)
