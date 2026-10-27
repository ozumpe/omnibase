"""OMNI-40: what the twin viewer shows at a moment of a replay. Pure: no Panel.

What these pin: with "only what was published" on, nothing is shown before its
``known_at`` (the 3 October 2022 case on the recorded fixture included); the
chain always draws the nodes no adapter senses yet, empty; a simulated chain is
labelled so; a reconstructed ``known_at`` is marked; and a forecast's actual is
hidden until it became known.
"""

from __future__ import annotations

import datetime

import pytest

from sis import twin_view, viewer
from sis.ports import Reading
from sis.prediction_error import CALIFORNIA, Question, Report, Step
from sis.twin_view import RECORDED, SIMULATED, STOCKS, US, Forecast, Timeline

ET = viewer.EASTERN
UNIT = "Dollars per Gallon"


def _at(day: str, hour: int = 17) -> datetime.datetime:
    return datetime.datetime.combine(datetime.date.fromisoformat(day), datetime.time(hour),
                                     tzinfo=ET)


def _reading(series: str, week: str, value: float, known: str, *, hour: int = 17) -> Reading:
    unit = "Thousand Barrels" if series == STOCKS else UNIT
    return Reading("eia", series, value, unit, _at(week, 0), _at(known, hour))


@pytest.fixture(scope="module")
def fixture() -> Timeline:
    return viewer.load_fixture(ask=None)


# -- what was known when -------------------------------------------------------------

def test_the_third_of_october_2022_case(fixture: Timeline) -> None:
    # The moment Monday 3 October's price came out (Wednesday 5 October, 5 p.m.
    # Eastern). The stocks for the week ending Friday 30 September, the low,
    # came out the next day: published only, the tank still shows 23 September.
    at = _at("2022-10-05")
    published = twin_view.visible(fixture, at)
    everything = twin_view.visible(fixture, at, published_only=False)
    assert twin_view.series(published, STOCKS)[-1].event_time == _at("2022-09-23", 0)
    assert _at("2022-09-30", 0) in {r.event_time for r in twin_view.series(everything, STOCKS)}
    assert twin_view.series(published, CALIFORNIA)[-1].event_time == _at("2022-10-03", 0)
    node = {n.name: n for n in twin_view.chain(fixture, published)}["stocks"]
    assert node.week == _at("2022-09-23", 0) and node.value == 24923.0
    # With the future shown, the tank is the world as it turned out to be then:
    # the low, not the last week of the data.
    later = {n.name: n for n in twin_view.chain(fixture, everything)}["stocks"]
    assert later.week == _at("2022-09-30", 0) and later.value == 24675.0
    assert later.known_at is not None and later.known_at > at


def test_nothing_is_shown_before_its_known_at(fixture: Timeline) -> None:
    for at in fixture.moments():
        view = twin_view.visible(fixture, at)
        assert view.readings, at
        assert all(r.known_at <= at for r in view.readings), at
        for node in twin_view.chain(fixture, view):
            assert node.known_at is None or node.known_at <= at


def test_a_revision_replaces_what_it_revises_only_once_known() -> None:
    first = _reading(CALIFORNIA, "2022-10-03", 6.20, "2022-10-05")
    revised = _reading(CALIFORNIA, "2022-10-03", 6.21, "2022-10-12")
    timeline = Timeline("t", RECORDED, (first, revised), ())
    assert [r.value for r in twin_view.visible(timeline, _at("2022-10-06")).readings] == [6.20]
    assert [r.value for r in twin_view.visible(timeline, _at("2022-10-12")).readings] == [6.21]
    assert [r.value for r in twin_view.visible(timeline, _at("2022-10-06"),
                                               published_only=False).readings] == [6.21]


def test_the_replay_moves_week_by_week_on_known_at(fixture: Timeline) -> None:
    moments = fixture.moments()
    assert moments == tuple(sorted(moments))
    assert len(moments) > 50   # without a predictor: every week's first known price


# -- the chain -----------------------------------------------------------------------

def test_the_unsensed_nodes_are_always_drawn_and_always_empty(fixture: Timeline) -> None:
    for at in (fixture.moments()[0], fixture.moments()[-1]):
        nodes = {n.name: n for n in twin_view.chain(fixture, twin_view.visible(fixture, at))}
        for name in ("refineries", "demand"):
            assert nodes[name].sensed is False and nodes[name].value is None
            assert "not yet sensed" in nodes[name].note


def test_the_premium_spikes_in_october_2022_and_not_in_august(fixture: Timeline) -> None:
    def premium(at: datetime.datetime) -> twin_view.Node:
        nodes = twin_view.chain(fixture, twin_view.visible(fixture, at))
        return {n.name: n for n in nodes}["premium"]

    october, august = premium(_at("2022-10-05")), premium(_at("2022-08-10"))
    assert october.alert and october.value is not None and october.value > 2.0
    assert not august.alert


def test_a_spike_needs_a_history_to_be_measured_against() -> None:
    week = _at("2022-10-03", 0)
    assert not twin_view.is_spike([(week, 5.0)])
    calm = [(week - datetime.timedelta(weeks=i), 1.0) for i in range(6, 0, -1)]
    assert twin_view.is_spike([*calm, (week, 1.6)])
    assert not twin_view.is_spike([*calm, (week, 1.4)])


@pytest.mark.docs   # the capture time comes from the fixture's README
def test_every_fixture_reading_is_marked_reconstructed(fixture: Timeline) -> None:
    # The fixture was built from a backfilled capture: no reading in it was
    # seen when it was first published.
    assert fixture.reconstructed == {twin_view.reading_key(r) for r in fixture.readings}
    nodes = twin_view.chain(fixture, twin_view.visible(fixture, _at("2022-10-05")))
    svg = twin_view.chain_svg(nodes, stocks=twin_view.stock_range(fixture), simulated=False)
    assert "≈" in svg


def test_a_simulated_chain_cannot_pass_for_a_recording() -> None:
    reading = _reading(CALIFORNIA, "2022-10-03", 6.2, "2022-10-05")
    for kind, labelled in ((SIMULATED, True), (RECORDED, False)):
        timeline = Timeline("t", kind, (reading,), ())
        nodes = twin_view.chain(timeline, twin_view.visible(timeline, _at("2022-10-06")))
        svg = twin_view.chain_svg(nodes, stocks=None, simulated=timeline.simulated)
        assert ("SIMULATED" in svg) is labelled
        assert ("url(#hatch)" in svg) is labelled


def test_labels_are_escaped_in_the_picture() -> None:
    node = twin_view.Node("refineries", "<script>", sensed=False, note="not yet sensed: a&b")
    nodes = (node, *twin_view.chain(Timeline("t", RECORDED, (), ()),
                                    twin_view.visible(Timeline("t", RECORDED, (), ()),
                                                      _at("2022-10-05")))[1:])
    svg = twin_view.chain_svg(nodes, stocks=None, simulated=False)
    assert "<script>" not in svg and "&lt;script&gt;" in svg and "a&amp;b" in svg


def test_a_timeline_is_recorded_or_simulated() -> None:
    with pytest.raises(ValueError, match="recorded"):
        Timeline("t", "imagined", (), ())


# -- prediction against reality ------------------------------------------------------

def _question(week: str, asked: str, next_week: str, actual: float) -> Question:
    return Question(week=_at(week, 0), at=_at(asked), state={}, next_week=_at(next_week, 0),
                    actual=actual)


def test_an_actual_is_hidden_until_it_became_known() -> None:
    asked = [_question("2022-09-26", "2022-09-28", "2022-10-03", 6.29)]
    report = Report(CALIFORNIA, UNIT, "t", (Step(_at("2022-09-26", 0), _at("2022-09-28"),
                                                 5.89, 6.29, 5.89 - 6.29),))
    readings = [_reading(CALIFORNIA, "2022-10-03", 6.29, "2022-10-05")]
    found = twin_view.forecasts(asked, report, readings)
    assert found[0].actual_known_at == _at("2022-10-05")
    timeline = Timeline("t", RECORDED, tuple(readings), (), forecasts=found)

    assert twin_view.visible_forecasts(timeline, _at("2022-09-27")) == ()
    before = twin_view.visible_forecasts(timeline, _at("2022-09-29"))
    assert before[0].predicted == 5.89 and before[0].actual is None and before[0].error is None
    after = twin_view.visible_forecasts(timeline, _at("2022-10-05"))
    assert after[0].actual == 6.29
    assert after[0].error == pytest.approx(-0.40)
    assert twin_view.visible_forecasts(timeline, _at("2022-09-27"), published_only=False) == found


def test_an_actual_never_known_within_the_replay_stays_hidden() -> None:
    forecast = Forecast(week=_at("2022-12-05", 0), asked_at=_at("2022-11-30"), predicted=4.8,
                        actual=4.7, actual_known_at=None)
    timeline = Timeline("t", RECORDED, (), (), forecasts=(forecast,))
    assert twin_view.visible_forecasts(timeline, _at("2099-01-01"))[0].actual is None


def test_the_premium_pairs_weeks_both_prices_describe() -> None:
    readings = (_reading(CALIFORNIA, "2022-10-03", 6.29, "2022-10-05"),
                _reading(US, "2022-10-03", 3.92, "2022-10-05"),
                _reading(CALIFORNIA, "2022-10-10", 6.4, "2022-10-12"))
    view = twin_view.visible(Timeline("t", RECORDED, readings, ()), _at("2022-10-12"))
    assert twin_view.premium(view) == [(_at("2022-10-03", 0), pytest.approx(2.37))]
