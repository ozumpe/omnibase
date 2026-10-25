"""Acceptance tests for the ``ca_price_persistence`` feature contract (OMNI-36).

**Runs inside the gauntlet sandbox**, next to untrusted candidate code, so this
is standard-library only and imports nothing from ``sis/``. ``import target``
resolves to whichever candidate is being validated.

POLICY-FORBIDDEN (``specs/`` is in ``sis.policy.GUARDRAIL_DIRS``).

The spec, one case per sentence: *next week's California retail price equals
this week's*, where "this week" is the latest week whose price the model has
been shown. The model is shown a state shaped as a backtest fixture's argument
(``as_of``, ``readings``, ``absent``), already resolved to what was known at
``as_of``: it does no resolution of its own.
"""

import pytest
from target import predict_next_week

CA = "PET.EMM_EPMRR_PTE_SCA_DPG.W"
US = "PET.EMM_EPMRR_PTE_NUS_DPG.W"
STOCKS = "PET.WGTSTP51.W"


def _reading(series: str, day: str, value: float, unit: str = "Dollars per Gallon") -> dict:
    return {"source": "eia", "series": series, "value": value, "unit": unit,
            "event_time": f"{day}T00:00:00-04:00", "known_at": f"{day}T21:00:00-04:00"}


def _state(*readings: dict, absent: tuple[dict, ...] = ()) -> dict:
    return {"as_of": "2022-10-05T17:00:00-04:00", "readings": list(readings),
            "absent": list(absent)}


def test_the_forecast_is_the_latest_california_price_shown() -> None:
    state = _state(_reading(CA, "2022-09-19", 5.42), _reading(CA, "2022-09-26", 5.99),
                   _reading(CA, "2022-10-03", 6.42))
    assert predict_next_week(state) == pytest.approx(6.42)


def test_order_in_the_list_does_not_decide_which_week_is_latest() -> None:
    state = _state(_reading(CA, "2022-10-03", 6.42), _reading(CA, "2022-09-19", 5.42))
    assert predict_next_week(state) == pytest.approx(6.42)


def test_other_series_are_not_the_california_price() -> None:
    state = _state(_reading(CA, "2022-09-26", 5.99), _reading(US, "2022-10-03", 3.91),
                   _reading(STOCKS, "2022-09-30", 26_000.0, "Thousand Barrels"))
    assert predict_next_week(state) == pytest.approx(5.99)


def test_a_week_stated_absent_is_skipped_not_read_as_zero() -> None:
    gap = {"source": "eia", "series": CA, "marker": "NA",
           "event_time": "2022-10-03T00:00:00-04:00", "known_at": "2022-10-05T17:00:00-04:00"}
    state = _state(_reading(CA, "2022-09-26", 5.99), absent=(gap,))
    assert predict_next_week(state) == pytest.approx(5.99)


def test_no_california_price_at_all_is_refused() -> None:
    with pytest.raises(ValueError):
        predict_next_week(_state(_reading(US, "2022-10-03", 3.91)))
