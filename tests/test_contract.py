"""Tests for the target contract (pure — no Ray, no sandbox)."""

import random
from dataclasses import replace

import pytest

from sis.contract import (
    DEFAULT_CONTRACTS,
    SUM_OF_DIVISORS,
    OptimizationContract,
    default_contract,
)
from sis.paths import PROJECT_ROOT
from sis.policy import ChangeTier, classify


def test_the_bootstrap_contract_points_at_files_that_exist() -> None:
    # A contract whose oracle or tests are missing degrades to a harness
    # failure at validate() time, which is a slow way to find a typo.
    spec = default_contract()
    for path in (spec.target_file, spec.oracle_file, spec.tests_file):
        assert (PROJECT_ROOT / path).exists(), path


def test_the_contract_is_forbidden_and_the_target_is_soft() -> None:
    # The whole point of the split: the SWE may write the target and may never
    # write the exam. If this inverts, every downstream gate is theatre.
    spec = default_contract()
    assert classify(spec.target_path) is ChangeTier.SOFT
    assert classify(spec.oracle_path) is ChangeTier.FORBIDDEN
    assert classify(spec.tests_path) is ChangeTier.FORBIDDEN


def test_every_registered_contract_is_protected() -> None:
    # Holds for the whole registry, not just the one we remembered to check —
    # so adding a contract can't quietly ship a writable exam.
    for spec in DEFAULT_CONTRACTS:
        assert classify(spec.oracle_path) is ChangeTier.FORBIDDEN, spec.name
        assert classify(spec.tests_path) is ChangeTier.FORBIDDEN, spec.name


def test_contracts_are_frozen() -> None:
    # A contract the running loop could mutate is not a contract.
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError is a dataclass detail
        SUM_OF_DIVISORS.max_latency_ratio = 0.5  # type: ignore[misc]


@pytest.mark.parametrize("ratio", [0.0, -0.1, 1.5])
def test_an_impossible_margin_is_rejected(ratio: float) -> None:
    # 1.0 means "no slower"; above that would be asking for a regression, and
    # 0.0 is unsatisfiable. Fail at construction, not at benchmark time.
    with pytest.raises(ValueError, match="max_latency_ratio"):
        replace(default_contract(), max_latency_ratio=ratio)


def test_a_contract_with_no_trials_is_rejected() -> None:
    # diff_trials=0 would silently disable differential correctness — the
    # anti-gaming gate — while still reporting a pass.
    with pytest.raises(ValueError, match="diff_trials"):
        replace(default_contract(), diff_trials=0)


def test_paths_resolve_against_the_project_root() -> None:
    spec = OptimizationContract(
        name="x", entry="f", target_path="runtime/target.py",
        oracle_path="specs/x/oracle.py", tests_path="specs/x/tests.py",
    )
    assert spec.oracle_file == str(PROJECT_ROOT / "specs/x/oracle.py")


# --- oracle loading + the stub answer (OMNI-6) ----------------------------


def test_load_oracle_exposes_the_reference_and_inputs() -> None:
    # The proposer inspects the oracle to build its prompt, so the contract has
    # to be able to import it -- not just hand its path to the sandbox.
    oracle = default_contract().load_oracle()
    assert oracle.reference(6) == 12  # 1+2+3+6
    assert oracle.BENCH_INPUTS
    assert oracle.random_input(random.Random(0))


def test_load_oracle_on_a_missing_path_raises() -> None:
    broken = replace(default_contract(), oracle_path="specs/nope/oracle.py")
    with pytest.raises(Exception):  # noqa: B017 - FileNotFoundError or RuntimeError
        broken.load_oracle()


def test_the_bootstrap_contract_has_a_stub_candidate() -> None:
    # SIS_PROPOSER=stub is the offline/CI default, so the bootstrap contract
    # must be runnable without an API key.
    spec = default_contract()
    assert spec.stub_candidate_path is not None
    assert (PROJECT_ROOT / spec.stub_candidate_path).exists()
