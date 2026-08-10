"""Tests for the InvariantGate (OMNI-18).

The gate's claim is that domain laws catch what enumerated examples cannot, so
the load-bearing test is exactly that: a candidate that satisfies every spec case
the contract lists and still violates a law on an input nobody wrote down.
"""

from __future__ import annotations

import importlib.util
import types
from dataclasses import replace

import pytest

from sis import gauntlet
from sis.contract import ROMAN, GateName, default_contract
from sis.episodic import gate_from_reason
from sis.invariant import (
    DEFAULT_INVARIANT_EXAMPLES,
    Impl,
    Invariant,
    bind,
    build_script,
    is_canary_compatible,
    needs_impl,
)
from sis.paths import INVARIANTS_PATH, PROJECT_ROOT
from sis.policy import ChangeTier, classify

# A correct implementation of the `roman` contract.
ROMAN_OK = '''\
"""Roman numeral conversion."""

_VALUES: list[tuple[int, str]] = [
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
    (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
]


def to_roman(value: int) -> str:
    """Return the canonical numeral for 1..3999."""
    if not 1 <= value <= 3999:
        raise ValueError(f"out of range: {value}")
    out: list[str] = []
    remaining = value
    for amount, symbol in _VALUES:
        while remaining >= amount:
            out.append(symbol)
            remaining -= amount
    return "".join(out)


def from_roman(numeral: str) -> int:
    """Parse a canonical numeral."""
    if not numeral:
        raise ValueError("empty numeral")
    total = 0
    index = 0
    for amount, symbol in _VALUES:
        while numeral[index:index + len(symbol)] == symbol:
            total += amount
            index += len(symbol)
    if index != len(numeral) or to_roman(total) != numeral:
        raise ValueError(f"not canonical: {numeral!r}")
    return total
'''

# Correct for every value the spec enumerates (1, 4, 9, 14, 40, 90, 400, 900,
# 1987, 3999) and wrong across a band none of them fall in. This is the whole
# point of the gate: acceptance cannot see it, a law can.
ROMAN_EVADES_ACCEPTANCE = ROMAN_OK.replace(
    '        raise ValueError(f"not canonical: {numeral!r}")\n    return total',
    '        raise ValueError(f"not canonical: {numeral!r}")\n'
    "    if 1000 <= total <= 1900:\n        return total + 1\n    return total",
)


def _shared() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("invariants", INVARIANTS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- declaration ----------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"name": ""}, "name must not be empty"),
        ({"strategy": ""}, "names no strategy"),
        ({"check": ""}, "names no predicate"),
    ],
)
def test_an_incoherent_invariant_is_rejected_at_declaration(
    kwargs: dict[str, str], match: str
) -> None:
    base = {"name": "round_trip", "strategy": "s", "check": "c"}
    with pytest.raises(ValueError, match=match):
        Invariant(**{**base, **kwargs})


def test_both_gate_profiles_include_the_invariant_gate() -> None:
    assert GateName.INVARIANT in ROMAN.gate_profile()
    assert GateName.INVARIANT in default_contract().gate_profile()


def test_the_invariant_module_is_guardrail_code() -> None:
    # A candidate able to edit a law could legislate itself correct.
    assert classify("sis/invariant.py") is ChangeTier.FORBIDDEN
    assert classify(INVARIANTS_PATH) is ChangeTier.FORBIDDEN
    assert classify("specs/roman/oracle.py") is ChangeTier.FORBIDDEN


def test_the_roman_contract_states_its_laws() -> None:
    names = {inv.name for inv in ROMAN.invariants}
    assert names == {"round_trip", "canonical"}
    assert ROMAN.invariant_examples == DEFAULT_INVARIANT_EXAMPLES


def test_acceptance_no_longer_duplicates_the_round_trip_law() -> None:
    """The exhaustive range check moved to the invariant gate.

    Keeping both would make the gate decorative for this contract: acceptance
    would catch every violation first, and the one shipped example of "laws find
    what enumerated cases cannot" would be the one domain where that is false.
    """
    source = (PROJECT_ROOT / "specs/roman/tests.py").read_text(encoding="utf-8")
    assert "for value in range(1, 4000)" not in source


# --- predicate shapes -----------------------------------------------------


def test_a_two_argument_predicate_can_also_judge_live_traffic() -> None:
    shared = _shared()
    assert not needs_impl(shared.sorted_permutation)
    assert is_canary_compatible(shared.sorted_permutation)


def test_a_three_argument_predicate_is_offline_only() -> None:
    # It would have to re-invoke the candidate on a production response, which
    # changes what production does.
    shared = _shared()
    assert needs_impl(shared.deterministic)
    assert not is_canary_compatible(shared.deterministic)


def test_binding_produces_the_shape_the_canary_consumes() -> None:
    shared = _shared()
    bound = bind(Invariant(name="sorted", strategy="integer_lists",
                           check="sorted_permutation"), shared)
    assert bound.name == "sorted"
    assert bound.check(([3, 1, 2],), [1, 2, 3]) is True
    assert bound.check(([3, 1, 2],), [3, 1, 2]) is False


def test_binding_an_offline_only_law_for_the_canary_fails_loudly() -> None:
    # Better here than one sample into a live rollout.
    shared = _shared()
    with pytest.raises(ValueError, match="cannot run against live traffic"):
        bind(Invariant(name="d", strategy="s", check="deterministic"), shared)


def test_binding_a_missing_predicate_fails() -> None:
    with pytest.raises(ValueError, match="no predicate"):
        bind(Invariant(name="x", strategy="s", check="nope"), _shared())


# --- the shared library ---------------------------------------------------


def test_sorted_permutation_admits_any_correct_sort_and_rejects_impostors() -> None:
    shared = _shared()
    assert shared.sorted_permutation(([3, 1, 2],), [1, 2, 3]) is True
    assert shared.sorted_permutation(([3, 1, 2],), [3, 1, 2]) is False  # unsorted
    assert shared.sorted_permutation(([3, 1, 2],), [1, 2]) is False     # dropped one
    assert shared.sorted_permutation(([3, 1, 2],), [1, 2, 9]) is False  # wrong elements


def test_non_negative_ignores_non_numbers() -> None:
    shared = _shared()
    assert shared.non_negative((), [0, 1, 2]) is True
    assert shared.non_negative((), [1, -1]) is False
    assert shared.non_negative((), ["a", "b"]) is True


def test_deterministic_and_idempotent_use_the_bound_entry() -> None:
    shared = _shared()
    impl = Impl(module=None, entry=sorted)
    assert shared.deterministic(([3, 1, 2],), [1, 2, 3], impl) is True
    assert shared.deterministic(([3, 1, 2],), [9, 9], impl) is False
    assert shared.idempotent(([3, 1, 2],), [1, 2, 3], impl) is True


def test_the_roman_laws_are_checkable_without_knowing_the_right_answer() -> None:
    spec = importlib.util.spec_from_file_location(
        "roman_oracle", PROJECT_ROOT / "specs/roman/oracle.py"
    )
    assert spec is not None and spec.loader is not None
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)

    assert oracle.canonical_form((4,), "IV") is True
    assert oracle.canonical_form((4,), "IIII") is False   # round-trips, not canonical
    assert oracle.canonical_form((10,), "VV") is False


# --- the script -----------------------------------------------------------


def test_the_script_prefers_a_contract_local_law_over_the_shared_one() -> None:
    script = build_script(
        candidate_path="/s/c.py", shared_path="/s/inv.py", oracle_path="/s/o.py",
        entry="f", plan=[], examples=10, seed=1,
    )
    assert script.index("getattr(oracle, name") < script.index("getattr(shared, name")


def test_the_generated_property_takes_no_default_arguments() -> None:
    """Hypothesis refuses @given on a function with defaults.

    The obvious way to capture loop variables is `def prop(args, _inv=inv)`, and
    it fails at runtime with an InvalidArgument that reads like a library bug
    rather than a closure mistake. Worth pinning so it is not reintroduced.
    """
    script = build_script(
        candidate_path="/s/c.py", shared_path="/s/inv.py", oracle_path=None,
        entry="f", plan=[], examples=10, seed=1,
    )
    assert "def prop(args):" in script


def test_the_example_database_is_disabled_in_the_sandbox() -> None:
    # The seed must be the only reproduction handle; a database would make a
    # replay depend on state the log does not carry.
    script = build_script(
        candidate_path="/s/c.py", shared_path="/s/inv.py", oracle_path=None,
        entry="f", plan=[], examples=10, seed=1,
    )
    assert "database=None" in script


# --- the gate, end to end -------------------------------------------------


def test_a_correct_candidate_passes_every_law() -> None:
    assert gauntlet.validate(ROMAN_OK, contract=ROMAN).passed


def test_a_candidate_that_satisfies_every_spec_case_can_still_break_a_law() -> None:
    """The reason this gate exists.

    ROMAN_EVADES_ACCEPTANCE is correct for all ten values the acceptance tests
    enumerate. Only a law over generated inputs finds the band where it is not.
    """
    result = gauntlet.validate(ROMAN_EVADES_ACCEPTANCE, contract=ROMAN, seed=7)
    assert not result.passed
    assert gate_from_reason(result.reason) == "invariant"
    assert "round_trip" in result.reason


def test_a_violation_reports_a_shrunk_counterexample() -> None:
    # The payoff over plain seeded random generation: 'args=(1000,)' is the
    # boundary of the defect, not whichever value happened to trip it.
    result = gauntlet.validate(ROMAN_EVADES_ACCEPTANCE, contract=ROMAN, seed=7)
    assert "args=(1000,)" in result.reason


def test_a_violation_is_reproducible_from_the_seed_alone() -> None:
    # Without this the recorded counterexample names something nobody can
    # regenerate, which is most of the value of recording it.
    first = gauntlet.validate(ROMAN_EVADES_ACCEPTANCE, contract=ROMAN, seed=7)
    second = gauntlet.validate(ROMAN_EVADES_ACCEPTANCE, contract=ROMAN, seed=7)
    assert first.reason == second.reason
    assert first.seed == second.seed == 7


def test_the_seed_reaches_the_reason_so_it_lands_in_the_episodic_log() -> None:
    result = gauntlet.validate(ROMAN_EVADES_ACCEPTANCE, contract=ROMAN, seed=4242)
    assert "seed=4242" in result.reason


def test_seeds_differ_between_runs_when_the_caller_does_not_pin_one() -> None:
    """A fixed seed would mean a fixed input set, which a candidate could learn.

    Checked on the context rather than by running the gate twice, so the test
    does not depend on two random draws differing.
    """
    seeds = {
        gauntlet.validate(ROMAN_EVADES_ACCEPTANCE, contract=ROMAN).seed
        for _ in range(3)
    }
    assert len(seeds) > 1


def test_an_unresolvable_law_is_a_harness_fault_not_the_candidates() -> None:
    broken = replace(
        ROMAN,
        invariants=(Invariant(name="x", strategy="no_such_strategy", check="round_trip"),),
    )
    result = gauntlet.validate(ROMAN_OK, contract=broken)
    assert not result.passed
    assert gate_from_reason(result.reason) == "harness"
    assert "no_such_strategy" in result.reason


def test_a_contract_with_no_laws_skips_the_gate() -> None:
    bare = replace(ROMAN, invariants=())
    assert gauntlet.validate(ROMAN_OK, contract=bare).passed


# --- episodic -------------------------------------------------------------


def test_the_offline_gate_is_distinct_from_the_canarys() -> None:
    # Conflating them would lose whether the sandbox or live traffic caught it,
    # which is the distinction the canary exists to add.
    assert gate_from_reason("invariant violated in sandbox (seed=1): {...}") == "invariant"
    # The canary's reason starts with the same two words. If the offline rule
    # ever loses its "in sandbox" qualifier it will shadow this one, and every
    # live violation will be filed as an offline one.
    assert gate_from_reason("invariant violated: capacity (3 of 300)") == "canary_invariant"


def test_an_invariant_timeout_is_still_a_timeout() -> None:
    assert gate_from_reason("invariant gate timed out") == "timeout"
