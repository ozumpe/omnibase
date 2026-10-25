"""Hand-written implementation of the ``ca_price_persistence`` contract (Class 2).

What the stub proposer returns for ``ca_price_persistence`` (OMNI-36), and the
predictor ``scripts/prediction_error.py`` runs when ``runtime/ca_price_persistence.py``
has not been built yet. It runs in the sandbox like any candidate.

Persistence: next week's California retail price equals the latest one shown.
The climatology baseline every later model must beat (D9), deliberately
trivial, and wrong in exactly the week a price jumps.
"""

import datetime
from typing import Any

CALIFORNIA = "PET.EMM_EPMRR_PTE_SCA_DPG.W"


def predict_next_week(state: dict[str, Any]) -> float:
    """Next week's California price: the latest week's price in *state*.

    *state* is what was known at ``state["as_of"]``, already resolved to the
    latest vintage per week, absences apart. Raises ValueError with no
    California price in it.
    """
    prices = [record for record in state["readings"] if record["series"] == CALIFORNIA]
    if not prices:
        raise ValueError("no California price is known yet")
    latest = max(prices, key=lambda record: datetime.datetime.fromisoformat(
        str(record["event_time"])))
    return float(latest["value"])
