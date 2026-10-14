"""sis.sensor_trace — how a real sensor's readings are kept: the capture format (OMNI-33, D12).

Every later fixture, scenario library entry and holdout split inherits the
format a trace is recorded in, and history does not come round again to be
recorded properly. So the format is decided here, before the first trace:

- **Append-only JSON lines.** A line is never rewritten or removed. A value
  the source changes is a new line with a later ``known_at``, so a replay can
  show exactly what was known on a given day, revisions included.
- **Three kinds of line.** ``capture``: one per fetch, first in its block,
  saying where the data came from, when, the file's sha256, and what the door
  made of every series (counts, and every refusal up to a cap). ``reading`` and
  ``absent``: one per value the fetch added or changed, each naming its capture.
- **Both times on every value**, and ``reconstructed``. A value already old
  when first fetched (a 2022 week fetched in 2026 was known in 2022) gets its
  ``known_at`` from the source's publication schedule, capped at the fetch
  time, and is marked reconstructed. A value first seen by a fetch of a series
  the trace already watched is known at that fetch's time: an upper bound,
  never a guess.
- **Readings only.** No expectation, no outcome, no model output. A fixture
  divides a trace by time into what a model is shown and what it is judged
  against; that division is the fixture's (OMNI-35), and nothing here
  pre-empts it.
- **Read back through the door, and against its capture.** A trace lives
  under ``runtime/``, which the loop may write, so every line is sanitised
  again when read (OMNI-32), and every value line must sit in the block of
  the capture it names, known no later than that capture's fetch (exactly at
  it, unless reconstructed), with the block as long as its capture says. A
  line that fails either check fails the read: it does not vanish from it.
  What this cannot catch is a forgery consistent with itself, a made-up
  capture line with plausible values after it. The trace is staging, not the
  exam: a human reviews it on its way into ``specs/`` (OMNI-35).
- **Never under ``specs/``.** Promotion into the exam is OMNI-35, through
  :mod:`sis.contract_author`. Nothing here writes there.

What a trace holds is the publisher's content as published. EIA's terms, for
one, allow reuse with attribution and forbid modifying content while still
naming EIA as its source: a sum, an interpolation or a model's output is
derived, and is never written into a trace as the source's.

Guardrail code (POLICY-FORBIDDEN): a loop that could change how a trace is
captured could choose what its own models are later judged against.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import pathlib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sis.clock import parse_event_time
from sis.paths import SPECS_DIR
from sis.ports import Reading
from sis.sensor_input import Absent, Limits, Rejection, SourceRules, sanitise

# Refusals kept word for word in a capture line, per series. The counts stay
# complete: a feed that breaks should not write a line the size of the feed.
MAX_REJECTIONS_KEPT = 20


class TraceRefused(ValueError):
    """A trace that cannot be read honestly: malformed, or refused by the door."""


@dataclass(frozen=True)
class Fetched:
    """What one fetch saw of one series, before the door.

    ``values`` are (period, value) pairs as the source sent them, except that
    a period the adapter could place in time is a timezone-aware datetime. One
    it could not place stays as sent, for the door to refuse. ``unit`` is the
    source's own unit text, which the door checks against the declared one.
    """

    series: str
    unit: object
    values: Sequence[tuple[object, object]]


@dataclass(frozen=True)
class CaptureReport:
    """What one capture added to a trace, for a log line."""

    capture: str
    appended: int
    reconstructed: int
    counts: Mapping[str, Mapping[str, int]]
    missing: tuple[str, ...]

    @property
    def refused(self) -> int:
        return sum(counts.get("rejected", 0) for counts in self.counts.values())

    @property
    def vanished(self) -> int:
        """Periods the trace holds that this fetch no longer carried."""
        return sum(counts.get("vanished", 0) for counts in self.counts.values())

    def report(self) -> str:
        """One line: never silent, even when nothing changed."""
        received = sum(counts.get("received", 0) for counts in self.counts.values())
        text = (f"{self.capture}: {received} values received, {self.appended} new or changed "
                f"({self.reconstructed} with a reconstructed known_at), {self.refused} refused")
        if self.vanished:
            text += f", {self.vanished} periods no longer sent"
        if self.missing:
            text += f"; not in the source: {', '.join(self.missing)}"
        return text

    @classmethod
    def of(cls, lines: Sequence[Mapping[str, Any]]) -> CaptureReport:
        """The report for the block of lines one capture appended."""
        head, values = lines[0], lines[1:]
        return cls(capture=str(head["capture"]), appended=len(values),
                   reconstructed=sum(1 for line in values if line["reconstructed"]),
                   counts=head["counts"], missing=tuple(head["missing"]))


# --- the file ----------------------------------------------------------------------


def writable(path: pathlib.Path) -> pathlib.Path:
    """*path*, resolved, unless it lies in ``specs/``: the exam is never written here.

    Compared by the file system's own identity (device and inode), not by
    spelling: on a case-insensitive disk ``SPECS/`` is ``specs/``.
    """
    resolved = path.resolve()
    for place in (resolved, *resolved.parents):
        try:
            inside = os.path.samefile(place, SPECS_DIR)
        except OSError:            # not there (yet): it cannot be specs/
            inside = place == SPECS_DIR.resolve()
        if inside:
            raise PermissionError(
                f"{path} is inside specs/, the exam. A trace is captured into runtime/ and "
                "promoted into specs/ only through the human-approved contract-author path "
                "(OMNI-35)")
    return resolved


def _decode(raw: bytes, where: pathlib.Path) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise TraceRefused(f"{where}: not UTF-8 text") from None
    if text and not text.endswith("\n"):
        raise TraceRefused(f"{where}: the last line is incomplete, so a capture was cut "
                           "short or the file was edited; it is not appended to or read")
    lines: list[dict[str, Any]] = []
    for number, entry in enumerate(text.splitlines(), start=1):
        try:
            line = json.loads(entry)
        except ValueError:
            raise TraceRefused(f"{where}: line {number} is not JSON") from None
        if not isinstance(line, dict) or line.get("kind") not in ("capture", "reading",
                                                                  "absent"):
            raise TraceRefused(f"{where}: line {number} is not a capture, reading or absence")
        lines.append(line)
    return lines


def load(path: pathlib.Path) -> list[dict[str, Any]]:
    """Every line of the trace at *path*, oldest first. None if nothing was captured yet."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    return _decode(raw, path)


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while chunk := os.read(fd, 1 << 20):
        chunks.append(chunk)
    return b"".join(chunks)


def append(path: pathlib.Path,
           compute: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Append what *compute* makes of the trace's lines, holding its lock throughout.

    Read and write under one exclusive lock, so two captures cannot both take
    a series for new and backfill it twice. The file is opened by its own
    name only (no symlink, no second hard link) and checked against
    ``specs/`` again once open. A block that cannot be written whole, the
    disk full say, is cut back off: the trace is as it was, never half a
    capture.
    """
    target = writable(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if os.fstat(fd).st_nlink != 1:
            raise PermissionError(f"{target} has another name (a hard link); a trace is "
                                  "written only under its own")
        writable(target)           # again, now that it is open
        new = compute(_decode(_read_all(fd), target))
        if new:
            data = memoryview("".join(json.dumps(line, sort_keys=True, allow_nan=False) + "\n"
                                      for line in new).encode("utf-8"))
            end = os.fstat(fd).st_size
            try:
                while data:
                    data = data[os.write(fd, data):]
                os.fsync(fd)
            except BaseException:
                os.ftruncate(fd, end)
                raise
    finally:
        os.close(fd)               # and with it the lock
    return new


# --- through the door, both ways ----------------------------------------------------


def _record(line: Mapping[str, Any]) -> dict[str, Any]:
    record = {"series": line.get("series"), "event_time": line.get("event_time"),
              "known_at": line.get("known_at")}
    if line["kind"] == "reading":
        record.update(value=line.get("value"), unit=line.get("unit"))
    else:
        record["value"] = line.get("marker")
    return record


def _instant(raw: object, what: str) -> datetime.datetime:
    if not isinstance(raw, str) or len(raw) > Limits().max_time:
        raise TraceRefused(f"{what} is not a timestamp")
    try:
        return parse_event_time(raw, where=what)
    except ValueError:
        raise TraceRefused(f"{what} is not a timezone-aware timestamp") from None


def _check_blocks(lines: Sequence[Mapping[str, Any]]) -> None:
    """Every value line sits in the block of the capture it names, and says no
    more than that capture could know; every block is as long as it says."""
    head: Mapping[str, Any] | None = None
    fetched_at = datetime.datetime.min.replace(tzinfo=datetime.UTC)
    expected = held = 0

    def block_is_whole() -> None:
        if head is not None and held != expected:
            raise TraceRefused(f"capture {head.get('capture')!r} says it added {expected} "
                               f"values, and {held} follow it")

    for number, line in enumerate(lines, start=1):
        if line["kind"] == "capture":
            block_is_whole()
            head, held = line, 0
            fetched_at = _instant(line.get("fetched_at"), f"line {number}: fetched_at")
            counts = line.get("counts")
            if not isinstance(counts, dict) or not all(
                    isinstance(c, dict) and isinstance(c.get("appended"), int)
                    for c in counts.values()):
                raise TraceRefused(f"line {number}: a capture without its counts")
            expected = sum(c["appended"] for c in counts.values())
            continue
        if head is None or line.get("capture") != head.get("capture"):
            raise TraceRefused(f"line {number} is not in the block of the capture it names")
        reconstructed = line.get("reconstructed")
        known_at = _instant(line.get("known_at"), f"line {number}: known_at")
        if not isinstance(reconstructed, bool) or known_at > fetched_at or (
                not reconstructed and known_at != fetched_at):
            raise TraceRefused(f"line {number}: known at {known_at.isoformat()}, which a "
                               f"capture fetched at {fetched_at.isoformat()} cannot have seen")
        held += 1
    block_is_whole()


def read_back(lines: Sequence[Mapping[str, Any]], rules: SourceRules) -> tuple[
        tuple[Reading, ...], tuple[Absent, ...]]:
    """A trace's readings and stated absences, each through the door again.

    First the blocks are checked against their captures (:func:`_check_blocks`),
    then every series goes through the door, readings and absences apart and in
    batches the door accepts. Any refusal raises: the trace was written from
    what the door accepted, so a refusal now means it was edited or cut short,
    or the rules changed under it, and any of those needs a human.
    """
    _check_blocks(lines)
    by_series: dict[object, list[Mapping[str, Any]]] = defaultdict(list)
    for line in lines:
        if line["kind"] == "capture":
            continue
        if line.get("source") != rules.source:
            raise TraceRefused(f"a {rules.source} trace holds a line from another source")
        by_series[line.get("series")].append(line)
    readings: list[Reading] = []
    absent: list[Absent] = []
    limits = Limits()
    for series_lines in by_series.values():
        for kind in ("reading", "absent"):
            of_kind = [line for line in series_lines if line["kind"] == kind]
            for start in range(0, len(of_kind), limits.max_batch):
                batch = of_kind[start:start + limits.max_batch]
                door = sanitise((_record(line) for line in batch), rules, limits=limits)
                if door.rejections:
                    first = door.rejections[0]
                    raise TraceRefused(f"the trace does not pass the door ({door.report()}); "
                                       f"first: {first.reason}: {first.detail}")
                if door.absent if kind == "reading" else door.readings:
                    raise TraceRefused("a line of the trace says it is a reading and holds no "
                                       "value, or says it is an absence and holds one")
                readings.extend(door.readings)
                absent.extend(door.absent)
    return tuple(readings), tuple(absent)


# --- one capture ---------------------------------------------------------------------


_State = tuple[str, float | str]   # ("value", 24675.0) or ("absent", "W")


def _latest(readings: Iterable[Reading], absent: Iterable[Absent]) -> tuple[
        dict[tuple[str, datetime.datetime], _State], set[str]]:
    """The latest state of every (series, period), and the series the trace holds."""
    latest: dict[tuple[str, datetime.datetime], tuple[datetime.datetime, _State]] = {}
    states: list[tuple[datetime.datetime, tuple[str, datetime.datetime], _State]] = [
        *((r.known_at, (r.series, r.event_time), ("value", r.value)) for r in readings),
        *((a.known_at, (a.series, a.event_time), ("absent", a.marker)) for a in absent)]
    for known_at, key, state in states:
        if key not in latest or known_at >= latest[key][0]:
            latest[key] = (known_at, state)
    return ({key: state for key, (_, state) in latest.items()},
            {series for series, _ in latest})


def _period_key(period: object) -> object:
    """What makes two periods of one fetch the same period: the instant, or the text sent."""
    return period if isinstance(period, datetime.datetime) else repr(period)[:100]


def _value_line(outcome: Reading | Absent, *, reconstructed: bool,
                capture: str) -> dict[str, Any]:
    line: dict[str, Any] = {
        "kind": "reading" if isinstance(outcome, Reading) else "absent",
        "source": outcome.source, "series": outcome.series,
        "event_time": outcome.event_time.isoformat(), "known_at": outcome.known_at.isoformat(),
        "reconstructed": reconstructed, "capture": capture}
    if isinstance(outcome, Reading):
        line.update(value=outcome.value, unit=outcome.unit)
    else:
        line["marker"] = outcome.marker
    return line


def capture_lines(
    prior: Sequence[Mapping[str, Any]],
    fetched: Iterable[Fetched],
    rules: SourceRules,
    *,
    capture: str,
    fetched_at: datetime.datetime,
    published: Callable[[str, datetime.datetime], datetime.datetime],
    provenance: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """The lines one fetch adds to a trace whose lines so far are *prior*. Pure.

    A series the trace has never held is backfill: a value's ``known_at`` is
    ``published(series, period)``, the source's schedule, capped at the fetch
    time, and its line is marked reconstructed. A series the trace holds is
    watched: a value that is new, or differs from the latest line for its
    period, is known at the fetch time. An unchanged value adds nothing. A
    period sent twice in one fetch is refused every time, valid or not, since
    the source has said two things about it. A period the trace holds that a
    watched series no longer carries is counted as ``vanished``: nothing in
    the trace is withdrawn, and the count says the source moved.

    The first line is always the capture's own, even when nothing changed:
    "fetched, and nothing was new" is a fact a later reader needs.
    """
    readings, absent = read_back(prior, rules)
    state, watched = _latest(readings, absent)
    counts: dict[str, dict[str, int]] = {}
    kept: list[dict[str, Any]] = []
    values: list[dict[str, Any]] = []
    for item in fetched:
        backfill = item.series not in watched
        scheduled = backfill and item.series in rules.series   # the door refuses the rest
        sent = Counter(_period_key(period) for period, _ in item.values)
        records: list[dict[str, object]] = []
        positions: list[int] = []
        rejections: list[Rejection] = []
        for index, (period, value) in enumerate(item.values):
            if sent[_period_key(period)] > 1:
                rejections.append(Rejection(index, "duplicate", f"{item.series}: one period "
                                            f"sent {sent[_period_key(period)]} times"))
                continue
            known_at = fetched_at
            if scheduled and isinstance(period, datetime.datetime):
                try:
                    known_at = min(published(item.series, period), fetched_at)
                except (OverflowError, ValueError):   # off the calendar: the door refuses it
                    known_at = fetched_at
            records.append({
                "series": item.series, "value": value, "unit": item.unit,
                "event_time": period.isoformat() if isinstance(period, datetime.datetime)
                else period,
                "known_at": known_at.isoformat()})
            positions.append(index)
        # In batches the door accepts: a daily series has more values than one
        # batch holds, and the size of a series is bounded upstream (its line).
        limits = Limits()
        accepted: list[Reading] = []
        stated: list[Absent] = []
        for start in range(0, len(records), limits.max_batch):
            door = sanitise(records[start:start + limits.max_batch], rules, limits=limits)
            accepted.extend(door.readings)
            stated.extend(door.absent)
            rejections.extend(Rejection(positions[start + r.index] if r.index >= 0 else -1,
                                        r.reason, r.detail) for r in door.rejections)
        added = 0
        outcomes: list[Reading | Absent] = [*accepted, *stated]
        for outcome in outcomes:
            key = (outcome.series, outcome.event_time)
            now: _State = (("value", outcome.value) if isinstance(outcome, Reading)
                           else ("absent", outcome.marker))
            if state.get(key) == now:
                continue
            values.append(_value_line(outcome, reconstructed=backfill, capture=capture))
            added += 1
        held = {period for series, period in state if series == item.series}
        vanished = len(held - {period for period, _ in item.values
                               if isinstance(period, datetime.datetime)})
        by_reason = Counter(rejection.reason for rejection in rejections)
        counts[item.series] = {
            "received": len(item.values), "readings": len(accepted),
            "absent": len(stated), "rejected": len(rejections), "appended": added,
            **({"vanished": vanished} if vanished else {}),
            **{f"rejected.{reason}": n for reason, n in sorted(by_reason.items())}}
        kept.extend({"series": item.series, "index": r.index, "reason": r.reason,
                     "detail": r.detail} for r in rejections[:MAX_REJECTIONS_KEPT])
    head = {"kind": "capture", "capture": capture, "source": rules.source,
            "fetched_at": fetched_at.isoformat(), "provenance": dict(provenance),
            "counts": counts, "rejections": kept,
            "missing": sorted(set(rules.series) - set(counts))}
    return [head, *values]


def describe(lines: Sequence[Mapping[str, Any]]) -> str:
    """What a trace holds, in one line, without reading it through the door."""
    kinds = Counter(line["kind"] for line in lines)
    reconstructed = sum(1 for line in lines if line["kind"] != "capture"
                        and line.get("reconstructed") is True)
    captures = [line for line in lines if line["kind"] == "capture"]
    last = ""
    if captures:           # the file is runtime data: only a real timestamp is shown
        try:
            when = _instant(captures[-1].get("fetched_at"), "fetched_at").isoformat()
        except TraceRefused:
            when = "?"
        last = f", last fetched {when}"
    return (f"{kinds['reading']} readings ({reconstructed} with a reconstructed known_at), "
            f"{kinds['absent']} without a value, {len(captures)} captures{last}")


__all__ = [
    "MAX_REJECTIONS_KEPT", "CaptureReport", "Fetched", "TraceRefused", "append",
    "capture_lines", "describe", "load", "read_back", "writable",
]
