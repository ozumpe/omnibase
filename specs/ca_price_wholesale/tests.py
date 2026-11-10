"""Acceptance tests for the ``ca_price_wholesale`` feature contract (OMNI-163).

**Runs in the harness**: ``import target`` resolves to a stand-in that calls
the candidate in its sandbox worker. Standard library only, nothing from
``sis/``.

POLICY-FORBIDDEN (``specs/`` is in ``sis.policy.GUARDRAIL_DIRS``).

The spec: *next week's California retail price, from what was known on
Thursday at 17:00 ET: the retail prices (California, Los Angeles, San
Francisco, US), PADD 5 gasoline stocks, the Los Angeles spot price, and the
date.* The state is shaped as a backtest fixture's argument (``as_of``,
``readings``, ``absent``), already resolved to what was known at ``as_of``.
Whether the forecast is good is the backtest gate's question, against
persistence on held-out weeks; these say what any answer must look like.
"""

import datetime
import math

import pytest
from target import predict_next_week

CA = "PET.EMM_EPMRR_PTE_SCA_DPG.W"
US = "PET.EMM_EPMRR_PTE_NUS_DPG.W"
SPOT = "PET.EER_EPMRR_PF4_Y05LA_DPG.D"
MONDAY = datetime.date(2022, 9, 26)


def _reading(series: str, day: datetime.date, value: float) -> dict:
    return {"source": "eia", "series": series, "value": value, "unit": "Dollars per Gallon",
            "event_time": f"{day.isoformat()}T00:00:00-04:00",
            "known_at": f"{day.isoformat()}T23:00:00-04:00"}


def _state(*, retail: float = 4.50, spot: float = 2.70, jump: float = 0.0,
           with_spot: bool = True) -> dict:
    """A year of steady prices; *jump* is added to the last five spot prices."""
    weeks = [MONDAY - datetime.timedelta(weeks=n) for n in range(53)]
    days = [MONDAY - datetime.timedelta(days=n) for n in range(371)]
    readings = [_reading(CA, week, retail) for week in weeks]
    readings += [_reading(US, week, retail - 1.0) for week in weeks]
    if with_spot:
        recent = [day for day in days if day.weekday() < 5][:5]
        readings += [_reading(SPOT, day, spot + (jump if day in recent else 0.0))
                     for day in days if day.weekday() < 5]
    return {"as_of": "2022-09-29T17:00:00-04:00", "readings": readings, "absent": []}


def _forecast(state: dict) -> float:
    value = predict_next_week(state)
    assert isinstance(value, int | float) and not isinstance(value, bool)
    assert math.isfinite(value)
    return float(value)


def test_a_steady_market_is_forecast_near_its_price() -> None:
    assert abs(_forecast(_state()) - 4.50) < 0.50


def test_a_rise_in_the_spot_price_does_not_lower_the_forecast() -> None:
    assert _forecast(_state(jump=0.50)) >= _forecast(_state())


def test_a_fall_in_the_spot_price_does_not_raise_the_forecast() -> None:
    assert _forecast(_state(jump=-0.50)) <= _forecast(_state())


def test_order_in_the_list_decides_nothing() -> None:
    state = _state(jump=0.30)
    shuffled = dict(state, readings=list(reversed(state["readings"])))
    assert _forecast(shuffled) == pytest.approx(_forecast(state))


def test_without_a_spot_price_there_is_still_a_forecast() -> None:
    # An outage, or a state that never had one: the retail price is enough to
    # say something, and a missing input is no reason to stop.
    assert abs(_forecast(_state(with_spot=False)) - 4.50) < 0.50


def test_no_california_price_at_all_is_refused() -> None:
    state = _state()
    state["readings"] = [r for r in state["readings"] if r["series"] != CA]
    with pytest.raises(ValueError):
        predict_next_week(state)
