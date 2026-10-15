"""OMNI-35: what a model may see at a moment of a replay (``sis.replay.as_of``)."""

from __future__ import annotations

import datetime

import pytest

from sis import policy
from sis.ports import Reading
from sis.replay import AmbiguousVintage, as_of
from sis.sensor_input import Absent

UTC = datetime.UTC
WEEK = datetime.datetime(2022, 9, 30, tzinfo=UTC)


def _r(value: float, known: int, *, series: str = "s", week: datetime.datetime = WEEK) -> Reading:
    return Reading(source="eia", series=series, value=value, unit="u", event_time=week,
                   known_at=week + datetime.timedelta(days=known))


def _a(known: int, *, week: datetime.datetime = WEEK) -> Absent:
    return Absent("eia", "s", week, week + datetime.timedelta(days=known), "")


def test_nothing_is_seen_before_it_is_known() -> None:
    view = as_of([_r(1.0, 5)], [], WEEK + datetime.timedelta(days=4))
    assert view.readings == () and view.absent == ()
    assert as_of([_r(1.0, 5)], [], WEEK + datetime.timedelta(days=5)).readings == (_r(1.0, 5),)


def test_a_revision_replaces_what_it_revises_only_from_when_it_became_known() -> None:
    first, revised = _r(24675.0, 5), _r(24700.0, 12)
    before = as_of([first, revised], [], WEEK + datetime.timedelta(days=11))
    after = as_of([first, revised], [], WEEK + datetime.timedelta(days=12))
    assert before.readings == (first,) and after.readings == (revised,)


def test_a_withdrawal_hides_the_value_from_then_on() -> None:
    value, withdrawn = _r(24675.0, 5), _a(9)
    assert as_of([value], [withdrawn], WEEK + datetime.timedelta(days=8)).readings == (value,)
    later = as_of([value], [withdrawn], WEEK + datetime.timedelta(days=9))
    assert later.readings == () and later.absent == (withdrawn,)


def test_mixed_anchors_are_kept_as_they_are_and_ordered_by_event_time() -> None:
    # Friday stocks and Monday prices: no alignment, no resampling.
    friday = datetime.datetime(2022, 9, 30, tzinfo=UTC)
    monday = datetime.datetime(2022, 10, 3, tzinfo=UTC)
    stock = _r(24675.0, 5, series="stocks", week=friday)
    price = _r(6.213, 2, series="price", week=monday)
    view = as_of([price, stock], [], monday + datetime.timedelta(days=4))
    assert view.readings == (stock, price)
    assert [r.event_time for r in view.readings] == [friday, monday]


def test_two_vintages_known_at_one_instant_are_refused() -> None:
    with pytest.raises(AmbiguousVintage, match="unknowable"):
        as_of([_r(1.0, 5), _r(2.0, 5)], [], WEEK + datetime.timedelta(days=6))
    with pytest.raises(AmbiguousVintage):
        as_of([_r(1.0, 5)], [_a(5)], WEEK + datetime.timedelta(days=6))


def test_the_moment_needs_a_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        as_of([], [], datetime.datetime(2022, 10, 3))


def test_what_a_model_is_shown_is_guardrail_code() -> None:
    for path in ("sis/replay.py", "sis/fixture_builder.py"):
        assert policy.classify(path) is policy.ChangeTier.FORBIDDEN, path
