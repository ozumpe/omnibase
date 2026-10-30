"""Contract-local laws and the baseline for the ``ca_price_wholesale`` feature (OMNI-163).

**Runs in the harness**, beside the backtest replay and the invariant gate, so
this is standard-library plus Hypothesis only and imports nothing from
``sis/``.

POLICY-FORBIDDEN (``specs/`` is in ``sis.policy.GUARDRAIL_DIRS``).

No reference implementation, as for every Class-2 contract. What is here:

- ``persistence``, the baseline the skill check names: next week's price is
  the latest one shown. A forecast must beat it on average over the held-out
  weeks by the contract's margin (``BacktestSkill``). It runs in the harness on
  its own copy of each episode's arguments, never in the candidate's worker.
- ``plausible_price``, the law the generated fuel scenarios are checked
  against (``fuel_scenario_states`` in ``specs/invariants.py``). A scenario may
  reject a candidate; it is never evidence that one forecasts well (D4).
"""

import datetime
from typing import Any

CALIFORNIA = "PET.EMM_EPMRR_PTE_SCA_DPG.W"
# California's plausible range as the EIA sensor declares it ($/gal).
LOW, HIGH = 0.5, 15.0


def persistence(state: dict[str, Any]) -> float:
    """The baseline: the latest California price in *state*."""
    prices = [r for r in state["readings"] if r["series"] == CALIFORNIA]
    latest = max(prices, key=lambda r: datetime.datetime.fromisoformat(str(r["event_time"])))
    return float(latest["value"])


def plausible_price(args: tuple[Any, ...], output: Any) -> bool:
    """A forecast is a finite price in California's plausible range."""
    del args
    return isinstance(output, int | float) and not isinstance(output, bool) \
        and LOW <= float(output) <= HIGH
