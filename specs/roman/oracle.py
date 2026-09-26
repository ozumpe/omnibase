"""Contract-local strategies and domain laws for the ``roman`` feature.

**Runs inside the gauntlet sandbox**, next to untrusted candidate code, so this
is standard-library plus Hypothesis only and imports nothing from ``sis/``.

POLICY-FORBIDDEN (``specs/`` is in ``sis.policy.GUARDRAIL_DIRS``).

**Named ``oracle.py`` by slot, not by nature.** For a Class-1 contract that file
holds the reference implementation a candidate must agree with. Class 2 has no
reference by definition — if a correct implementation already existed there would
be nothing to build — so what lives here is only the trusted, contract-local code
the gates resolve *first*: strategies that describe a valid input, and laws that
name this domain's own exports. There is deliberately no ``to_roman`` here; the
gate would then be comparing the candidate against an answer key, which is
exactly the Class-1 shape this contract is not.
"""

from typing import Any

from hypothesis import strategies as st

# The spec defines conversion over 1..3999 and nothing outside it.
MIN_VALUE = 1
MAX_VALUE = 3999


def in_range_values() -> Any:
    """Every integer the spec defines, as an args tuple.

    The whole domain is 3999 values, so the strategy covers it exactly — no
    sampling gap for a candidate to hide a size-conditional branch in, which is
    the cheat the sort contract's oracle was widened to catch. `min_value`/
    `max_value` rather than a filter so Hypothesis generates only valid inputs
    instead of discarding most of what it makes.
    """
    return st.tuples(st.integers(min_value=MIN_VALUE, max_value=MAX_VALUE))


def round_trip(args: tuple[Any, ...], output: Any, impl: Any) -> bool:
    """``from_roman(to_roman(n)) == n`` for every n the spec defines.

    The law that makes this contract worth building first: it is checkable
    without knowing what the right numeral *is*, so it catches a candidate that
    is self-consistently wrong on inputs nobody enumerated — precisely what the
    hand-written acceptance cases cannot do.

    Three arguments because it names a *sibling* export rather than re-invoking
    the entry point, which also makes it offline-only: applying it to live
    traffic would mean calling the candidate again on its own response.
    """
    return bool(impl.module.from_roman(output) == args[0])


def canonical_form(args: tuple[Any, ...], output: Any) -> bool:
    """No numeral repeats a symbol more than three times, and none uses IIII-style runs.

    Two-argument, so it is canary-compatible: unlike round-trip it judges the
    response alone. Stated separately from round-trip because a candidate can
    round-trip perfectly with a non-canonical encoding — ``IIII`` parses back to
    4 quite happily — and the spec asks for the canonical numeral, not merely a
    reversible one.
    """
    del args
    numeral = str(output)
    if not numeral:
        return False
    if any(symbol * 4 in numeral for symbol in "IXCM"):
        return False
    # D, L and V are never repeated at all: two of them is always a larger symbol.
    return not any(symbol * 2 in numeral for symbol in "DLV")
