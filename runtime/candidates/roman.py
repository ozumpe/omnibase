"""Hand-written implementation of the ``roman`` feature contract (Class 2).

What the stub proposer (``SIS_PROPOSER=stub``, the offline/CI default) returns
for ``roman``, standing in for what a real LLM would build from the spec. Zero
API calls, so ``main.py --contract roman`` runs with no key (OMNI-147).

Unlike the Class-1 stubs this is not a faster version of anything: there is no
earlier version. It is what the spec asks for, and the gauntlet judges it only
on that (the acceptance cases, the round-trip and canonical-form laws).
"""

_SYMBOLS: tuple[tuple[int, str], ...] = (
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
    (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
)

MIN_VALUE = 1
MAX_VALUE = 3999


def to_roman(value: int) -> str:
    """The canonical numeral for *value*, 1 to 3999. Raises ValueError outside it."""
    if not MIN_VALUE <= value <= MAX_VALUE:
        raise ValueError(f"no roman numeral for {value}: the range is {MIN_VALUE}..{MAX_VALUE}")
    parts: list[str] = []
    for amount, symbol in _SYMBOLS:
        count, value = divmod(value, amount)
        parts.append(symbol * count)
    return "".join(parts)


def from_roman(numeral: str) -> int:
    """The value of a canonical numeral. Raises ValueError for anything else.

    Parsed greedily, then checked by converting back: only the canonical
    spelling of a value in range reads, so ``IIII``, ``VV`` and ``IC`` do not.
    """
    value, rest = 0, numeral
    for amount, symbol in _SYMBOLS:
        while rest.startswith(symbol):
            value += amount
            rest = rest[len(symbol):]
    if rest or not MIN_VALUE <= value <= MAX_VALUE or to_roman(value) != numeral:
        raise ValueError(f"not a canonical roman numeral: {numeral!r}")
    return value
