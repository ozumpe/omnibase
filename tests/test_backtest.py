"""Tests for the backtest gate (OMNI-19).

Split deliberately: the parsing/comparison logic is pure and tested directly,
the sandbox plumbing is tested through ``_backtest_gate`` with a hand-built temp
dir, and only two cases pay for a full ``validate()`` run — that path spends real
time in mypy and pytest, so exercising every harness fault through it would make
the suite slow without testing anything the cheaper level misses.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import types
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from sis import gauntlet
from sis.backtest import (
    DEFAULT_TOLERANCE,
    FIXTURE_SCHEMA,
    Backtest,
    Split,
    build_script,
    holdout,
    parse_expectation,
    parse_fixture,
)
from sis.contract import OptimizationContract, default_contract
from sis.episodic import gate_from_reason
from sis.paths import COMPARATORS_PATH, PROJECT_ROOT
from sis.policy import ChangeTier, classify

CANDIDATE_SOURCE = (PROJECT_ROOT / "runtime/candidates/optimised_target.py").read_text(
    encoding="utf-8"
)


def _comparators() -> types.ModuleType:
    """Import specs/comparators.py the way the sandbox does — by path."""
    spec = importlib.util.spec_from_file_location("comparators", COMPARATORS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fixture(path: pathlib.Path, args: list[Any], *, event_time: str | None = None) -> str:
    body: dict[str, Any] = {"schema": FIXTURE_SCHEMA, "args": args}
    if event_time is not None:
        body["event_time"] = event_time
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def _write_expect(path: pathlib.Path, value: Any) -> str:
    path.write_text(
        json.dumps({"schema": FIXTURE_SCHEMA, "value": value}), encoding="utf-8"
    )
    return str(path)


# --- fixture parsing ------------------------------------------------------


def test_a_fixture_round_trips_with_its_event_time() -> None:
    raw = json.dumps(
        {"schema": FIXTURE_SCHEMA, "args": [6], "event_time": "2026-01-15T00:00:00Z"}
    )
    fixture = parse_fixture(raw, where="f.json")
    assert fixture.args == [6]
    # A parsed instant, not the string it was written as (OMNI-23); the
    # timezone rules live in sis.clock and are tested in tests/test_clock.py.
    assert fixture.event_time == datetime(2026, 1, 15, tzinfo=UTC)


def test_event_time_is_optional_today_but_part_of_the_schema() -> None:
    # Nothing records it yet (OMNI-23 owns the Clock port). The field exists now
    # so fixtures written today stay replayable later -- history does not come
    # round again, so a fixture captured without it is capital destroyed.
    fixture = parse_fixture(
        json.dumps({"schema": FIXTURE_SCHEMA, "args": [1]}), where="f.json"
    )
    assert fixture.event_time is None


def test_an_unknown_schema_version_is_refused_rather_than_guessed_at() -> None:
    raw = json.dumps({"schema": 99, "args": [1]})
    with pytest.raises(ValueError, match="unsupported fixture schema"):
        parse_fixture(raw, where="f.json")


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "expected a JSON object"),
        (json.dumps({"schema": FIXTURE_SCHEMA}), "'args' must be a list"),
        (json.dumps({"schema": FIXTURE_SCHEMA, "args": {}}), "'args' must be a list"),
        (
            json.dumps({"schema": FIXTURE_SCHEMA, "args": [], "event_time": 17}),
            "'event_time' must be an ISO-8601 string",
        ),
    ],
)
def test_malformed_fixtures_name_the_file_they_came_from(raw: str, match: str) -> None:
    with pytest.raises(ValueError, match=match) as exc:
        parse_fixture(raw, where="specs/x/q1.json")
    assert "specs/x/q1.json" in str(exc.value)


def test_an_expectation_carries_its_recorded_value() -> None:
    assert parse_expectation(
        json.dumps({"schema": FIXTURE_SCHEMA, "value": 12}), where="e.json"
    ) == 12


def test_an_expectation_may_record_a_falsy_outcome() -> None:
    # `if not data.get("value")` would reject a recorded 0 / False / [] -- all
    # of which are perfectly good outcomes. Presence, not truthiness.
    for recorded in (0, False, [], "", None):
        assert parse_expectation(
            json.dumps({"schema": FIXTURE_SCHEMA, "value": recorded}), where="e.json"
        ) == recorded


def test_an_expectation_without_a_value_is_a_malformed_file() -> None:
    with pytest.raises(ValueError, match="missing 'value'"):
        parse_expectation(json.dumps({"schema": FIXTURE_SCHEMA}), where="e.json")


# --- the Backtest declaration --------------------------------------------


def test_a_backtest_defaults_to_the_holdout_split() -> None:
    # The strict side is the safe default: a fixture that silently defaulted to
    # `train` would be quietly available to a proposer.
    assert Backtest(name="q1", fixture="a.json", expect="b.json").split is Split.HOLDOUT


def test_holdout_selects_only_the_fixtures_whose_answers_must_stay_hidden() -> None:
    train = Backtest(name="t", fixture="a", expect="b", split=Split.TRAIN)
    held = Backtest(name="h", fixture="c", expect="d", split=Split.HOLDOUT)
    assert holdout((train, held)) == (held,)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"name": ""}, "name must not be empty"),
        ({"tolerance": -0.1}, "tolerance must be >= 0"),
        ({"compare": ""}, "names no comparator"),
    ],
)
def test_an_incoherent_backtest_is_rejected_at_declaration(
    kwargs: dict[str, Any], match: str
) -> None:
    base = {"name": "q1", "fixture": "a.json", "expect": "b.json"}
    with pytest.raises(ValueError, match=match):
        Backtest(**{**base, **kwargs})  # type: ignore[arg-type]


def test_duplicate_backtest_names_in_one_contract_are_rejected() -> None:
    # Names identify a fixture in the episodic log; two "q1"s make a rejection
    # untraceable to the episode that caused it.
    duplicate = (
        Backtest(name="q1", fixture="a.json", expect="b.json"),
        Backtest(name="q1", fixture="c.json", expect="d.json"),
    )
    with pytest.raises(ValueError, match="duplicate backtest names"):
        replace(default_contract(), backtests=duplicate)


def test_shipped_contracts_declare_no_backtests() -> None:
    # Both shipped targets are pure functions with no history to reproduce.
    # Inventing fixtures for them would make the gate look exercised while
    # testing nothing -- the first real fixtures arrive with a modelled target.
    from sis.contract import DEFAULT_CONTRACTS

    assert all(c.backtests == () for c in DEFAULT_CONTRACTS)


def test_every_contract_artifact_lives_where_the_loop_cannot_write_it() -> None:
    """The exam -- oracle, acceptance tests, and any fixtures -- must be FORBIDDEN.

    A fixture outside ``specs/`` would be loop-writable, which means a candidate
    could rewrite the history it is judged against. This is the same failure L5
    fixed for the reference oracle, and it is worth pinning rather than trusting
    that whoever adds the next contract remembers.
    """
    from sis.contract import DEFAULT_CONTRACTS

    for contract in DEFAULT_CONTRACTS:
        assert classify(contract.oracle_path) is ChangeTier.FORBIDDEN
        assert classify(contract.tests_path) is ChangeTier.FORBIDDEN
        for backtest in contract.backtests:
            assert classify(backtest.fixture) is ChangeTier.FORBIDDEN
            assert classify(backtest.expect) is ChangeTier.FORBIDDEN
    assert classify(COMPARATORS_PATH) is ChangeTier.FORBIDDEN


# --- comparators ----------------------------------------------------------


def test_exact_ignores_tolerance() -> None:
    comparators = _comparators()
    assert comparators.exact(5, 5, 0.9)[0] is True
    assert comparators.exact(5, 6, 0.9)[0] is False


def test_within_tolerance_accepts_a_close_enough_number() -> None:
    comparators = _comparators()
    ok, detail = comparators.within_tolerance(102.0, 100.0, 0.05)
    assert ok is True, detail


def test_within_tolerance_rejects_a_number_outside_the_margin_and_says_by_how_much() -> None:
    comparators = _comparators()
    ok, detail = comparators.within_tolerance(120.0, 100.0, 0.05)
    assert ok is False
    # The detail lands in the episodic log; "did not reproduce history" alone
    # would not be debuggable.
    assert "20" in detail and "5.0%" in detail


def test_a_recorded_zero_admits_only_zero() -> None:
    # Relative tolerance around zero is zero. Documented rather than fudged:
    # a domain needing an absolute floor supplies its own comparator, instead
    # of every other fixture silently loosening.
    comparators = _comparators()
    assert comparators.within_tolerance(0.0, 0.0, 0.05)[0] is True
    assert comparators.within_tolerance(0.001, 0.0, 0.05)[0] is False


def test_booleans_never_compare_numerically() -> None:
    # bool is a subclass of int, so a numeric path would let True satisfy a
    # recorded 1 within any tolerance -- a type confusion dressed up as a pass.
    comparators = _comparators()
    assert comparators.within_tolerance(True, 1, 0.5)[0] is False
    assert comparators.within_tolerance(True, True, 0.5)[0] is True


def test_sequences_compare_elementwise_and_report_the_failing_index() -> None:
    comparators = _comparators()
    assert comparators.within_tolerance([1.0, 2.0], [1.0, 2.01], 0.05)[0] is True
    ok, detail = comparators.within_tolerance([1.0, 9.0], [1.0, 2.0], 0.05)
    assert ok is False
    assert "[1]" in detail


def test_a_length_mismatch_is_reported_as_such() -> None:
    comparators = _comparators()
    ok, detail = comparators.within_tolerance([1, 2], [1, 2, 3], 0.05)
    assert ok is False
    assert "length mismatch" in detail


def test_mappings_compare_by_key_and_name_the_missing_ones() -> None:
    comparators = _comparators()
    assert comparators.within_tolerance({"a": 1.0}, {"a": 1.01}, 0.05)[0] is True
    ok, detail = comparators.within_tolerance({"a": 1.0}, {"a": 1.0, "b": 2.0}, 0.05)
    assert ok is False
    assert "missing ['b']" in detail


def test_nested_structures_report_the_full_path_to_the_difference() -> None:
    comparators = _comparators()
    ok, detail = comparators.within_tolerance(
        {"q1": [1.0, 5.0]}, {"q1": [1.0, 2.0]}, 0.05
    )
    assert ok is False
    assert "'q1'" in detail and "[1]" in detail


def test_strings_compare_whole_rather_than_character_by_character() -> None:
    comparators = _comparators()
    ok, detail = comparators.within_tolerance("blue", "green", 0.5)
    assert ok is False
    assert "length mismatch" not in detail


# --- the script builder (pure) -------------------------------------------


def test_the_script_prefers_a_contract_local_comparator_over_the_shared_one() -> None:
    script = build_script(
        candidate_path="/tmp/c.py", comparators_path="/tmp/cmp.py",
        oracle_path="/tmp/o.py", entry="f", plan=[],
    )
    # Resolution order is the mechanism by which a domain supplies a comparison
    # the shared library has no business knowing about.
    assert script.index('getattr(oracle, bt["compare"]') < script.index(
        'getattr(shared, bt["compare"]'
    )


def test_the_script_tolerates_a_contract_with_no_oracle() -> None:
    script = build_script(
        candidate_path="/tmp/c.py", comparators_path="/tmp/cmp.py",
        oracle_path=None, entry="f", plan=[],
    )
    assert "oracle_path = None" in script


# --- the gate, against a real sandbox ------------------------------------


def _gate(
    tmp_path: pathlib.Path, backtests: tuple[Backtest, ...], *, entry: str = "sum_of_divisors"
) -> gauntlet.Result | None:
    """Run ``_backtest_gate`` against a hand-built sandbox dir.

    Cheaper than a full ``validate()`` (no mypy, no pytest) while exercising the
    real script in the real sandbox.
    """
    candidate = tmp_path / "target.py"
    candidate.write_text(CANDIDATE_SOURCE, encoding="utf-8")
    oracle = tmp_path / "oracle.py"
    oracle.write_text(
        (PROJECT_ROOT / "specs/sum_of_divisors/oracle.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "sitecustomize.py").write_text(gauntlet._NETWORK_GUARD, encoding="utf-8")
    env = gauntlet._sandbox_env(home=str(tmp_path), pythonpath=str(tmp_path))
    spec = replace(default_contract(), entry=entry, backtests=backtests)
    return gauntlet._backtest_gate(
        spec, tmp=tmp_path, candidate=candidate, oracle_mod=oracle,
        tmpdir=str(tmp_path), env=env,
    )


def test_a_contract_with_no_backtests_skips_the_gate_entirely(tmp_path: pathlib.Path) -> None:
    assert _gate(tmp_path, ()) is None


def test_a_candidate_reproducing_the_record_passes(tmp_path: pathlib.Path) -> None:
    fixture = _write_fixture(tmp_path / "f.json", [6], event_time="2026-01-15T00:00:00Z")
    expect = _write_expect(tmp_path / "e.json", 12)  # 1+2+3+6
    assert _gate(tmp_path, (Backtest(name="six", fixture=fixture, expect=expect),)) is None


def test_a_candidate_contradicting_the_record_is_rejected(tmp_path: pathlib.Path) -> None:
    fixture = _write_fixture(tmp_path / "f.json", [6])
    expect = _write_expect(tmp_path / "e.json", 999)
    result = _gate(tmp_path, (Backtest(name="six", fixture=fixture, expect=expect),))
    assert result is not None and not result.passed
    assert "did not reproduce recorded history" in result.reason
    # The episode's name and the comparator that judged it survive into the
    # reason, so the log says which fixture broke and how it was measured.
    assert "six" in result.reason
    assert "within_tolerance" in result.reason


def test_a_rejection_names_the_split_it_was_judged_against(tmp_path: pathlib.Path) -> None:
    fixture = _write_fixture(tmp_path / "f.json", [6])
    expect = _write_expect(tmp_path / "e.json", 999)
    result = _gate(
        tmp_path,
        (Backtest(name="six", fixture=fixture, expect=expect, split=Split.HOLDOUT),),
    )
    assert result is not None and "holdout" in result.reason


def test_tolerance_is_per_backtest(tmp_path: pathlib.Path) -> None:
    # sum_of_divisors(6) == 12; a recorded 12.5 is ~4% off.
    fixture = _write_fixture(tmp_path / "f.json", [6])
    expect = _write_expect(tmp_path / "e.json", 12.5)
    strict = Backtest(name="s", fixture=fixture, expect=expect, tolerance=0.01)
    loose = Backtest(name="l", fixture=fixture, expect=expect, tolerance=0.10)
    assert _gate(tmp_path, (strict,)) is not None
    assert _gate(tmp_path, (loose,)) is None


def test_a_contract_may_name_a_comparator_defined_in_its_own_oracle(
    tmp_path: pathlib.Path,
) -> None:
    # The oracle's comparator must win over the shared library, which is how a
    # domain expresses a comparison the shared set should not know about.
    oracle = tmp_path / "oracle.py"
    candidate = tmp_path / "target.py"
    candidate.write_text(CANDIDATE_SOURCE, encoding="utf-8")
    oracle.write_text(
        "def always_ok(actual, expected, tolerance):\n"
        "    return True, 'domain comparator ran'\n",
        encoding="utf-8",
    )
    (tmp_path / "sitecustomize.py").write_text(gauntlet._NETWORK_GUARD, encoding="utf-8")
    env = gauntlet._sandbox_env(home=str(tmp_path), pythonpath=str(tmp_path))
    fixture = _write_fixture(tmp_path / "f.json", [6])
    expect = _write_expect(tmp_path / "e.json", 999)  # wrong -- only the oracle saves it
    spec = replace(
        default_contract(),
        backtests=(
            Backtest(name="six", fixture=fixture, expect=expect, compare="always_ok"),
        ),
    )
    assert gauntlet._backtest_gate(
        spec, tmp=tmp_path, candidate=candidate, oracle_mod=oracle,
        tmpdir=str(tmp_path), env=env,
    ) is None


def test_a_missing_fixture_file_is_a_harness_fault_not_the_candidates(
    tmp_path: pathlib.Path,
) -> None:
    expect = _write_expect(tmp_path / "e.json", 12)
    result = _gate(
        tmp_path,
        (Backtest(name="six", fixture=str(tmp_path / "nope.json"), expect=expect),),
    )
    assert result is not None and result.reason.startswith("harness:")
    assert gate_from_reason(result.reason) == "harness"


def test_a_malformed_fixture_is_a_harness_fault_and_names_the_file(
    tmp_path: pathlib.Path,
) -> None:
    bad = tmp_path / "f.json"
    bad.write_text("{not json", encoding="utf-8")
    expect = _write_expect(tmp_path / "e.json", 12)
    result = _gate(tmp_path, (Backtest(name="six", fixture=str(bad), expect=expect),))
    assert result is not None and result.reason.startswith("harness:")
    assert "six" in result.reason


def test_an_unknown_comparator_is_a_harness_fault(tmp_path: pathlib.Path) -> None:
    fixture = _write_fixture(tmp_path / "f.json", [6])
    expect = _write_expect(tmp_path / "e.json", 12)
    result = _gate(
        tmp_path,
        (Backtest(name="six", fixture=fixture, expect=expect, compare="no_such_fn"),),
    )
    assert result is not None and result.reason.startswith("harness:")
    assert gate_from_reason(result.reason) == "harness"


def test_a_candidate_without_the_entry_point_fails_as_an_interface_problem(
    tmp_path: pathlib.Path,
) -> None:
    fixture = _write_fixture(tmp_path / "f.json", [6])
    expect = _write_expect(tmp_path / "e.json", 12)
    result = _gate(
        tmp_path,
        (Backtest(name="six", fixture=fixture, expect=expect),),
        entry="not_exported",
    )
    assert result is not None
    assert gate_from_reason(result.reason) == "interface"


# --- episodic classification ---------------------------------------------


def test_a_backtest_rejection_is_logged_under_its_own_gate() -> None:
    # Distinct from `correctness`: that gate compares against a reference oracle
    # evaluated on demand, this one against history that actually happened.
    reason = "backtest failed: candidate did not reproduce recorded history — {...}"
    assert gate_from_reason(reason) == "backtest"


def test_a_backtest_timeout_is_still_logged_as_a_timeout() -> None:
    assert gate_from_reason("backtest gate timed out") == "timeout"


# --- end to end through validate() ---------------------------------------


def _contract_with(backtests: tuple[Backtest, ...]) -> OptimizationContract:
    return replace(default_contract(), backtests=backtests)


def test_validate_rejects_a_candidate_that_contradicts_history(
    tmp_path: pathlib.Path,
) -> None:
    """The acceptance criterion: a real cycle stops at the backtest gate.

    The candidate here is the shipped optimised target -- correct, fast, and
    passing every other gate -- so the only thing that can reject it is the
    recorded episode it fails to reproduce.
    """
    fixture = _write_fixture(tmp_path / "f.json", [6])
    expect = _write_expect(tmp_path / "e.json", 999)
    contract = _contract_with(
        (Backtest(name="q1", fixture=fixture, expect=expect, split=Split.HOLDOUT),)
    )
    result = gauntlet.validate(CANDIDATE_SOURCE, contract=contract)
    assert not result.passed
    assert gate_from_reason(result.reason) == "backtest"


def test_validate_passes_a_candidate_that_reproduces_history(
    tmp_path: pathlib.Path,
) -> None:
    fixture = _write_fixture(tmp_path / "f.json", [6])
    expect = _write_expect(tmp_path / "e.json", 12)
    contract = _contract_with((Backtest(name="q1", fixture=fixture, expect=expect),))
    result = gauntlet.validate(CANDIDATE_SOURCE, contract=contract)
    assert result.passed, result.reason


def test_the_default_tolerance_matches_the_documented_worked_example() -> None:
    # docs/CLASS2_CONTRACT.md's planner example lands "within 5% of realized cost".
    assert DEFAULT_TOLERANCE == 0.05
