"""Shared invariant predicates and strategies — the domain-agnostic laws.

**Runs inside the gauntlet sandbox**, next to untrusted candidate code, so this
is standard-library plus Hypothesis only and imports nothing from ``sis/``.

POLICY-FORBIDDEN (``specs/`` is in ``sis.policy.GUARDRAIL_DIRS``). An invariant
decides whether a candidate obeys the domain's laws, so a candidate able to edit
one could simply legislate itself correct.

Two shapes, and the difference decides where a law can run (see
``sis.invariant``)::

    check(args, output)        -> bool     # also usable on live traffic
    check(args, output, impl)  -> bool     # offline only; impl.module, impl.entry

``args`` is the argument tuple the strategy produced; ``output`` is what the
candidate returned for it.

**What belongs here versus in a contract's own oracle.** These are laws that hold
regardless of domain — a sorted output is a sorted permutation whether it is
prices or vehicle counts. Laws that name a domain's own vocabulary ("cargo is
conserved", "``from_roman`` inverts ``to_roman``") belong beside that contract,
where they resolve first. The engine never learns a domain's words.

The catalogue below is deliberately short. `docs/CLASS2_CONTRACT.md` argues the
per-domain cost is small because invariants cluster into reusable *kinds* —
conservation, capacity, monotonicity, and these — so a new project assembles a
contract from a catalogue far more than it authors one. Adding a genuinely new
kind here is a one-time cost every later project inherits.
"""

from typing import Any

from hypothesis import strategies as st

# --- strategies -----------------------------------------------------------
# Factories, not strategies: called once per gate run, so a strategy that is
# expensive to construct is not rebuilt per example.


def small_integers() -> Any:
    """Non-negative ints in a range wide enough to cross typical branch points."""
    return st.tuples(st.integers(min_value=0, max_value=10_000))


def integer_lists() -> Any:
    """Lists of ints, spanning empty through large.

    **The length range is load-bearing, not decorative.** The sort contract's
    oracle records what happens when it is not: a candidate reading
    ``return v if len(v) > 500 else sorted(v)`` — silently unsorted on large
    inputs — passed every gate, because the broken branch was never reached. An
    invariant can only catch what the distribution covers.
    """
    return st.tuples(
        st.lists(st.integers(min_value=-500, max_value=500), min_size=0, max_size=1200)
    )


# --- predicates: canary-compatible (two arguments) ------------------------


def sorted_permutation(args: tuple[Any, ...], output: Any) -> bool:
    """Output is ordered and contains exactly the input's elements.

    The canonical example of why invariants beat golden outputs: it admits any
    correct sort while rejecting "return the input" and "return a sorted list of
    something else", neither of which a fixed expected-value test would catch on
    an input it did not enumerate.
    """
    original = list(args[0])
    result = list(output)
    if len(result) != len(original):
        return False
    if any(result[i] > result[i + 1] for i in range(len(result) - 1)):
        return False
    return sorted(original) == sorted(result)


def non_negative(args: tuple[Any, ...], output: Any) -> bool:
    """Every number in the output is >= 0. Quantities, counts, costs, durations."""
    del args
    values = output if isinstance(output, list | tuple) else [output]
    return all(v >= 0 for v in values if isinstance(v, int | float))


# --- predicates: offline only (three arguments) ---------------------------


def deterministic(args: tuple[Any, ...], output: Any, impl: Any) -> bool:
    """The same input produces the same output twice.

    Offline only, and worth stating why it *has* to be: checking it means calling
    the candidate a second time, which against live traffic would double
    production's work and change what it does.

    Note this is the law a ``Determinism.DETERMINISTIC`` contract asserts about
    its own implementation, so it is a reasonable default for any such contract
    — and exactly the law a STOCHASTIC one must not declare.
    """
    return bool(impl.entry(*args) == output)


def idempotent(args: tuple[Any, ...], output: Any, impl: Any) -> bool:
    """Applying the operation to its own output changes nothing.

    Only meaningful where the output is itself a valid input (sorting,
    normalising, canonicalising); a contract whose entry maps between different
    types should not declare it.
    """
    del args
    return bool(impl.entry(output) == output)
