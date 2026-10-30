"""sis.viewer — the twin viewer (OMNI-40): the supply chain, replayed. Panel.

Run it::

    poetry install --with ui
    poetry run python -m sis.viewer                      # the autumn-2022 fixture
    poetry run python -m sis.viewer --trace runtime/sensor_traces/eia.jsonl

The chain is drawn as things, week by week. West Coast stocks are a tank and
the retail prices are pumps. California's premium over the US price turns red
when it spikes. Refineries and demand have no adapter yet, so they are drawn
empty. Charts sit underneath, with the predictor's forecast against what came
true. A toggle switches between what had been published by the moment shown
(the default) and everything, the future included. Paused when the price for
Monday 3 October 2022 came out (5 October), the two differ: the low stocks of
the week ending 30 September were not out until 6 October.

A second tab drives a generated scenario (OMNI-34) through the same view. It is
hatched and labelled SIMULATED throughout, because a simulation may reject a
model but never promote one (D4).

**Read-only by construction.** The viewer reads a fixture's JSON, a sensor trace
through the sanitising door (``sensor_trace.read_back``) or a generated
scenario. It writes nothing, opens no store for writing, and never talks to
Ray or the actors. ``tests/test_viewer.py`` pins all three. The predictor is a
SOFT target, so it is asked in a sandbox worker, never in this process.

**Served like the console.** It uses the console's ``frontend.*`` settings:
loopback by default, and it refuses to start unauthenticated on any other
bind (``operator.check_servable``). The default port is the console's plus one.

What to show is decided in :mod:`sis.twin_view`, which is pure and tested
without Panel. This module loads the data (read-only I/O) and draws it. Panel
is imported lazily, as in ``sis.frontend``, because it is optional (``--with ui``).
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sis import prediction_error, sensor_trace, twin_view
from sis.clock import parse_event_time
from sis.paths import PROJECT_ROOT
from sis.ports import Reading
from sis.prediction_error import CALIFORNIA, Question
from sis.sensor_input import Absent
from sis.twin_view import RECORDED, SIMULATED, Timeline

if TYPE_CHECKING:  # pragma: no cover - typing only
    import panel as pn

TITLE = "omnibase — twin viewer"
FIXTURE = PROJECT_ROOT / "specs" / "ca_price_next_week"
EASTERN = ZoneInfo("America/New_York")
AUTUMN_2022 = (datetime.datetime(2022, 8, 1, tzinfo=datetime.UTC),
               datetime.datetime(2022, 12, 1, tzinfo=datetime.UTC))

Ask = Callable[[Sequence[Question]], list[Any]]


# --- loading: read-only ------------------------------------------------------------


def predictor_source(path: pathlib.Path | None = None) -> str:
    """The predictor's source: *path*, else the built one, else the shipped stub."""
    from sis.contract import CA_PRICE_PERSISTENCE

    if path is None:
        path = PROJECT_ROOT / CA_PRICE_PERSISTENCE.entry_module
        if not path.exists():
            assert CA_PRICE_PERSISTENCE.stub_candidate_path is not None
            path = PROJECT_ROOT / CA_PRICE_PERSISTENCE.stub_candidate_path
    return path.read_text(encoding="utf-8")


def sandboxed(source: str, *, timeout_s: float = 120.0) -> Ask:
    """Ask *source*'s predictor in a sandbox worker, one batch per replay."""
    from sis.contract import CA_PRICE_PERSISTENCE

    def ask(asked: Sequence[Question]) -> list[Any]:
        return prediction_error.ask_in_sandbox(source, CA_PRICE_PERSISTENCE.entry, asked,
                                               timeout_s=timeout_s)

    return ask


def _absent(record: dict[str, Any], where: str) -> Absent:
    return Absent(record["source"], record["series"],
                  parse_event_time(record["event_time"], where=where),
                  parse_event_time(record["known_at"], where=where), record["marker"])


def _timeline(label: str, kind: str, readings: Sequence[Reading], absent: Sequence[Absent],
              asked: Sequence[Question], ask: Ask | None,
              reconstructed: frozenset[twin_view.ReadingKey] = frozenset()) -> Timeline:
    found: tuple[twin_view.Forecast, ...] = ()
    if ask is not None and asked:
        units = {r.unit for r in readings if r.series == CALIFORNIA}
        report = prediction_error.score(asked, ask(asked), unit=", ".join(sorted(units)),
                                        source=label)
        found = twin_view.forecasts(asked, report, readings)
    return Timeline(label=label, kind=kind, readings=tuple(readings), absent=tuple(absent),
                    forecasts=found, reconstructed=reconstructed)


_FETCHED = re.compile(r"fetched (\d{4}-\d{2}-\d{2}T[0-9:.]+[+-]\d{2}:\d{2})")


def load_fixture(directory: pathlib.Path = FIXTURE, *, ask: Ask | None = None) -> Timeline:
    """A recorded fixture (OMNI-35): every episode's as-of view, and its answer.

    The episodes' views together are every vintage known by the last one. Each
    episode is one question, and its ``_expect.json`` holds the answer. A
    reading known before the capture was fetched (from the README's provenance)
    was backfilled, so its ``known_at`` is reconstructed.
    """
    where = str(directory)
    readings: dict[twin_view.ReadingKey, Reading] = {}
    absent: dict[tuple[str, str, datetime.datetime, datetime.datetime], Absent] = {}
    asked: list[Question] = []
    for path in sorted((directory / "backtests").glob("*_fixture.json")):
        week_text = path.name.removesuffix("_fixture.json")
        episode = json.loads(path.read_text(encoding="utf-8"))
        state = episode["args"][0]
        shown = [Reading.from_record(r, where=f"{where}/{path.name}") for r in state["readings"]]
        for reading in shown:
            readings.setdefault(twin_view.reading_key(reading), reading)
        for record in state["absent"]:
            item = _absent(record, f"{where}/{path.name}")
            absent.setdefault((item.source, item.series, item.event_time, item.known_at), item)
        expect = json.loads((directory / "backtests" / f"{week_text}_expect.json")
                            .read_text(encoding="utf-8"))
        target = [r.event_time for r in shown if r.series == CALIFORNIA]
        next_week = datetime.datetime.combine(datetime.date.fromisoformat(week_text),
                                              datetime.time(), tzinfo=EASTERN)
        asked.append(Question(week=max(target) if target else next_week,
                              at=parse_event_time(state["as_of"], where=where), state=state,
                              next_week=next_week, actual=float(expect["value"])))
    readme = directory / "README.md"
    fetched = _FETCHED.search(readme.read_text(encoding="utf-8")) if readme.exists() else None
    marks: frozenset[twin_view.ReadingKey] = frozenset()
    if fetched is not None:
        at = parse_event_time(fetched.group(1), where=f"{where}/README.md")
        marks = frozenset(key for key, r in readings.items() if r.known_at < at)
    return _timeline(f"fixture {directory.name}", RECORDED, list(readings.values()),
                     list(absent.values()), asked, ask, marks)


def load_trace(path: pathlib.Path, *, ask: Ask | None = None,
               start: datetime.datetime | None = AUTUMN_2022[0],
               end: datetime.datetime | None = AUTUMN_2022[1]) -> Timeline:
    """A sensor trace (OMNI-33), read back through the sanitising door."""
    from sis.adapters_real import EIA_RULES

    lines = sensor_trace.load(path)
    if not lines:
        raise sensor_trace.TraceRefused(f"no trace at {path}: capture one first "
                                        "(scripts/capture_sensor.py)")
    readings, absent = sensor_trace.read_back(lines, EIA_RULES)
    marks = frozenset(
        (line["source"], line["series"],
         parse_event_time(line["event_time"], where=str(path)),
         parse_event_time(line["known_at"], where=str(path)))
        for line in lines if line.get("kind") == "reading" and line.get("reconstructed") is True)
    asked = prediction_error.questions(readings, absent, start=start, end=end)
    return _timeline(f"trace {path.name}", RECORDED, readings, absent, asked, ask, marks)


def load_scenario(kind: str, *, seed: int, ask: Ask | None = None,
                  **options: Any) -> Timeline:
    """A generated scenario (OMNI-34), labelled simulated."""
    from sis import scenarios

    generated = scenarios.generate(kind, seed=seed, **options)
    asked = prediction_error.questions(generated.readings, generated.absent)
    return _timeline(f"scenario {kind}:{seed}", SIMULATED, generated.readings,
                     generated.absent, asked, ask)


# --- drawing -----------------------------------------------------------------------

_COLOURS = {CALIFORNIA: "#d9480f", twin_view.US: "#1c7ed6", twin_view.LOS_ANGELES: "#e8590c",
            twin_view.SAN_FRANCISCO: "#f08c00", twin_view.STOCKS: "#2f6fb0"}


def _plain(when: datetime.datetime) -> datetime.datetime:
    """A naive UTC datetime, which Bokeh plots without guessing a zone."""
    return when.astimezone(datetime.UTC).replace(tzinfo=None)


def _figure(title: str, at: datetime.datetime, y_label: str, simulated: bool, *,
            x_range: tuple[datetime.datetime, datetime.datetime] | None = None) -> Any:
    from bokeh.models import Span
    from bokeh.plotting import figure

    extra: dict[str, Any] = {}
    if x_range is not None:
        extra["x_range"] = (_plain(x_range[0]), _plain(x_range[1]))
    fig = figure(title=title, x_axis_type="datetime", height=260, sizing_mode="stretch_width",
                 y_axis_label=y_label, tools="pan,wheel_zoom,box_zoom,reset,hover", **extra)
    fig.add_layout(Span(location=_plain(at), dimension="height", line_color="#555",
                        line_dash="dashed", line_width=1))
    if simulated:
        fig.background_fill_color = "#fff4e0"
    return fig


def _plot_series(fig: Any, readings: Sequence[Reading], timeline: Timeline, *,
                 label: str, colour: str) -> None:
    if not readings:
        return
    xs = [_plain(r.event_time) for r in readings]
    ys = [r.value for r in readings]
    fig.line(xs, ys, color=colour, legend_label=label, line_width=2)
    marked = [twin_view.reading_key(r) in timeline.reconstructed for r in readings]
    fig.scatter([x for x, m in zip(xs, marked, strict=True) if not m],
                [y for y, m in zip(ys, marked, strict=True) if not m], color=colour, size=6)
    if any(marked):
        # Hollow: a reconstructed known_at, from the backfill's release schedule.
        fig.scatter([x for x, m in zip(xs, marked, strict=True) if m],
                    [y for y, m in zip(ys, marked, strict=True) if m], color=colour,
                    fill_color="white", size=6)


def _forecast_table(rows: Sequence[twin_view.Forecast]) -> str:
    def money(value: float | None) -> str:
        return "—" if value is None else f"{value:+.3f}" if value < 0 else f"{value:.3f}"

    lines = ["| week | asked | forecast | actual | error | note |", "|---|---|---|---|---|---|"]
    for row in rows:
        week = row.week.date().isoformat() if row.week else "—"
        error = "—" if row.error is None else f"{row.error:+.3f}"
        lines.append(f"| {week} | {row.asked_at.date().isoformat()} | {money(row.predicted)} | "
                     f"{money(row.actual)} | {error} | {row.note} |")
    return "\n".join(lines)


def build_replay(timeline: Timeline) -> pn.viewable.Viewable:
    """One timeline's replay: player, known-at toggle, chain, charts and forecasts."""
    import panel as pn

    moments = timeline.moments()
    banner = (pn.pane.Alert(f"**SIMULATED** ({timeline.label}). A simulation may reject a "
                            "model, never promote one (D4).", alert_type="warning")
              if timeline.simulated else
              pn.pane.Alert(f"**Recorded** ({timeline.label}). Hollow points and ≈ mark a "
                            "reconstructed known_at (the backfill's release schedule).",
                            alert_type="info"))
    if not moments:
        return pn.Column(banner, pn.pane.Markdown("Nothing to replay in this timeline."))
    player = pn.widgets.Player(name="Week", start=0, end=len(moments) - 1, value=0,
                               loop_policy="once", interval=1500, sizing_mode="stretch_width")
    published = pn.widgets.Toggle(name="Only what was published by then", value=True,
                                  button_type="primary")
    bounds = twin_view.stock_range(timeline)
    weeks = [f.week for f in timeline.forecasts if f.week is not None]
    margin = datetime.timedelta(weeks=1)
    window = (min(weeks) - margin, max(weeks) + margin) if weeks else None
    covered = twin_view.time_range(timeline)
    span = (covered[0] - margin, covered[1] + margin) if covered else None

    def render(index: int, only_published: bool) -> pn.viewable.Viewable:
        at = moments[index]
        view = twin_view.visible(timeline, at, published_only=only_published)
        nodes = twin_view.chain(timeline, view)
        when = at.astimezone(EASTERN).strftime("%a %d %b %Y, %H:%M %Z")
        mode = "only what was published by then" if only_published else \
            "everything, including the future"
        prices = _figure("Retail prices", at, "Dollars per Gallon", timeline.simulated,
                         x_range=span)
        for series_id, label in twin_view.PRICE_SERIES.items():
            _plot_series(prices, twin_view.series(view, series_id), timeline, label=label,
                         colour=_COLOURS[series_id])
        prices.legend.location = "top_left"
        stocks = _figure("West Coast gasoline stocks", at, "Thousand Barrels",
                         timeline.simulated, x_range=span)
        _plot_series(stocks, twin_view.series(view, twin_view.STOCKS), timeline,
                     label="PADD 5", colour=_COLOURS[twin_view.STOCKS])
        stocks.legend.location = "bottom_left"
        shown = twin_view.visible_forecasts(timeline, at, published_only=only_published)
        forecast = _figure("California next week: forecast against actual", at,
                           "Dollars per Gallon", timeline.simulated, x_range=window)
        predicted = [(f.week, f.predicted) for f in shown if f.week and f.predicted is not None]
        actual = [(f.week, f.actual) for f in shown if f.week and f.actual is not None]
        if predicted:
            forecast.scatter([_plain(w) for w, _ in predicted], [v for _, v in predicted],
                             marker="square", size=8, color="#7048e8", legend_label="forecast")
        if actual:
            forecast.line([_plain(w) for w, _ in actual], [v for _, v in actual],
                          color=_COLOURS[CALIFORNIA], legend_label="actual", line_width=2)
        return pn.Column(
            pn.pane.Markdown(f"### {when}\nShowing {mode}."),
            pn.pane.SVG(twin_view.chain_svg(nodes, stocks=bounds, simulated=timeline.simulated),
                        sizing_mode="stretch_width"),
            pn.Row(prices, stocks, sizing_mode="stretch_width"),
            forecast,
            pn.pane.Markdown(_forecast_table(shown)),
            sizing_mode="stretch_width")

    return pn.Column(banner, pn.Row(player, published, sizing_mode="stretch_width"),
                     pn.bind(render, player.param.value, published.param.value),
                     sizing_mode="stretch_width")


def build_scenario(ask: Ask | None) -> pn.viewable.Viewable:
    """Slice 2: sliders that generate a scenario and replay it through the same view."""
    import panel as pn

    from sis import scenarios

    gen = scenarios.pinned()
    kind = pn.widgets.Select(name="Scenario", options=list(gen.KINDS), value=gen.KINDS[0])
    seed = pn.widgets.IntInput(name="Seed", value=0, start=0)
    weeks = pn.widgets.IntSlider(name="Weeks", start=6, end=52, value=gen.DEFAULT_WEEKS)
    low, high = gen.OBSERVED[kind.value]
    magnitude = pn.widgets.FloatSlider(name="Magnitude", start=low, end=high,
                                       value=(low + high) / 2, step=(high - low) / 100)
    events = pn.widgets.CheckButtonGroup(name="Data events", options=list(gen.DATA_EVENTS),
                                         value=[])

    def bounds(chosen: str) -> None:
        start, end = gen.OBSERVED[chosen]
        magnitude.param.update(start=start, end=end, value=(start + end) / 2,
                               step=(end - start) / 100)

    kind.param.watch(lambda event: bounds(event.new), "value")

    def render(chosen: str, seed_value: int, length: int, size: float,
               data_events: list[str]) -> pn.viewable.Viewable:
        try:
            timeline = load_scenario(chosen, seed=seed_value, ask=ask, weeks=length,
                                     magnitude=size, data_events=tuple(data_events))
        except ValueError as exc:
            return pn.pane.Alert(str(exc), alert_type="danger")
        return build_replay(timeline)

    return pn.Column(
        pn.pane.Markdown("Magnitudes stay within what the real series have shown "
                         "(`OBSERVED` in `specs/scenarios.py`)."),
        pn.Row(kind, seed, weeks, sizing_mode="stretch_width"),
        pn.Row(magnitude, pn.Column(pn.pane.Markdown("Data events"), events),
               sizing_mode="stretch_width"),
        pn.bind(render, kind.param.value, seed.param.value, weeks.param.value_throttled,
                magnitude.param.value_throttled, events.param.value),
        sizing_mode="stretch_width")


def build_app(recorded: Timeline, ask: Ask | None) -> pn.viewable.Viewable:
    """The whole page: the recorded replay and the scenario tab."""
    import panel as pn

    return pn.template.FastListTemplate(
        title=TITLE,
        main=[pn.Tabs(("Recorded", build_replay(recorded)),
                      ("Scenario (simulated)", build_scenario(ask)), dynamic=True)])


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from sis import config, frontend, operator
    from sis.operator import RefusesToServe

    parser = argparse.ArgumentParser(prog="python -m sis.viewer", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixture", type=pathlib.Path, default=FIXTURE)
    parser.add_argument("--trace", type=pathlib.Path, help="a sensor trace instead of a fixture")
    parser.add_argument("--predictor", type=pathlib.Path, default=None)
    parser.add_argument("--port", type=int, default=None,
                        help="default: the console's frontend.port plus one")
    parser.add_argument("--show", action="store_true", help="open a browser")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    settings = config.config().frontend
    try:
        # Before anything is loaded, as before anything binds a socket.
        operator.check_servable(settings)
    except RefusesToServe as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    ask = sandboxed(predictor_source(args.predictor))
    try:
        recorded = (load_trace(args.trace, ask=ask) if args.trace is not None
                    else load_fixture(args.fixture, ask=ask))
    except (OSError, ValueError) as exc:  # TraceRefused is a ValueError
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    try:
        frontend.serve(frontend=settings, show=args.show, title=TITLE,
                       app=lambda: build_app(recorded, ask),
                       port=settings.port + 1 if args.port is None else args.port)
    except RefusesToServe as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
