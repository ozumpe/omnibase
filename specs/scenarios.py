"""Fuel-market scenarios — seeded, generated episodes of the weekly series (OMNI-34).

Drives a twin into situations that have never happened: a regional supply
shock, a sudden outage, a national price move, a demand shock, and the data
events a real feed has (a missing week, a value re-published later, a value
withdrawn). Every scenario is a pure function of its arguments and its seed,
so a failure it finds can be reproduced byte for byte.

**This file exists twice, and the split is D7's** (docs/OMNITRACK_VISION.md):

- ``specs/scenarios.py`` is the **pinned** copy. It generates the gauntlet's
  inputs (``specs/invariants.py`` loads it, and the invariant gate copies it
  into the exam) and drives a twin through ``sis.scenarios``. POLICY-FORBIDDEN,
  as all of ``specs/`` is: it changes only through a human.
- ``runtime/scenarios.py`` is the **working** copy, a SOFT target the loop may
  improve. Nothing reads it to judge anything. An improvement reaches the
  pinned copy only when a human copies it there, after judging it against
  held-out *real* traces, never against its own output.

Without the split the loop would generate the inputs it is graded on.

**Simulation is for refutation, real data is for acceptance** (D4). Every
reading here has ``source`` ``"sim"``, which ``sis.backtest.parse_fixture`` and
``sis.fixture_builder.build`` refuse: a scenario may reject a candidate through
the invariant gate, and it can never be a backtest's evidence for promoting one.

**Values are generated, never copied** from a recorded trace: a scenario that
embedded a held-out week would leak the exam. Magnitudes stay inside the ranges
in ``OBSERVED`` unless the caller asks for a stress test, which says so
(``Scenario.stress``) and is never drawn by the gauntlet's strategy.

**Runs inside the gauntlet sandbox**, next to untrusted candidate code: the
standard library only, nothing from ``sis/``. Records are plain JSON data in
the shapes ``sis.ports.Reading.as_record`` and the fixture builder's absences
use, so a twin reads a scenario exactly as it reads a recorded trace.
"""

from __future__ import annotations

import datetime
import json
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

SOURCE = "sim"
EASTERN = ZoneInfo("America/New_York")

# The series a scenario writes, by EIA's own ids so a twin reads the same
# names it reads from a capture. The source ("sim") is what tells them apart.
STOCKS = "PET.WGTSTP51.W"                  # West Coast (PADD 5) total gasoline stocks
CALIFORNIA = "PET.EMM_EPMRR_PTE_SCA_DPG.W"  # California regular reformulated, retail
US = "PET.EMM_EPMRR_PTE_NUS_DPG.W"          # United States regular, retail
UNITS = {STOCKS: "Thousand Barrels", CALIFORNIA: "Dollars per Gallon", US: "Dollars per Gallon"}

# When a value counts as known, as the EIA sensor reconstructs it for backfilled
# history (sis.adapters_real.EIA_SERIES): Friday stocks on the next Thursday at
# 5 p.m. Eastern, Monday prices on the Wednesday at 5 p.m. Stocks and prices are
# anchored on different weekdays and are not aligned here: that is a model's
# choice, as it is for a recorded trace (sis.replay).
RELEASE = {STOCKS: (6, datetime.time(17)), CALIFORNIA: (2, datetime.time(17)),
           US: (2, datetime.time(17))}

KINDS = ("supply_shock", "outage", "national_move", "demand_shock")
DATA_EVENTS = ("missing_week", "republished", "withdrawn")

# What the real series have shown, approximately (EIA weekly data since 2010).
# Levels bound every generated value; a kind's magnitude is the size of its
# shock, drawn from (and, unless ``stress``, checked against) its range:
#   supply_shock  fraction by which stocks fall (autumn 2022: about a tenth)
#   outage        fraction stocks drop in one week (the Lower Atlantic, May 2021)
#   national_move $/gal the US price moves, either way (spring 2022: about +$1)
#   demand_shock  fraction stocks build as consumption falls (spring 2020)
OBSERVED: Mapping[str, tuple[float, float]] = {
    "stocks": (24_000.0, 36_000.0),
    "us": (1.70, 5.10),
    "premium": (0.20, 2.60),   # California over the US
    "supply_shock": (0.04, 0.15),
    "outage": (0.05, 0.25),
    "national_move": (0.20, 1.00),
    "demand_shock": (0.04, 0.15),
}

DEFAULT_START = datetime.date(2019, 1, 7)  # a Monday
DEFAULT_WEEKS = 16


@dataclass(frozen=True)
class Scenario:
    """One generated episode: its parameters, and every vintage it publishes."""

    kind: str
    seed: int
    weeks: int
    start: str
    magnitude: float
    stress: bool
    data_events: tuple[str, ...]
    readings: tuple[dict[str, Any], ...]
    absent: tuple[dict[str, Any], ...]

    def to_json(self) -> str:
        """The scenario as canonical JSON: the same arguments give the same bytes."""
        return json.dumps({
            "kind": self.kind, "seed": self.seed, "weeks": self.weeks, "start": self.start,
            "magnitude": self.magnitude, "stress": self.stress,
            "data_events": list(self.data_events), "readings": list(self.readings),
            "absent": list(self.absent)}, sort_keys=True, separators=(",", ":"))


def _midnight(day: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(0), tzinfo=EASTERN)


def _known(series: str, period: datetime.date) -> datetime.datetime:
    days, at = RELEASE[series]
    return datetime.datetime.combine(period + datetime.timedelta(days=days), at, tzinfo=EASTERN)


def _ramp(week: int, onset: int, length: int) -> float:
    """0 before *onset*, rising linearly to 1 over *length* weeks, then 1."""
    if week < onset:
        return 0.0
    return min(1.0, (week - onset + 1) / length)


def _clamp(value: float, bounds: tuple[float, float], stress: bool) -> float:
    return value if stress else min(max(value, bounds[0]), bounds[1])


def _shape(kind: str, rng: random.Random, weeks: int, magnitude: float,
           ) -> list[tuple[float, float, float]]:
    """Per week, the multiplier on stocks, the $ added to the premium and to the US price."""
    onset = rng.randint(2, max(2, weeks // 3))
    out: list[tuple[float, float, float]] = []
    if kind == "supply_shock":
        fall, rebuild = rng.randint(3, 6), rng.randint(3, 6)
        sensitivity = rng.uniform(5.0, 8.0)  # $/gal of premium per unit of stock deficit
        for week in range(weeks):
            down = _ramp(week, onset, fall)
            up = _ramp(week, onset + fall, rebuild)
            deficit = magnitude * (down - up)
            out.append((1.0 - deficit, sensitivity * deficit, 0.0))
    elif kind == "outage":
        overshoot = rng.uniform(0.2, 0.5) * magnitude
        spike = rng.uniform(3.0, 5.0) * magnitude
        for week in range(weeks):
            if week == onset:
                out.append((1.0 - magnitude, spike, 0.0))
            elif week > onset:
                decay = 0.5 ** (week - onset - 1)
                out.append((1.0 + overshoot * decay, spike * 0.3 * decay, 0.0))
            else:
                out.append((1.0, 0.0, 0.0))
    elif kind == "national_move":
        length = rng.randint(2, 6)
        move = magnitude if rng.random() < 0.5 else -magnitude
        for week in range(weeks):
            out.append((1.0, 0.0, move * _ramp(week, onset, length)))
    elif kind == "demand_shock":
        length = rng.randint(2, 5)
        price_fall = rng.uniform(2.0, 4.0) * magnitude
        for week in range(weeks):
            ramp = _ramp(week, onset, length)
            out.append((1.0 + magnitude * ramp, -price_fall * 0.5 * ramp, -price_fall * ramp))
    else:
        raise ValueError(f"{kind!r} is not a scenario kind ({', '.join(KINDS)})")
    return out


def generate(kind: str, *, seed: int, weeks: int = DEFAULT_WEEKS,
             start: datetime.date = DEFAULT_START, magnitude: float | None = None,
             data_events: Iterable[str] = (), stress: bool = False) -> Scenario:
    """The scenario *kind* with *seed*: see the module docstring.

    *magnitude* is drawn from ``OBSERVED[kind]`` when not given; one given
    outside that range is refused unless *stress* is set, and a stress
    scenario is not clamped to the observed levels either.
    """
    if kind not in KINDS:
        raise ValueError(f"{kind!r} is not a scenario kind ({', '.join(KINDS)})")
    events = tuple(dict.fromkeys(data_events))
    unknown = [event for event in events if event not in DATA_EVENTS]
    if unknown:
        raise ValueError(f"{unknown[0]!r} is not a data event ({', '.join(DATA_EVENTS)})")
    if weeks < 6:
        raise ValueError(f"a scenario needs at least 6 weeks, got {weeks}")
    if start.weekday() != 0:
        raise ValueError(f"a scenario starts on a Monday, {start.isoformat()} is not one")
    rng = random.Random(f"{kind}:{seed}")
    low, high = OBSERVED[kind]
    if magnitude is None:
        magnitude = rng.uniform(low, high)
    elif not stress and not low <= magnitude <= high:
        raise ValueError(f"{kind} magnitude {magnitude} is outside what the real series "
                         f"have shown [{low}, {high}]; pass stress=True for a stress test")
    stocks0 = rng.uniform(27_000.0, 32_000.0)
    us0 = rng.uniform(2.40, 4.00)
    premium0 = rng.uniform(0.50, 1.30)

    values: dict[tuple[str, datetime.date], float] = {}
    for week, (stock_factor, premium_add, us_add) in enumerate(
            _shape(kind, rng, weeks, magnitude)):
        monday = start + datetime.timedelta(weeks=week)
        friday = monday + datetime.timedelta(days=4)
        stocks = _clamp(stocks0 * stock_factor * (1 + rng.gauss(0, 0.005)),
                        OBSERVED["stocks"], stress)
        us = _clamp(us0 + us_add + rng.gauss(0, 0.01), OBSERVED["us"], stress)
        premium = _clamp(premium0 + premium_add + rng.gauss(0, 0.01), OBSERVED["premium"],
                         stress)
        values[(STOCKS, friday)] = float(round(stocks))
        values[(US, monday)] = round(us, 3)
        values[(CALIFORNIA, monday)] = round(us + premium, 3)

    readings: list[dict[str, Any]] = []
    absent: list[dict[str, Any]] = []

    def reading(series: str, period: datetime.date, value: float,
                known: datetime.datetime) -> dict[str, Any]:
        return {"source": SOURCE, "series": series, "value": value, "unit": UNITS[series],
                "event_time": _midnight(period).isoformat(), "known_at": known.isoformat()}

    def stated_absent(series: str, period: datetime.date, known: datetime.datetime,
                      marker: str) -> dict[str, Any]:
        return {"source": SOURCE, "series": series, "marker": marker,
                "event_time": _midnight(period).isoformat(), "known_at": known.isoformat()}

    # Each data event lands on its own (series, period), never the first week:
    # two vintages of one period known at one instant would have no order.
    later_weeks = start + datetime.timedelta(weeks=1)
    targets = rng.sample(sorted(key for key in values if key[1] >= later_weeks), len(events))
    hit = dict(zip(targets, events, strict=True))
    for (series, period), value in values.items():
        known = _known(series, period)
        event = hit.get((series, period))
        if event == "missing_week":
            absent.append(stated_absent(series, period, known, "NA"))
            continue
        readings.append(reading(series, period, value, known))
        later = known + datetime.timedelta(weeks=1)
        if event == "republished":
            step = rng.uniform(0.003, 0.01) if series == STOCKS else rng.uniform(0.005, 0.03)
            revised = value * (1 + step) if series == STOCKS else value + step
            if rng.random() < 0.5:
                revised = value * (1 - step) if series == STOCKS else value - step
            revised = float(round(revised)) if series == STOCKS else round(revised, 3)
            readings.append(reading(series, period, revised, later))
        elif event == "withdrawn":
            absent.append(stated_absent(series, period, later, "W"))

    def order(record: Mapping[str, Any]) -> tuple[str, str, str]:
        return (record["known_at"], record["event_time"], record["series"])

    return Scenario(kind=kind, seed=seed, weeks=weeks, start=start.isoformat(),
                    magnitude=magnitude, stress=stress, data_events=events,
                    readings=tuple(sorted(readings, key=order)),
                    absent=tuple(sorted(absent, key=order)))


def state_at(scenario: Scenario, at: datetime.datetime) -> dict[str, Any]:
    """What a model is shown at *at*, in the shape a backtest fixture's argument has.

    The rule ``sis.replay.as_of`` applies to a recorded trace, restated here
    because this file may import nothing from ``sis/`` (a test holds the two to
    the same answer): only what is known by *at*, and per period its latest
    vintage, a reading or a stated absence.
    """
    latest: dict[tuple[str, str, str], tuple[datetime.datetime, dict[str, Any]]] = {}
    for record in (*scenario.readings, *scenario.absent):
        known = datetime.datetime.fromisoformat(record["known_at"])
        if known > at:
            continue
        key = (record["source"], record["series"], record["event_time"])
        held = latest.get(key)
        if held is None or known > held[0]:
            latest[key] = (known, record)

    def order(record: Mapping[str, Any]) -> tuple[datetime.datetime, str, str]:
        return (datetime.datetime.fromisoformat(record["event_time"]), record["source"],
                record["series"])

    seen = sorted((record for _, record in latest.values()), key=order)
    return {"as_of": at.isoformat(),
            "readings": [record for record in seen if "value" in record],
            "absent": [record for record in seen if "marker" in record]}


def decision_times(scenario: Scenario, series: str = CALIFORNIA) -> list[datetime.datetime]:
    """When each week's first vintage of *series* became known: the moments a question is asked."""
    first: dict[str, datetime.datetime] = {}
    for record in scenario.readings:
        if record["series"] == series:
            known = datetime.datetime.fromisoformat(record["known_at"])
            first[record["event_time"]] = min(known, first.get(record["event_time"], known))
    return sorted(first.values())
