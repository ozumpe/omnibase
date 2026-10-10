"""sis.sensor_input — the one door sensor data comes in through (OMNI-32).

The hard rules treat generated *code* as untrusted. A real sensor adds
untrusted *data*: readings and text an outside party writes. A statistics
office is not an attacker, but it is outside the trust boundary, and what it
sends reaches scenario libraries, backtest fixtures and possibly a prompt.

So nothing a real adapter fetched becomes a :class:`~sis.ports.Reading` except
through :func:`sanitise`, one function for every adapter, the way
``gauntlet.ensure_sandbox_ready`` is one precondition for every executor of
generated code. What it guarantees:

- **The shape is the declared one.** A record has exactly the fields a reading
  needs, of the right types, for a series the adapter declared. A portal that
  changes its format fails here, loudly, and does not half-parse.
- **A marker is not a number.** Tables mark a withheld or unavailable value
  with letters or dashes. Coerced, that is a silent zero, and a twin that
  thinks the stocks emptied. Here it is :class:`Absent`: the publisher said
  nothing, which is neither a value nor a fault.
- **A number is finite, and plausible for its series.** A negative price is
  refused. A thousands separator is read only where the source declares one:
  ``1,234`` is 1234 in one country and 1.234 in another.
- **Both times are real.** Timezone-aware, and ``known_at`` not before the
  period the value describes began.
- **Size is bounded**, per field and per batch. A batch over the limit is
  refused whole.
- **Nothing is dropped quietly.** Every refusal is a :class:`Rejection` with a
  reason, and the result counts them. A feed that starts failing validation is
  a signal; a sanitiser that discards without saying so is indistinguishable
  from a sensor that went dark.

Free text (a series' name, a store's name, a footnote) is a different problem:
valid, and still written by an outsider. It is carried as
:class:`UntrustedText`, which has no raw ``str()``. Formatted into a prompt or
a generated file it is marked and inert: no quote, backslash, brace or newline
survives, so it cannot close a docstring, leave a string or add a line of its
own, whatever it is put inside. Where a real literal is needed, in a generated
file, :meth:`UntrustedText.literal` is its ``repr``, for an expression of its
own (OMNI-26's rule: ``repr``, never raw interpolation).

Pure: no I/O, no clock, no network. And guardrail code (POLICY-FORBIDDEN): the
loop must not be able to widen what it trusts.
"""

from __future__ import annotations

import datetime
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sis.clock import parse_event_time
from sis.ports import Reading

# What a source writes where a number belongs when it has none to give:
# withheld, not available, not applicable, suppressed. Compared after
# stripping and upper-casing.
NO_VALUE_MARKERS: frozenset[str] = frozenset({
    "", "-", "--", "---", ".", "..", "...", "*", "W", "NA", "N/A", "N.A.",
    "NULL", "NONE", "X", "Z", "(D)", "(NA)", "(S)", "(X)", "(Z)",
})
# Written as a number and not one: a fault in the feed, not a stated absence.
_NOT_FINITE: frozenset[str] = frozenset({"NAN", "INF", "+INF", "-INF", "INFINITY",
                                         "+INFINITY", "-INFINITY"})

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]*\Z")
_PLAIN_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?\Z")
_FIELDS = frozenset({"series", "value", "event_time", "known_at"})
_OPTIONAL = frozenset({"unit"})


class Refused(ValueError):
    """One record, or one piece of text, does not pass. Carries the reason's code."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Limits:
    """How much a batch and a field may hold."""

    max_batch: int = 10_000    # records in one batch
    max_id: int = 80           # characters in a source or series id
    max_text: int = 300        # characters in one free-text field
    max_number: int = 40       # characters in a number as written
    max_time: int = 40         # characters in a timestamp as written


@dataclass(frozen=True)
class SeriesRule:
    """What an adapter declares about one series it reads.

    ``minimum`` and ``maximum`` are the plausible range, both included: a value
    outside it is a fault in the feed, not news. ``period`` is how long before
    ``event_time`` the period a value describes began (seven days for a figure
    dated at its week's end, nothing for a price at a moment): a value cannot
    have been known before then.
    """

    unit: str
    minimum: float
    maximum: float
    period: datetime.timedelta = datetime.timedelta(0)

    def __post_init__(self) -> None:
        if not (math.isfinite(self.minimum) and math.isfinite(self.maximum)
                and self.minimum <= self.maximum):
            raise ValueError(f"a series' range must be finite and ordered, got "
                             f"[{self.minimum}, {self.maximum}]")
        if self.period < datetime.timedelta(0):
            raise ValueError("a series' period cannot be negative")
        if not self.unit.strip():
            raise ValueError("a series needs its unit")


@dataclass(frozen=True)
class SourceRules:
    """What an adapter declares about its source: its id, and each series it reads.

    A series that is not declared is refused: an adapter reads what it was
    written to read, and a new series in the feed is drift until a human adds
    it. ``thousands`` is the source's digit-group separator, if it writes one.
    """

    source: str
    series: Mapping[str, SeriesRule]
    thousands: str | None = None

    def __post_init__(self) -> None:
        for name in (self.source, *self.series):
            if not _ID.match(name):
                raise ValueError(f"{name!r} is not an id (letters, digits and _ . : -)")
        if self.thousands not in (None, ",", ".", " ", "'"):
            raise ValueError(f"{self.thousands!r} is not a digit-group separator")


@dataclass(frozen=True)
class Rejection:
    """One record refused, or the whole batch (``index`` -1).

    ``detail`` is for a human and safe to show anywhere: what it repeats of
    the source's own text is reduced to a few harmless characters.
    """

    index: int
    reason: str
    detail: str


@dataclass(frozen=True)
class Absent:
    """The source gave a marker where a value belongs: no value, which is not zero."""

    source: str
    series: str
    event_time: datetime.datetime
    known_at: datetime.datetime
    marker: str


@dataclass(frozen=True)
class Sanitised:
    """What one batch became: readings, stated absences, and every refusal."""

    readings: tuple[Reading, ...] = ()
    absent: tuple[Absent, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    received: int = 0

    def counts(self) -> dict[str, int]:
        """How many records were received, accepted, absent, and refused for each reason."""
        by_reason = Counter(rejection.reason for rejection in self.rejections)
        return {"received": self.received, "readings": len(self.readings),
                "absent": len(self.absent), "rejected": len(self.rejections),
                **{f"rejected.{reason}": count for reason, count in sorted(by_reason.items())}}

    def report(self) -> str:
        """One line for a log: never silent, even when all is well."""
        counts = self.counts()
        reasons = ", ".join(f"{key.removeprefix('rejected.')} {count}"
                            for key, count in counts.items() if key.startswith("rejected."))
        return (f"sensor input: {counts['received']} received, {counts['readings']} readings, "
                f"{counts['absent']} without a value, {counts['rejected']} rejected"
                + (f" ({reasons})" if reasons else ""))


# --- free text -------------------------------------------------------------------


def clean_text(raw: object, *, limit: int) -> str:
    """Text with what cannot be seen taken out, or :class:`Refused`.

    Control and format characters go: newlines, escapes, zero-width characters,
    and the bidirectional overrides that make text read differently from what
    it is. Over *limit* characters is refused, not cut: half a field is a
    different field.
    """
    if not isinstance(raw, str):
        raise Refused("schema", f"text expected, got {type(raw).__name__}")
    if len(raw) > limit * 4:  # before any work on something enormous
        raise Refused("size", f"{len(raw)} characters, at most {limit} allowed")
    kept = "".join(" " if char in "\t\n\r\v\f" else char for char in raw
                   if char in "\t\n\r\v\f" or unicodedata.category(char) not in ("Cc", "Cf",
                                                                            "Co", "Cs", "Cn"))
    cleaned = " ".join(kept.split())
    if len(cleaned) > limit:
        raise Refused("size", f"{len(cleaned)} characters, at most {limit} allowed")
    return cleaned


# What an outsider's text may keep when it is formatted into something else.
# Letters, digits and spaces always; of the rest only what means nothing to a
# string literal, a format template, a shell or markup.
_INERT_PUNCTUATION = frozenset(".,:;!?()-_/+’”")
_INERT_INSTEAD = str.maketrans({
    "'": "’", "`": "’", '"': "”",     # typographic: they close nothing
    "{": "(", "}": ")", "[": "(", "]": ")", "<": "(", ">": ")",
})
_OPEN, _CLOSE = "⟦", "⟧"   # mathematical white square brackets


def _inert(text: str) -> str:
    kept = "".join(
        char if (unicodedata.category(char)[0] in "LN" or char in _INERT_PUNCTUATION) else " "
        for char in text.translate(_INERT_INSTEAD))
    return " ".join(kept.split())


class UntrustedText:
    """Text an outsider wrote: cleaned, bounded, and never formatted as written.

    ``str()`` and every f-string give the text marked and made inert, in
    mathematical white brackets (U+27E6, U+27E7). No quote of either kind
    survives, no backslash, brace, bracket or newline, so it stays data
    whatever it is put inside: a line of a prompt, a docstring with either
    quote, a format template. Put where code belongs it is a syntax error,
    since no expression starts with that bracket. It can still be *read*, and
    what it says is an outsider's: the marks are there so that a reader, or a
    model, can tell.

    Two ways out, both by name:

    - :meth:`literal`: a Python string literal (its ``repr``) for a generated
      file, as an expression of its own, never inside another string;
    - :attr:`text`: the cleaned text itself.
    """

    __slots__ = ("_text",)

    def __init__(self, raw: object, *, limit: int = Limits().max_text) -> None:
        self._text = clean_text(raw, limit=limit)

    @property
    def text(self) -> str:
        return self._text

    def literal(self) -> str:
        """A Python literal of the text. Only ever as an expression of its own."""
        return repr(self._text)

    def __str__(self) -> str:
        return f"{_OPEN}{_inert(self._text)}{_CLOSE}"

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)

    def __repr__(self) -> str:
        return f"UntrustedText({self._text!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, UntrustedText) and other._text == self._text

    def __hash__(self) -> int:
        return hash(("UntrustedText", self._text))


# --- one record --------------------------------------------------------------------


_SHOWABLE = re.compile(r"[^A-Za-z0-9_.:+\-,/]")


def _shown(value: object, limit: int = 12) -> str:
    """What the source sent, made safe to show anywhere: for a rejection's detail.

    A rejection is read by people, filed in bugs and may end up near a prompt,
    so it does not carry the source's text as written. Its first few
    characters are shown, every one outside a small set as ``?``, spaces
    included, with its length: enough to recognise what arrived, not enough to
    say anything.
    """
    text = value if isinstance(value, str) else repr(value)
    shown = _SHOWABLE.sub("?", text[:limit])
    return f"'{shown}'" + (f"... ({len(text)} characters)" if len(text) > limit else "")


def _id(raw: object, what: str, limit: int) -> str:
    if not isinstance(raw, str):
        raise Refused("schema", f"{what} must be text, got {type(raw).__name__}")
    if len(raw) > limit or not _ID.match(raw):
        raise Refused("series" if what == "series" else "schema",
                      f"{what} {_shown(raw)} is not an id")
    return raw


def parse_number(raw: object, *, thousands: str | None = None,
                 limit: int = Limits().max_number) -> float | None:
    """The number *raw* states, ``None`` for a no-value marker, or :class:`Refused`.

    Never a guess. A marker is not zero. A digit-group separator is read only
    as the one the source declared, and only in groups of three.
    """
    if isinstance(raw, bool) or raw is None:
        if raw is None:
            return None
        raise Refused("schema", "a value must be a number or text, got bool")
    if isinstance(raw, int | float):
        try:
            number = float(raw)
        except OverflowError:
            raise Refused("not_finite", "an integer too large to be a measurement") from None
    elif isinstance(raw, str):
        if len(raw) > limit:
            raise Refused("size", f"a number of {len(raw)} characters")
        text = raw.strip()
        if text.upper() in NO_VALUE_MARKERS:
            return None
        if text.upper() in _NOT_FINITE:
            raise Refused("not_finite", f"{_shown(raw)} is not a finite number")
        if thousands is not None and thousands in text:
            grouped = re.compile(rf"[+-]?\d{{1,3}}({re.escape(thousands)}\d{{3}})+"
                                 + (r"(\.\d+)?\Z" if thousands != "." else r"(,\d+)?\Z"))
            if not grouped.match(text):
                raise Refused("number", f"{_shown(raw)} is not grouped in threes")
            text = text.replace(thousands, "")
            if thousands == ".":
                text = text.replace(",", ".")
        if not _PLAIN_NUMBER.match(text):
            raise Refused("number", f"{_shown(raw)} is not a number")
        number = float(text)
    else:
        raise Refused("schema", f"a value must be a number or text, got {type(raw).__name__}")
    if not math.isfinite(number):
        raise Refused("not_finite", f"{_shown(raw)} is not a finite number")
    return number


def _time(raw: object, what: str, limit: int) -> datetime.datetime:
    if isinstance(raw, datetime.datetime):
        raw = raw.isoformat()
    if not isinstance(raw, str):
        raise Refused("schema", f"{what} must be a timestamp, got {type(raw).__name__}")
    if len(raw) > limit:
        raise Refused("size", f"{what} of {len(raw)} characters")
    try:
        return parse_event_time(raw, where=what)
    except ValueError as exc:
        raise Refused("time", f"{what} {_shown(raw)}: "
                              f"{'no timezone' if 'no timezone' in str(exc) else 'not a time'}"
                      ) from None


def _one(record: object, rules: SourceRules, limits: Limits) -> Reading | Absent:
    if not isinstance(record, Mapping):
        raise Refused("schema", f"a record must be a mapping, got {type(record).__name__}")
    names = {name if isinstance(name, str) else repr(name) for name in record}
    missing, extra = _FIELDS - names, names - _FIELDS - _OPTIONAL
    if missing or extra:
        raise Refused("schema", "; ".join(
            part for part in (f"missing {sorted(missing)}" if missing else "",
                              f"unexpected {_shown(sorted(extra))}" if extra else "") if part))
    series = _id(record["series"], "series", limits.max_id)
    rule = rules.series.get(series)
    if rule is None:
        raise Refused("series", f"series {series!r} is not one this adapter declared")
    if "unit" in record and record["unit"] != rule.unit:
        raise Refused("unit", f"{series} is declared in {rule.unit!r}, "
                              f"the source says {_shown(record['unit'])}")
    event_time = _time(record["event_time"], "event_time", limits.max_time)
    known_at = _time(record["known_at"], "known_at", limits.max_time)
    if known_at < event_time - rule.period:
        raise Refused("known_before_period",
                      f"{series}: known at {known_at.isoformat()}, before the period "
                      f"it describes began ({(event_time - rule.period).isoformat()})")
    value = parse_number(record["value"], thousands=rules.thousands, limit=limits.max_number)
    if value is None:
        marker = record["value"]
        return Absent(rules.source, series, event_time, known_at,
                      "" if marker is None else str(marker).strip())
    if not rule.minimum <= value <= rule.maximum:
        raise Refused("range", f"{series}: {value!r} {rule.unit} is outside "
                               f"[{rule.minimum!r}, {rule.maximum!r}]")
    return Reading(source=rules.source, series=series, value=value, unit=rule.unit,
                   event_time=event_time, known_at=known_at)


# --- the door ----------------------------------------------------------------------


def sanitise(
    records: Iterable[object], rules: SourceRules, *, limits: Limits | None = None,
) -> Sanitised:
    """Turn what an adapter fetched into readings. The only way in (OMNI-32).

    Every record is accepted as a :class:`~sis.ports.Reading`, stated as
    :class:`Absent`, or refused with a reason; none is dropped without one. A
    batch over ``limits.max_batch`` is refused whole and nothing in it is read:
    a feed ten times its size is not the feed the adapter was written for.
    """
    limits = limits or Limits()
    batch: list[object] = []
    for record in records:
        batch.append(record)
        if len(batch) > limits.max_batch:
            return Sanitised(received=len(batch), rejections=(Rejection(
                -1, "batch_size", f"more than {limits.max_batch} records in one batch; "
                                  "none of them was read"),))
    readings: list[Reading] = []
    absent: list[Absent] = []
    rejections: list[Rejection] = []
    for index, record in enumerate(batch):
        try:
            outcome = _one(record, rules, limits)
        except Refused as refused:
            rejections.append(Rejection(index, refused.reason, refused.detail))
        except (ValueError, TypeError, OverflowError) as exc:
            # A record no rule foresaw is still a refusal, never a crash and
            # never a reading.
            rejections.append(Rejection(index, "schema", f"{type(exc).__name__}: "
                                                         f"{_shown(str(exc), 80)}"))
        else:
            (readings if isinstance(outcome, Reading) else absent).append(outcome)  # type: ignore[arg-type]
    return Sanitised(tuple(readings), tuple(absent), tuple(rejections), len(batch))


__all__ = [
    "NO_VALUE_MARKERS", "Absent", "Limits", "Refused", "Rejection", "Sanitised",
    "SeriesRule", "SourceRules", "UntrustedText", "clean_text", "parse_number", "sanitise",
]
