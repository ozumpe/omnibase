"""sis.replay — what a model may see at a moment of a replay (OMNI-35).

**A replay runs on ``known_at``**, the order in which the world revealed itself,
and ``event_time`` is data. Decided with OMNI-35, before the first fixture was
recorded, because a fixture recorded unreplayably cannot be recorded again:

- At decision time ``t`` a model sees exactly the readings with
  ``known_at <= t``. For each (source, series, period) it sees the **latest
  vintage as of t**: a revision replaces what it revises from the moment it
  became known, never earlier. A value the source later stated no value for
  (a withdrawal, an :class:`~sis.sensor_input.Absent`) is not seen after the
  withdrawal, and not before it either if the absence came first.
- **Mixed anchors are not aligned.** Friday-dated stocks and Monday-dated
  prices keep their own ``event_time``; resampling onto a common grid is a
  model's choice, made in the model, never the fixture's. A replay that
  aligned them would have to decide what was known on a day nothing was
  published.
- **Ties cannot hide an order.** Within one period no two lines share a
  ``known_at`` (OMNI-160): a capture is refused unless it is after the last.
  A collection that has one anyway is refused here, rather than resolved by
  whatever order it happened to arrive in. Across periods and series the
  order is ``event_time``, then source, then series, as ``SimSensor.read``
  orders a window.

Ordering by ``event_time`` instead would hand a model the week ending Friday 30
September 2022's stocks on that Friday, though they were published the next
Wednesday, after the Monday 3 October price that describes a later date. By
``known_at`` the look-ahead is impossible by construction.

Pure: no I/O, no clock. Guardrail code (POLICY-FORBIDDEN): it decides what a
model is shown, so it is part of how a model is judged.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from dataclasses import dataclass

from sis.ports import Reading
from sis.sensor_input import Absent


class AmbiguousVintage(ValueError):
    """Two vintages of one period became known at the same instant."""


@dataclass(frozen=True)
class AsOf:
    """What was known at ``at``: per period, the latest reading or stated absence."""

    at: datetime.datetime
    readings: tuple[Reading, ...]
    absent: tuple[Absent, ...]


def as_of(readings: Iterable[Reading], absent: Iterable[Absent],
          at: datetime.datetime) -> AsOf:
    """The as-of view at *at*: see the module docstring for the rule.

    *readings* and *absent* are every vintage there is (a sensor's read over
    all time by ``known_at``, or a trace read back); what was not yet known at
    *at* is left out here.
    """
    if at.utcoffset() is None:
        raise ValueError(f"as_of: {at.isoformat()!r} has no timezone, as every reading does")
    latest: dict[tuple[str, str, datetime.datetime], Reading | Absent] = {}
    every: list[Reading | Absent] = [*readings, *absent]
    for item in every:
        if item.known_at > at:
            continue
        key = (item.source, item.series, item.event_time)
        held = latest.get(key)
        if held is not None and held.known_at == item.known_at:
            raise AmbiguousVintage(
                f"{item.series} {item.event_time.isoformat()}: two vintages known at "
                f"{item.known_at.isoformat()}; their order is unknowable")
        if held is None or item.known_at > held.known_at:
            latest[key] = item

    def order(item: Reading | Absent) -> tuple[datetime.datetime, str, str]:
        return (item.event_time, item.source, item.series)

    seen = sorted(latest.values(), key=order)
    return AsOf(at=at,
                readings=tuple(item for item in seen if isinstance(item, Reading)),
                absent=tuple(item for item in seen if isinstance(item, Absent)))


__all__ = ["AmbiguousVintage", "AsOf", "as_of"]
