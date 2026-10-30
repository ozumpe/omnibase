"""sis.twin_view — what the twin viewer shows at a moment of a replay (OMNI-40).

The decisions behind the viewer (``sis.viewer``), kept out of it so they are
tested without Panel, as the repo keeps decision logic pure and I/O at the edges:

- **What was known when.** :func:`visible` is :func:`sis.replay.as_of` when only
  what was published by then is shown (the default), and the latest vintage of
  everything when the future is shown too. The viewer draws nothing else, so
  with the default on no reading reaches the screen before its ``known_at``.
- **The chain as things.** :func:`chain` turns a view into nodes: the West
  Coast's gasoline stocks as a tank, California's and the US's retail prices as
  pumps, California's premium over the US price, and the nodes no adapter senses
  yet (refineries, demand), drawn empty so the picture never implies data the
  twin does not have. :func:`chain_svg` draws them.
- **Prediction against reality.** :func:`forecasts` pairs each question a replay
  asked (``sis.prediction_error``) with its answer and with when the actual value
  became known, and :func:`visible_forecasts` hides an actual the moment did not
  have yet, as :func:`visible` hides a reading.

A :class:`Timeline` says whether it was recorded or simulated, and which of its
readings had their ``known_at`` reconstructed (OMNI-33's backfill), so the
viewer can label both (D4: a simulation may reject a model, never promote one).

Pure: no I/O, no clock.
"""

from __future__ import annotations

import datetime
import html
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sis.ports import Reading
from sis.prediction_error import CALIFORNIA, Question, Report
from sis.replay import AsOf, as_of
from sis.sensor_input import Absent

RECORDED = "recorded"
SIMULATED = "simulated"

STOCKS = "PET.WGTSTP51.W"                   # West Coast (PADD 5) total gasoline stocks
US = "PET.EMM_EPMRR_PTE_NUS_DPG.W"          # United States, regular, retail
LOS_ANGELES = "PET.EMM_EPMRR_PTE_Y05LA_DPG.W"
SAN_FRANCISCO = "PET.EMM_EPMRR_PTE_Y05SF_DPG.W"
PRICE_SERIES = {CALIFORNIA: "California", US: "United States", LOS_ANGELES: "Los Angeles",
                SAN_FRANCISCO: "San Francisco"}

# The premium is highlighted when it is this many times its median over the
# year before. Autumn 2022: about $1.00 for most of the year, $2.03 on 3 Oct.
SPIKE_RATIO = 1.5
_SPIKE_WINDOW = datetime.timedelta(weeks=52)

_END_OF_TIME = datetime.datetime.max.replace(tzinfo=datetime.UTC)

ReadingKey = tuple[str, str, datetime.datetime, datetime.datetime]


def reading_key(reading: Reading) -> ReadingKey:
    """One vintage of one period: what a reconstructed mark is attached to."""
    return (reading.source, reading.series, reading.event_time, reading.known_at)


@dataclass(frozen=True)
class Forecast:
    """One week the predictor was asked about, and what came true."""

    week: datetime.datetime | None            # the week predicted
    asked_at: datetime.datetime               # when it was asked
    predicted: float | None
    actual: float | None
    actual_known_at: datetime.datetime | None  # None: not known within the replay
    note: str = ""

    @property
    def error(self) -> float | None:
        """Predicted minus actual, as ``sis.prediction_error`` signs it."""
        if self.predicted is None or self.actual is None:
            return None
        return self.predicted - self.actual


@dataclass(frozen=True)
class Timeline:
    """Every vintage a replay can show, and how honestly it can be labelled."""

    label: str
    kind: str                                  # RECORDED or SIMULATED
    readings: tuple[Reading, ...]
    absent: tuple[Absent, ...]
    forecasts: tuple[Forecast, ...] = ()
    reconstructed: frozenset[ReadingKey] = frozenset()

    def __post_init__(self) -> None:
        if self.kind not in (RECORDED, SIMULATED):
            raise ValueError(f"a timeline is {RECORDED!r} or {SIMULATED!r}, not {self.kind!r}")

    @property
    def simulated(self) -> bool:
        return self.kind == SIMULATED

    def moments(self) -> tuple[datetime.datetime, ...]:
        """The replay's steps: each moment a question was asked, else each week's price.

        A week by week replay, driven like the OMNI-35 replay: by when a week's
        California price first became known, never by the week it describes.
        """
        if self.forecasts:
            return tuple(sorted({f.asked_at for f in self.forecasts}))
        first: dict[datetime.datetime, datetime.datetime] = {}
        for reading in self.readings:
            if reading.series == CALIFORNIA:
                known = first.get(reading.event_time)
                first[reading.event_time] = reading.known_at if known is None \
                    else min(known, reading.known_at)
        return tuple(sorted(set(first.values())))


def visible(timeline: Timeline, at: datetime.datetime, *, published_only: bool = True) -> AsOf:
    """What the viewer may draw at *at*: see the module docstring."""
    if published_only:
        return as_of(timeline.readings, timeline.absent, at)
    everything = as_of(timeline.readings, timeline.absent, _END_OF_TIME)
    return AsOf(at=at, readings=everything.readings, absent=everything.absent)


def series(view: AsOf, name: str) -> list[Reading]:
    """One series' readings in a view, by the week they describe."""
    return sorted((r for r in view.readings if r.series == name), key=lambda r: r.event_time)


def premium(view: AsOf) -> list[tuple[datetime.datetime, float]]:
    """California's price over the US price, for each week both are in the view."""
    us = {r.event_time: r.value for r in view.readings if r.series == US}
    return [(r.event_time, r.value - us[r.event_time])
            for r in series(view, CALIFORNIA) if r.event_time in us]


def is_spike(points: Sequence[tuple[datetime.datetime, float]]) -> bool:
    """Whether the last premium is :data:`SPIKE_RATIO` times its median over the year before.

    Needs at least four earlier weeks: a spike measured against nothing is noise.
    """
    if len(points) < 5:
        return False
    week, last = points[-1]
    before = [value for when, value in points[:-1] if when >= week - _SPIKE_WINDOW]
    if len(before) < 4:
        return False
    median = statistics.median(before)
    return median > 0 and last >= SPIKE_RATIO * median


@dataclass(frozen=True)
class Node:
    """One part of the chain, as the picture draws it."""

    name: str
    label: str
    sensed: bool
    value: float | None = None
    unit: str = ""
    week: datetime.datetime | None = None
    known_at: datetime.datetime | None = None
    reconstructed: bool = False
    note: str = ""                    # what would sense it, or why it is highlighted
    alert: bool = False


# Drawn whatever the data, so the picture is the roadmap too. Each names what
# would sense it; none has an adapter yet.
UNSENSED = (
    Node("refineries", "Refineries", sensed=False, note="not yet sensed: CEC Weekly Fuels Watch"),
    Node("demand", "Drivers", sensed=False, note="not yet sensed: PeMS traffic"),
)


def _latest_node(view: AsOf, name: str, label: str, series_id: str,
                 reconstructed: frozenset[ReadingKey]) -> Node:
    readings = series(view, series_id)
    if not readings:
        return Node(name, label, sensed=True, note="nothing published yet")
    last = readings[-1]
    return Node(name, label, sensed=True, value=last.value, unit=last.unit, week=last.event_time,
                known_at=last.known_at, reconstructed=reading_key(last) in reconstructed)


def chain(timeline: Timeline, view: AsOf) -> tuple[Node, ...]:
    """The chain at a view's moment, upstream to downstream: see the module docstring.

    Only readings describing a period that had begun by then are drawn. With
    only the published shown that changes nothing (a value is never known before
    its period began); with the future shown, the chain is the world as it later
    turned out to have been at that moment, not as it was at the end of the data.
    """
    view = AsOf(at=view.at, readings=tuple(r for r in view.readings if r.event_time <= view.at),
                absent=tuple(a for a in view.absent if a.event_time <= view.at))
    marks = timeline.reconstructed
    points = premium(view)
    if points:
        week, value = points[-1]
        spike = is_spike(points)
        gap = Node("premium", "California premium", sensed=True, value=value,
                   unit="Dollars per Gallon", week=week, alert=spike,
                   note=f"at least {SPIKE_RATIO:g}x its median over the year before"
                   if spike else "")
    else:
        gap = Node("premium", "California premium", sensed=True,
                   note="needs both prices for one week")
    return (
        UNSENSED[0],
        _latest_node(view, "stocks", "West Coast stocks", STOCKS, marks),
        _latest_node(view, "california", "California pump", CALIFORNIA, marks),
        _latest_node(view, "us", "US pump", US, marks),
        gap,
        UNSENSED[1],
    )


def time_range(timeline: Timeline) -> tuple[datetime.datetime, datetime.datetime] | None:
    """The first and last period any reading describes: a chart's fixed x axis."""
    times = [r.event_time for r in timeline.readings]
    return (min(times), max(times)) if times else None


def stock_range(timeline: Timeline) -> tuple[float, float] | None:
    """The lowest and highest stocks in the whole timeline: the tank's empty and full."""
    values = [r.value for r in timeline.readings if r.series == STOCKS]
    return (min(values), max(values)) if values else None


def forecasts(asked: Sequence[Question], report: Report,
              readings: Iterable[Reading]) -> tuple[Forecast, ...]:
    """Each question with its answer, and when its actual value first became known."""
    if len(asked) != len(report.steps):
        raise ValueError(f"{len(report.steps)} steps for {len(asked)} questions")
    first: dict[datetime.datetime, datetime.datetime] = {}
    for reading in readings:
        if reading.series == report.target:
            known = first.get(reading.event_time)
            first[reading.event_time] = reading.known_at if known is None \
                else min(known, reading.known_at)
    out = []
    for question, step in zip(asked, report.steps, strict=True):
        week = question.next_week
        out.append(Forecast(week=week, asked_at=question.at, predicted=step.predicted,
                            actual=step.actual,
                            actual_known_at=first.get(week) if week is not None else None,
                            note=step.note))
    return tuple(out)


def visible_forecasts(timeline: Timeline, at: datetime.datetime, *,
                      published_only: bool = True) -> tuple[Forecast, ...]:
    """The forecasts asked by *at*, with an actual only once it had become known.

    Showing the future shows every forecast with its actual, as :func:`visible`
    shows every reading.
    """
    if not published_only:
        return timeline.forecasts
    shown = []
    for forecast in timeline.forecasts:
        if forecast.asked_at > at:
            continue
        known = forecast.actual_known_at is not None and forecast.actual_known_at <= at
        shown.append(forecast if known else Forecast(
            week=forecast.week, asked_at=forecast.asked_at, predicted=forecast.predicted,
            actual=None, actual_known_at=None, note=forecast.note or "not known yet"))
    return tuple(shown)


# --- the picture --------------------------------------------------------------------

_WIDTH, _HEIGHT = 760, 230


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _when(node: Node) -> str:
    if node.week is None:
        return ""
    mark = " ≈" if node.reconstructed else ""
    return f"week of {node.week.date().isoformat()}{mark}"


def chain_svg(nodes: Sequence[Node], *, stocks: tuple[float, float] | None,
              simulated: bool) -> str:
    """The chain as an SVG: a tank, two pumps, the premium, and the unsensed nodes.

    ``≈`` after a week marks a reconstructed ``known_at``. A simulated chain is
    drawn on a hatched ground with a SIMULATED banner, so a screenshot of it
    cannot pass for a recording.
    """
    by_name = {node.name: node for node in nodes}
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_WIDTH} {_HEIGHT}" '
             f'width="100%" role="img" font-family="sans-serif" font-size="12">',
             '<defs><pattern id="hatch" width="10" height="10" patternUnits="userSpaceOnUse" '
             'patternTransform="rotate(45)"><rect width="10" height="10" fill="#fff4e0"/>'
             '<line x1="0" y1="0" x2="0" y2="10" stroke="#f0c080" stroke-width="4"/>'
             '</pattern></defs>',
             f'<rect width="{_WIDTH}" height="{_HEIGHT}" '
             f'fill="{"url(#hatch)" if simulated else "#f7f9fb"}"/>']
    if simulated:
        parts.append('<text x="10" y="18" font-weight="bold" fill="#b35c00">SIMULATED — '
                     'may reject a model, never promote one (D4)</text>')
    # Flow arrows between the stages, left to right.
    for x in (130, 300, 470, 640):
        parts.append(f'<path d="M{x} 120 l20 0 m-6 -5 l6 5 l-6 5" stroke="#8899aa" '
                     'fill="none" stroke-width="2"/>')
    parts += _unsensed(by_name["refineries"], 20)
    parts += _tank(by_name["stocks"], 160, stocks)
    parts += _pump(by_name["california"], 330)
    parts += _pump(by_name["us"], 500, small=True)
    parts += _premium(by_name["premium"], 500)
    parts += _unsensed(by_name["demand"], 665)
    parts.append("</svg>")
    return "".join(parts)


def _text(x: float, y: float, text: str, **attrs: str) -> str:
    extra = "".join(f' {key.replace("_", "-")}="{value}"' for key, value in attrs.items())
    return f'<text x="{x}" y="{y}"{extra}>{html.escape(text)}</text>'


def _unsensed(node: Node, x: float) -> list[str]:
    return [f'<rect x="{x}" y="70" width="105" height="100" rx="8" fill="none" '
            'stroke="#aab4bf" stroke-dasharray="6 4" stroke-width="2"/>',
            _text(x + 52, 110, node.label, text_anchor="middle", fill="#8a96a3",
                  font_weight="bold"),
            _text(x + 52, 130, "not yet sensed", text_anchor="middle", fill="#8a96a3"),
            _text(x + 52, 190, node.note.removeprefix("not yet sensed: "),
                  text_anchor="middle", fill="#8a96a3", font_size="10")]


def _tank(node: Node, x: float, bounds: tuple[float, float] | None) -> list[str]:
    height = 110.0
    level = 0.0
    if node.value is not None and bounds is not None:
        low, high = bounds
        level = 0.5 if high <= low else (node.value - low) / (high - low)
    fill = max(0.04, min(1.0, level)) * height if node.value is not None else 0.0
    out = [f'<rect x="{x}" y="{170 - height}" width="120" height="{height}" rx="10" '
           'fill="#ffffff" stroke="#36506b" stroke-width="2"/>']
    if fill:
        out.append(f'<rect x="{x + 3}" y="{167 - fill}" width="114" height="{fill}" rx="7" '
                   'fill="#4f8fca" opacity="0.85"/>')
    value = "—" if node.value is None else f"{node.value:,.0f} kbbl"
    out += [_text(x + 60, 48, node.label, text_anchor="middle", font_weight="bold"),
            _text(x + 60, 120, value, text_anchor="middle", font_weight="bold",
                  fill="#10283f"),
            _text(x + 60, 190, _when(node) or node.note, text_anchor="middle",
                  font_size="10", fill="#4b5b6b")]
    return out


def _pump(node: Node, x: float, *, small: bool = False) -> list[str]:
    top = 100 if small else 60
    body = 170 - top
    return [f'<rect x="{x}" y="{top}" width="60" height="{body}" rx="6" fill="#ffffff" '
            'stroke="#36506b" stroke-width="2"/>',
            f'<rect x="{x + 8}" y="{top + 8}" width="44" height="22" rx="3" fill="#10283f"/>',
            _text(x + 30, top + 24, _money(node.value), text_anchor="middle", fill="#e9f3ff",
                  font_size="11"),
            f'<path d="M{x + 60} {top + 20} q22 0 22 30 l0 40" stroke="#36506b" '
            'fill="none" stroke-width="3"/>',
            _text(x + 30, top - 8, node.label, text_anchor="middle", font_weight="bold"),
            _text(x + 30, 190 if not small else 205, _when(node) or node.note,
                  text_anchor="middle", font_size="10", fill="#4b5b6b")]


def _premium(node: Node, x: float) -> list[str]:
    colour = "#c62828" if node.alert else "#36506b"
    label = "premium " + ("—" if node.value is None else f"+{_money(node.value)}")
    out = [f'<rect x="{x - 10}" y="30" width="120" height="40" rx="20" fill="'
           f'{"#ffebee" if node.alert else "#ffffff"}" stroke="{colour}" stroke-width="2"/>',
           _text(x + 50, 55, label, text_anchor="middle", fill=colour, font_weight="bold")]
    if node.alert:
        out.append(_text(x + 50, 22, "spike", text_anchor="middle", fill=colour,
                         font_weight="bold"))
    return out


__all__ = [
    "PRICE_SERIES", "RECORDED", "SIMULATED", "SPIKE_RATIO", "STOCKS", "UNSENSED", "US",
    "Forecast", "Node", "Timeline", "chain", "chain_svg", "forecasts", "is_spike", "premium",
    "reading_key", "series", "stock_range", "time_range", "visible", "visible_forecasts",
]
