"""Contract-local laws for the ``ca_price_persistence`` feature (OMNI-36).

**Runs inside the gauntlet sandbox**, next to untrusted candidate code, so this
is standard-library plus Hypothesis only and imports nothing from ``sis/``.

POLICY-FORBIDDEN (``specs/`` is in ``sis.policy.GUARDRAIL_DIRS``).

No reference implementation, as for every Class-2 contract: the laws say what a
next-week California price must look like, and the inputs come from the pinned
scenario generator (``fuel_scenario_states`` in ``specs/invariants.py``). A
scenario may reject a candidate here; it is never evidence that one forecasts
well (D4). How well it forecasts is the prediction-error report's question
(``sis.prediction_error``), asked of recorded data.
"""

from typing import Any

# California's plausible range as the EIA sensor declares it ($/gal).
LOW, HIGH = 0.5, 15.0


def plausible_price(args: tuple[Any, ...], output: Any) -> bool:
    """A forecast is a finite price in California's plausible range."""
    del args
    return isinstance(output, int | float) and not isinstance(output, bool) \
        and LOW <= float(output) <= HIGH
