"""sis.clock — event time behind a port, so history can be replayed.

The engine reads "time" for three unrelated reasons, and conflating them is the
mistake this module exists to prevent. Only the first belongs here.

1. **Event time — "when did this happen?"** Episodic events, provenance entries,
   and the timestamps on a recorded fixture. This is the port: it must be
   replayable, because a backtest reconstructs what a model knew *at the moment
   an episode occurred*, and a gate that reads the host's wall clock instead
   produces a different answer on every run.

2. **Duration — "how long did that take?"** ``time.perf_counter()`` in the
   benchmark gate and in request timing. **Deliberately not behind this port.**
   A monotonic duration source measures an interval that really elapsed; routing
   it through a replay clock would report zero elapsed time and quietly destroy
   the benchmark gate, which is the one measurement the whole Class-1 loop turns
   on. If you are timing something, use ``perf_counter`` directly.

3. **Window filtering inside a Serve replica** (``CanaryRouter._record``). Also
   not behind this port, for a subtler reason: the replica timestamps
   observations and filters them *in the same process*, precisely so a reader's
   clock never has to be reconciled with a replica's. Injecting a replayable
   clock there would reintroduce the two-clock problem that design avoids.
   ``InMemoryCloud`` already takes its own ``clock`` callable for window
   filtering; that is a monotonic source for the same job, and is not this.

## Engine time is not world time

A second distinction, easy to lose once a clock is injectable: the SelfModel's
provenance entries and the episodic log's ``ts`` record **when the engine
acted**, not when the world did. Those are an audit trail, and an audit trail
that can be moved is worth less than one that cannot — so ``SelfModel.record``
deliberately stays on the wall clock. ``EpisodicEvent`` takes a clock only so a
*replay driver* can stamp reconstructed episodes in the timeline they belong to,
and so tests need not sleep; the default is unchanged.

The rule of thumb: if the timestamp answers "when did something happen out
there", it is event time. If it answers "when did we do this", it is wall clock.

## Why this exists before anything reads it

Nothing replays a trace yet. The field is here now because **a fixture recorded
without event time cannot be replayed and history does not come round again** —
unlike code, you cannot re-record last quarter. The cost of the field today is a
few lines; the cost of adding it after fixtures exist is every fixture. See
docs/OMNITRACK_VISION.md D6.

## Configuration is an argument, never an environment variable

Role actors are *detached* Ray actors in their own processes: they inherit the
driver's environment at **creation**, so anything exported after ``bootstrap()``
is invisible to them. A ``SIS_CLOCK`` variable would therefore work in a
single-process test and silently do nothing in a real cycle. Pass a clock in, the
same rule ``SIS_CONTRACT`` already follows.
"""

from __future__ import annotations

import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A source of *event time*, always timezone-aware.

    Read-only on purpose. Advancing is a property of a particular
    implementation, not of the capability: consumers only ever ask what time it
    is, and putting ``advance_to`` here would force :class:`WallClock` to either
    lie or raise. The replay driver holds a concrete :class:`ReplayClock` and
    drives it; everything downstream depends on this one method.
    """

    def now(self) -> datetime.datetime: ...


class WallClock:
    """The host clock in UTC — current behaviour, and the default everywhere."""

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)


class ReplayClock:
    """Event time driven by a recorded trace.

    Doubles as the test clock: constructed at an instant and never advanced, it
    is a frozen clock, which is how a gate becomes testable without sleeping.

    Advancing is **monotonic — it refuses to go backwards**. A trace whose
    episodes are out of chronological order is a data bug, and one that silently
    rewound would produce a replay that is reproducible but wrong, which is
    strictly worse than a loud failure. Replaying a genuinely separate trace
    means a new clock, not a rewind.
    """

    def __init__(self, start: datetime.datetime) -> None:
        self._now = _require_aware(start, "ReplayClock start")

    def now(self) -> datetime.datetime:
        return self._now

    def advance_to(self, moment: datetime.datetime) -> None:
        """Move event time to *moment*. Raises if that would move backwards."""
        moment = _require_aware(moment, "ReplayClock.advance_to")
        if moment < self._now:
            raise ValueError(
                f"replay clock cannot move backwards: {moment.isoformat()} is before "
                f"the current {self._now.isoformat()} — the trace is out of order, "
                "or two traces are sharing one clock"
            )
        self._now = moment

    @classmethod
    def at(cls, raw: str) -> ReplayClock:
        """Construct from an ISO-8601 string, e.g. a fixture's ``event_time``."""
        return cls(parse_event_time(raw, where="ReplayClock.at"))


def parse_event_time(raw: str, *, where: str) -> datetime.datetime:
    """Parse an ISO-8601 instant, rejecting anything ambiguous.

    Timezone-awareness is **required**, not defaulted. A naive timestamp means
    "10:30, somewhere" — replaying it assumes an offset the recorder never
    stated, and the error surfaces as a model that looks subtly wrong rather than
    as a parse failure. ``Z`` is accepted (``fromisoformat`` handles it from
    3.11), so the common recorded form works.
    """
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where}: {raw!r} is not an ISO-8601 timestamp ({exc})") from exc
    return _require_aware(parsed, where)


def _require_aware(moment: datetime.datetime, where: str) -> datetime.datetime:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(
            f"{where}: {moment.isoformat()!r} has no timezone. Event time must be "
            "unambiguous to be replayable — record an offset (or 'Z' for UTC)."
        )
    return moment


def now_iso(clock: Clock | None = None) -> str:
    """The current event time as an ISO-8601 string; wall clock when unset.

    The default keeps every existing caller behaving exactly as it did, so
    adopting the port is opt-in per call site rather than a flag day.
    """
    return (clock or WallClock()).now().isoformat()
