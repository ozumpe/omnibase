"""sis.events — a coarse event signal for price shocks the inputs cannot explain (OMNI-227).

California's gasoline price jumps when a refinery there goes down, and no
amount of tuning on stocks and prices sees that coming. This module turns a
free public source into a weekly 0/1 series a model, or a human reading the
futility page (OMNI-228), can line up with its largest errors.

**The source** is EIA's weekly West Coast (PADD 5) refinery utilisation
(``PET.W_NA_YUP_R50_PER.W``): the share of operable capacity the region's
refineries ran at, in the same bulk file and the same Wednesday release as the
stocks the EIA sensor already captures. It is public domain (EIA's
``copyright: None``), free, has history from 2010, and costs no extra
download. EIA's "This Week in Petroleum" commentary, the other free candidate,
stopped in October 2025.

**The event**: a week whose utilisation fell by at least
:data:`OUTAGE_DROP_POINTS` percentage points from the week before is flagged
1 (an unplanned outage or a large turnaround), any other week 0. A week with
no value, or no week seven days before it, gets no flag. The threshold is a
constant, not a config key: changing it rewrites history, and every week it
touches would come back as a revision.

**Honest times.** ``event_time`` is the week's end, as the utilisation's. On
the first capture (backfill) a flag is known when the later of its two
utilisation values became known, as the EIA trace records it; after that, a
new or changed flag is known at the capture that found it, which is after
both. So a replay never shows a flag before the values it was made from.

**The door.** Flags enter through :func:`sis.sensor_input.sanitise` like
every reading, under :data:`EVENT_RULES` (unit ``flag``, range [0, 1]), into
their own append-only trace (``runtime/sensor_traces/events.jsonl``) by the
same machinery as the EIA trace (:mod:`sis.sensor_trace`). No text from any
source reaches a prompt: the signal is a number.

**Where it is shown**: only in a training split built with
``scripts/build_fixture.py --training --events`` (OMNI-227 first slice). No
gate reads it, and no held-out episode shows it.

Deriving is pure (:func:`outages`); :class:`EventSensor` reads the EIA trace,
appends to its own, and reads it back.
"""

from __future__ import annotations

import datetime
import pathlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sis import sensor_trace
from sis.adapters import SimSensor
from sis.clock import Clock, WallClock
from sis.paths import SENSOR_TRACES_DIR
from sis.ports import Reading, TimeAxis
from sis.sensor_input import Absent, SeriesRule, SourceRules

# The EIA series the events are made from.
PADD5_UTILISATION = "PET.W_NA_YUP_R50_PER.W"

# The event series, and the rules its trace is read back under.
REFINERY_OUTAGE = "events.padd5_refinery_outage.W"
EVENT_RULES = SourceRules("events", {
    REFINERY_OUTAGE: SeriesRule("flag", 0.0, 1.0, period=datetime.timedelta(days=7)),
})
EVENTS_TRACE = SENSOR_TRACES_DIR / "events.jsonl"

# A fall of at least this many percentage points in a week is an outage. From
# 2018 to 2026 it flags about five weeks a year, among them the September 2019
# drops that came with that autumn's California price spike.
OUTAGE_DROP_POINTS = 4.0

_WEEK = datetime.timedelta(days=7)


@dataclass(frozen=True)
class Flag:
    """One week's event: its week end, 0 or 1, and when both values it rests on were known."""

    week: datetime.datetime
    value: float
    known_at: datetime.datetime


def _latest(readings: Iterable[Reading], series: str) -> dict[datetime.datetime, Reading]:
    """Each period's latest vintage of *series*."""
    out: dict[datetime.datetime, Reading] = {}
    for reading in readings:
        if reading.series != series:
            continue
        held = out.get(reading.event_time)
        if held is None or reading.known_at > held.known_at:
            out[reading.event_time] = reading
    return out


def outages(readings: Iterable[Reading], absent: Iterable[Absent] = (), *,
            drop: float = OUTAGE_DROP_POINTS) -> list[Flag]:
    """The refinery-outage flag for every week with a value and one a week before. Pure.

    From the latest vintage of each week's utilisation; a week whose latest
    word is an absence (EIA blanked it) has no value, so no flag.
    """
    latest = _latest(readings, PADD5_UTILISATION)
    for gone in absent:
        held = latest.get(gone.event_time)
        if gone.series == PADD5_UTILISATION and held is not None \
                and gone.known_at > held.known_at:
            del latest[gone.event_time]
    flags = []
    for week in sorted(latest):
        before = latest.get(week - _WEEK)
        if before is None:
            continue
        now = latest[week]
        fell = before.value - now.value
        flags.append(Flag(week, 1.0 if fell >= drop else 0.0,
                          max(now.known_at, before.known_at)))
    return flags


def fetched(flags: Iterable[Flag]) -> sensor_trace.Fetched:
    """The flags as one fetch of the event series, for :func:`sensor_trace.capture_lines`."""
    return sensor_trace.Fetched(REFINERY_OUTAGE, EVENT_RULES.series[REFINERY_OUTAGE].unit,
                                tuple((flag.week, flag.value) for flag in flags))


class EventSensor:
    """The event series: derived from the EIA trace, captured into its own, read from it."""

    def __init__(self, trace: pathlib.Path = EVENTS_TRACE, *,
                 eia_trace: pathlib.Path | None = None, clock: Clock | None = None) -> None:
        from sis.adapters_real import EIA_TRACE

        self.trace = sensor_trace.writable(trace)
        self.eia_trace = eia_trace if eia_trace is not None else EIA_TRACE
        self._clock = clock or WallClock()

    def capture(self) -> sensor_trace.CaptureReport:
        """Derive the flags from the EIA trace, and append what is new or changed."""
        from sis.adapters_real import EIA_RULES

        lines = sensor_trace.load(self.eia_trace)
        readings, absent = sensor_trace.read_back(lines, EIA_RULES)
        flags = outages(readings, absent)
        known: Mapping[datetime.datetime, datetime.datetime] = {
            flag.week: flag.known_at for flag in flags}
        eia_last = next((line for line in reversed(lines) if line["kind"] == "capture"), None)

        def published(series: str, period: datetime.datetime) -> datetime.datetime:
            # Backfill only: when both values the flag rests on were known.
            return known[period]

        def block(prior: list[dict[str, object]]) -> list[dict[str, object]]:
            fetched_at = self._clock.now()
            stamp = fetched_at.astimezone(datetime.UTC)
            provenance = {"derived_from": PADD5_UTILISATION, "rule": f"a fall of at least "
                          f"{OUTAGE_DROP_POINTS:g} points in a week",
                          "eia_capture": None if eia_last is None else eia_last["capture"]}
            return sensor_trace.capture_lines(
                prior, [fetched(flags)], EVENT_RULES,
                capture=f"events-{stamp:%Y%m%dT%H%M%S.%fZ}", fetched_at=fetched_at,
                published=published, provenance=provenance)

        return sensor_trace.CaptureReport.of(sensor_trace.append(self.trace, block))

    def read(self, start: datetime.datetime, end: datetime.datetime, *,
             by: TimeAxis) -> list[Reading]:
        readings, _ = sensor_trace.read_back(sensor_trace.load(self.trace), EVENT_RULES)
        return SimSensor(readings).read(start, end, by=by)


__all__ = ["EVENTS_TRACE", "EVENT_RULES", "OUTAGE_DROP_POINTS", "PADD5_UTILISATION",
           "REFINERY_OUTAGE", "EventSensor", "Flag", "fetched", "outages"]
