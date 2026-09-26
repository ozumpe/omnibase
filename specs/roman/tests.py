"""Acceptance tests for the ``roman`` feature contract.

**Runs inside the gauntlet sandbox**, next to untrusted candidate code, so this
is standard-library only and imports nothing from ``sis/``. ``import target``
resolves to whichever candidate is being validated.

POLICY-FORBIDDEN (``specs/`` is in ``sis.policy.GUARDRAIL_DIRS``): this is the
exam, and the implementer must never be able to edit it.

These are **spec cases, not a test suite** — the difference matters. They are
the executable form of what the feature was asked to do, so each one should be
traceable to a sentence a human wrote, and they should stay small enough that a
reviewer reads them instead of skimming. The exhaustive, adversarial coverage
that would normally live here is the invariant gate's job (OMNI-18): a
round-trip property over generated inputs catches the cases nobody enumerated,
which is precisely what a hand-written list cannot do.
"""

import pytest
from target import from_roman, to_roman

# One example per rule the spec states, rather than per interesting integer.
CANONICAL = [
    (1, "I"),
    (4, "IV"),        # subtractive form, not IIII
    (9, "IX"),
    (14, "XIV"),
    (40, "XL"),
    (90, "XC"),
    (400, "CD"),
    (900, "CM"),
    (1987, "MCMLXXXVII"),
    (3999, "MMMCMXCIX"),   # the largest value the spec defines
]


@pytest.mark.parametrize(("value", "numeral"), CANONICAL)
def test_to_roman_produces_the_canonical_numeral(value: int, numeral: str) -> None:
    assert to_roman(value) == numeral


@pytest.mark.parametrize(("value", "numeral"), CANONICAL)
def test_from_roman_reads_the_canonical_numeral(numeral: str, value: int) -> None:
    assert from_roman(numeral) == value


def test_subtractive_pairs_are_required_not_optional() -> None:
    """The spec asks for canonical output, so IIII is wrong even though it reads as 4.

    Stated as its own case because it is the rule an implementation is most
    likely to get *nearly* right: a naive greedy conversion over the four
    ordinary symbols produces IIII and passes anything that only checks
    round-tripping.
    """
    assert to_roman(4) == "IV"
    assert to_roman(9) == "IX"
    assert to_roman(40) == "XL"


@pytest.mark.parametrize("value", [0, -1, 4000])
def test_values_outside_the_defined_range_are_rejected(value: int) -> None:
    """Roman numerals have no zero and no negative, and the spec stops at 3999.

    Rejecting loudly rather than returning "" or a made-up numeral: a silent
    wrong answer here would propagate into whatever consumes the feature.
    """
    with pytest.raises(ValueError):
        to_roman(value)


@pytest.mark.parametrize("numeral", ["", "IIII", "VV", "IC", "ABC", "MMMM"])
def test_malformed_numerals_are_rejected(numeral: str) -> None:
    """Only canonical numerals parse.

    ``IIII`` and ``VV`` are readable but non-canonical; ``IC`` is an invalid
    subtractive pair; ``MMMM`` exceeds the defined range. Accepting any of them
    would make ``from_roman`` a different function from the one specified.
    """
    with pytest.raises(ValueError):
        from_roman(numeral)


# Round-tripping the whole 1..3999 range used to live here, exhaustively. It has
# moved to the contract's `round_trip` invariant (OMNI-18), which is where this
# file's own docstring always said it belonged. Keeping both would have made the
# invariant gate decorative for this contract: acceptance would catch every
# violation first, and the demonstration that laws find what enumerated cases
# cannot would be true of every domain except the one shipped to show it.
