"""Hand-written implementation of the ``ca_price_wholesale`` contract (Class 2).

What the stub proposer returns for ``ca_price_wholesale`` (OMNI-163). It runs
in the sandbox like any candidate.

Next week's California retail price from the wholesale price: retail follows
the LA spot price with a lag, and a retail margin far from its usual level
tends back towards it. The change from this week's price is a linear function
of

- the change in the spot price (the mean of the last five days shown, minus the
  mean of the five before),
- the retail margin over spot now, less its mean over the past year,
- the season (two harmonics of the day of the year).

The coefficients were fitted by least squares on 2010-2019, before every
held-out week (OMNI-163's spike: mean absolute error $0.051 against
persistence's $0.068 on 2020-2026). They are statistics derived from the data,
not the data. Without enough spot prices in the state (an outage, or a
generated scenario that has none) the forecast is persistence.
"""

import datetime
import math
from statistics import fmean
from typing import Any

CALIFORNIA = "PET.EMM_EPMRR_PTE_SCA_DPG.W"
SPOT = "PET.EER_EPMRR_PF4_Y05LA_DPG.D"
DAYS = 5            # spot prices in one window
YEAR = 52           # weeks of margin history the usual margin is taken over
ENOUGH = 40         # weeks of it needed before the margin is trusted
# Intercept, spot change, margin anomaly, sin/cos of the season, sin/cos of twice it.
COEFFICIENTS = (0.00777, 0.11837, -0.17073, 0.01399, 0.00172, 0.01171, -0.00725)


def _day(record: dict[str, Any]) -> datetime.date:
    return datetime.datetime.fromisoformat(str(record["event_time"])).date()


def _series(state: dict[str, Any], series: str) -> list[tuple[datetime.date, float]]:
    return sorted((_day(r), float(r["value"])) for r in state["readings"]
                  if r["series"] == series)


def _window(spot: list[tuple[datetime.date, float]], until: datetime.date,
            skip: int = 0) -> float | None:
    """The mean of *DAYS* spot prices on or before *until*, *skip* windows back."""
    held = [value for day, value in spot if day <= until]
    end = len(held) - skip * DAYS
    window = held[max(0, end - DAYS):end]
    return fmean(window) if len(window) == DAYS else None


def predict_next_week(state: dict[str, Any]) -> float:
    """Next week's California price, from what was known at ``state["as_of"]``.

    Raises ValueError with no California price in *state*.
    """
    retail = _series(state, CALIFORNIA)
    if not retail:
        raise ValueError("no California price is known yet")
    week, price = retail[-1]
    spot = _series(state, SPOT)
    now, before = _window(spot, week), _window(spot, week, skip=1)
    margins = []
    for past_week, past_price in retail[-1 - YEAR:-1]:
        level = _window(spot, past_week)
        if level is not None:
            margins.append(past_price - level)
    if now is None or before is None or len(margins) < ENOUGH:
        return price
    season = week.timetuple().tm_yday / 365.25 * 2 * math.pi
    features = (1.0, now - before, (price - now) - fmean(margins),
                math.sin(season), math.cos(season), math.sin(2 * season), math.cos(2 * season))
    return price + sum(c * x for c, x in zip(COEFFICIENTS, features, strict=True))
