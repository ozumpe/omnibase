"""Tests for FeatureContract and the contract-selected gate profile (OMNI-17).

The refactor's claim is that a Class-1 optimisation and a Class-2 feature run
through one ``validate()`` with different profiles. So these tests care about
three things: that the profiles differ in the ways they should, that a feature
can be built and judged with no reference implementation anywhere, and that the
seed requirement is the *only* place determinism reaches the gate stack.
"""

from __future__ import annotations

import pathlib
import textwrap
from dataclasses import replace

import pytest

from sis import gauntlet
from sis.contract import (
    ROMAN,
    Contract,
    Determinism,
    FeatureContract,
    GateName,
    OptimizationContract,
    default_contract,
)
from sis.episodic import gate_from_reason
from sis.policy import ChangeTier, classify

# A correct, fully typed implementation of the `roman` contract. Written here
# rather than in runtime/ because a Class-2 feature has no starting file --
# "no pre-existing correct version" is the defining property of the class.
ROMAN_IMPL = textwrap.dedent(
    '''\
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
        """Parse a canonical numeral. Rejects anything non-canonical."""
        if not numeral:
            raise ValueError("empty numeral")
        total = 0
        index = 0
        for amount, symbol in _VALUES:
            while numeral[index:index + len(symbol)] == symbol:
                total += amount
                index += len(symbol)
        if index != len(numeral):
            raise ValueError(f"not a canonical numeral: {numeral!r}")
        if to_roman(total) != numeral:
            raise ValueError(f"not a canonical numeral: {numeral!r}")
        return total
    '''
)


# --- the profiles ---------------------------------------------------------


def test_a_feature_contract_omits_the_gates_that_need_a_reference() -> None:
    """No no-op and no differential/benchmark, and both omissions are load-bearing.

    A feature being built for the first time has nothing to be identical *to*,
    and both halves of the differential gate presuppose a reference that can be
    evaluated on demand -- exactly what Class 2 does not have.
    """
    profile = ROMAN.gate_profile()
    assert GateName.NOOP not in profile
    assert GateName.DIFFERENTIAL_BENCHMARK not in profile
    assert GateName.ACCEPTANCE in profile
    assert GateName.INTERFACE in profile


def test_an_optimization_contract_keeps_the_full_class_one_stack() -> None:
    profile = default_contract().gate_profile()
    assert profile == (
        GateName.AST, GateName.NOOP, GateName.MYPY, GateName.INTERFACE,
        GateName.ACCEPTANCE, GateName.INVARIANT, GateName.BACKTEST,
        GateName.DIFFERENTIAL_BENCHMARK,
    )


def test_both_classes_satisfy_the_one_contract_protocol() -> None:
    # The protocol is what lets validate() be written once. Assigning proves
    # structural conformance at type-check time; the call proves it at runtime.
    for contract in (default_contract(), ROMAN):
        spec: Contract = contract
        assert spec.gate_profile()
        assert spec.entry in spec.public_api


def test_every_gate_a_profile_can_name_has_an_implementation() -> None:
    # The registry is the only place a gate implementation is named, so a
    # profile cannot ask for one that does not exist -- but only if the table
    # stays complete as GateName grows.
    assert set(GateName) == set(gauntlet._GATES)


# --- declaration-time validation -----------------------------------------


def test_an_entry_point_outside_the_public_api_is_rejected() -> None:
    # The interface gate only checks what public_api lists, so an entry missing
    # from it would never be checked at all.
    with pytest.raises(ValueError, match="not in public_api"):
        FeatureContract(
            name="x", spec_ref="CONF-1", entry="build", entry_module="runtime/x.py",
            acceptance_tests="specs/x/tests.py", public_api=("something_else",),
        )


def test_a_feature_contract_defaults_to_deterministic() -> None:
    # Determinism is an axis with a safe default, not a ladder: a contract that
    # says nothing behaves exactly as it does today, permanently.
    assert ROMAN.determinism is Determinism.DETERMINISTIC
    assert default_contract().determinism is Determinism.DETERMINISTIC


def test_the_contract_layer_is_guardrail_code() -> None:
    """A loop able to edit a contract could return an empty gate profile.

    `sis/policy.py`'s docstring always named contracts as FORBIDDEN; the list
    only protected `specs/` until OMNI-17 made the profile itself a contract
    decision.
    """
    for path in ("sis/contract.py", "sis/backtest.py", "sis/clock.py",
                 "sis/gauntlet.py", "specs/roman/tests.py"):
        assert classify(path) is ChangeTier.FORBIDDEN, path
    # ...and the feature's implementation module is the one thing it may write.
    assert classify(ROMAN.entry_module) is ChangeTier.SOFT


# --- the interface gate ---------------------------------------------------


def _interface(
    tmp_path: pathlib.Path, spec: Contract, source: str
) -> gauntlet.Result | None:
    candidate = tmp_path / "target.py"
    candidate.write_text(source, encoding="utf-8")
    (tmp_path / "sitecustomize.py").write_text(gauntlet._NETWORK_GUARD, encoding="utf-8")
    ctx = gauntlet._GateContext(
        contract=spec, code_str=source, tmp=tmp_path, tmpdir=str(tmp_path),
        env=gauntlet._sandbox_env(home=str(tmp_path), pythonpath=str(tmp_path)),
        candidate=candidate,
    )
    return gauntlet._gate_interface(ctx)


def test_the_interface_gate_accepts_a_complete_public_api(tmp_path: pathlib.Path) -> None:
    assert _interface(tmp_path, ROMAN, ROMAN_IMPL) is None


def test_a_partial_public_api_is_rejected_and_names_what_is_missing(
    tmp_path: pathlib.Path,
) -> None:
    # Half an API is the failure mode this gate exists for: without it, the
    # missing symbol surfaces as a wall of acceptance errors that never says
    # "you did not export from_roman".
    only_one = "def to_roman(value: int) -> str:\n    return 'I'\n"
    result = _interface(tmp_path, ROMAN, only_one)
    assert result is not None
    assert "from_roman" in result.reason
    assert gate_from_reason(result.reason) == "interface"


def test_a_non_callable_entry_point_is_rejected(tmp_path: pathlib.Path) -> None:
    # Exporting the *name* is not enough; a module-level constant would satisfy
    # a naive hasattr check and then fail confusingly at call time.
    shadowed = "to_roman = 'I'\ndef from_roman(numeral: str) -> int:\n    return 1\n"
    result = _interface(tmp_path, ROMAN, shadowed)
    assert result is not None and "not callable" in result.reason


# --- determinism reaches exactly one gate --------------------------------


def _stochastic(entry: str = "to_roman") -> FeatureContract:
    return replace(ROMAN, determinism=Determinism.STOCHASTIC, entry=entry)


def test_a_stochastic_contract_requires_a_seeded_entry_point(
    tmp_path: pathlib.Path,
) -> None:
    """Without a seed the gauntlet cannot reproduce a failure.

    Every distributional gate downstream would then be measuring noise it
    cannot distinguish from a real regression -- which is why this is the one
    place determinism changes the gate stack rather than a comparator.
    """
    result = _interface(tmp_path, _stochastic(), ROMAN_IMPL)
    assert result is not None
    assert "seed" in result.reason
    assert gate_from_reason(result.reason) == "interface"


def test_a_seeded_entry_point_satisfies_a_stochastic_contract(
    tmp_path: pathlib.Path,
) -> None:
    seeded = ROMAN_IMPL.replace(
        "def to_roman(value: int) -> str:",
        "def to_roman(value: int, seed: int = 0) -> str:",
    )
    assert _interface(tmp_path, _stochastic(), seeded) is None


def test_determinism_does_not_change_which_gates_run() -> None:
    # The whole argument for determinism being a field rather than a third
    # contract class: the profile is identical, only comparators differ.
    assert _stochastic().gate_profile() == ROMAN.gate_profile()
    stochastic_opt = replace(default_contract(), determinism=Determinism.STOCHASTIC)
    assert stochastic_opt.gate_profile() == default_contract().gate_profile()


def test_a_stochastic_optimisation_contract_is_expressible() -> None:
    # The cell a "Class 3" would have nowhere to put: a slow Monte Carlo
    # simulation is a Class-1 optimisation *and* stochastic.
    spec = replace(default_contract(), determinism=Determinism.STOCHASTIC)
    assert isinstance(spec, OptimizationContract)
    assert GateName.DIFFERENTIAL_BENCHMARK in spec.gate_profile()


# --- the acceptance criterion: a feature, end to end ----------------------


def test_a_feature_built_from_its_spec_passes_the_class_two_profile() -> None:
    """One trivial feature, judged with no reference implementation anywhere.

    Nothing in this path compares against a prior version or a faster one: the
    verdict comes entirely from the contract's human-authored acceptance tests
    in POLICY-FORBIDDEN `specs/`.
    """
    result = gauntlet.validate(ROMAN_IMPL, contract=ROMAN)
    assert result.passed, result.reason
    # No benchmark ran, so there is no latency to report -- and that is correct
    # rather than a gap: "faster" is not what makes a feature right.
    assert result.latency_seconds is None


def test_a_feature_that_misreads_its_spec_is_rejected_at_acceptance() -> None:
    # Greedy conversion without the subtractive pairs: reads as 4, is not IV.
    # Exactly the near-miss the spec case calls out.
    naive = ROMAN_IMPL.replace('(900, "CM"), ', "").replace('(400, "CD"), ', "")
    naive = naive.replace('(90, "XC"), ', "").replace('(40, "XL"), ', "")
    naive = naive.replace('(9, "IX"), ', "").replace('(4, "IV"), ', "")
    result = gauntlet.validate(naive, contract=ROMAN)
    assert not result.passed
    assert gate_from_reason(result.reason) == "acceptance"


def test_a_feature_with_no_acceptance_tests_is_a_harness_fault() -> None:
    # Fails closed -- an unrun correctness gate must never read as a pass --
    # but says which side is at fault.
    broken = replace(ROMAN, acceptance_tests="specs/nope/tests.py")
    result = gauntlet.validate(ROMAN_IMPL, contract=broken)
    assert not result.passed
    assert gate_from_reason(result.reason) == "harness"


def test_validating_a_feature_never_reads_the_entry_module_from_disk() -> None:
    """`runtime/roman.py` does not exist, and the cycle must not need it to.

    A Class-2 contract points at where the implementer *will* write. Resolving
    a baseline from that path would fail every first-ever cycle for a file
    whose absence is the entire point.
    """
    assert not pathlib.Path(ROMAN.target_file).exists()
    assert gauntlet.validate(ROMAN_IMPL, contract=ROMAN).passed
