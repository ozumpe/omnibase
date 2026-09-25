"""Tests for the SLO gate (OMNI-24): a latency budget, explicitly not a correctness gate.

The load-bearing claim is the distinction itself: a candidate that is correct and
slow is rejected under its own reject gate (``slo``), after every correctness gate
has passed, and the CEO counts that at a reduced weight — never as the wrong-answer
verdict a correctness gate gives.
"""

from __future__ import annotations

import pathlib
from dataclasses import replace
from typing import Any

import pytest

from sis import gauntlet
from sis.contract import ROMAN, SUM_OF_DIVISORS, FeatureContract, GateName
from sis.episodic import gate_from_reason
from sis.policy import ChangeTier, classify
from sis.roles import (
    DEFAULT_SLO_FAILURE_WEIGHT,
    CEOConfig,
    ceo_config_from_env,
    check_slo_failure_weight,
    evaluate_brakes,
    failure_weight,
)
from sis.slo import DomainSLO, build_script, evaluate_slo, percentile_value
from tests.test_invariant import ROMAN_OK

INPUTS = ((1,), (4,), (1987,))

# Correct, and slow: every call sleeps. It passes interface and acceptance, so the
# only gate with anything to say about it is the SLO gate.
ROMAN_SLOW = ROMAN_OK.replace(
    "    out: list[str] = []",
    "    import time\n    time.sleep(0.005)\n    out: list[str] = []",
)

# The invariant gate calls the candidate hundreds of times; for a deliberately
# slow candidate that is seconds of work that tests nothing about the SLO gate.
ROMAN_NO_LAWS = replace(ROMAN, invariants=())


def _with(slo: DomainSLO, **overrides: Any) -> FeatureContract:
    return replace(ROMAN_NO_LAWS, slo=slo, **overrides)


# --- declaration ----------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"budget_ms": 0, "inputs": INPUTS}, "budget_ms must be > 0"),
        ({"budget_ms": 1, "inputs": INPUTS, "metric": "accuracy"}, "not supported"),
        ({"budget_ms": 1, "inputs": INPUTS, "percentile": 0}, "percentile"),
        ({"budget_ms": 1, "inputs": INPUTS, "max_repeats": 0}, "max_repeats"),
        ({"budget_ms": 1}, "exactly one"),
        ({"budget_ms": 1, "inputs": INPUTS, "workload": "w"}, "exactly one"),
        ({"budget_ms": 1, "inputs": ()}, "must not be empty"),
        ({"budget_ms": 1, "inputs": ([1],)}, "argument tuples"),
        ({"budget_ms": 1, "inputs": ((object(),),)}, "literals"),
        ({"budget_ms": 1, "workload": "not a name"}, "function name"),
    ],
)
def test_a_malformed_slo_is_rejected_at_declaration(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        DomainSLO(**kwargs)


def test_a_workload_slo_needs_an_oracle_to_resolve_it_in() -> None:
    with pytest.raises(ValueError, match="oracle_path is None"):
        _with(DomainSLO(budget_ms=1, workload="w"), oracle_path=None)


def test_the_slo_gate_runs_last_and_only_for_class_2() -> None:
    assert ROMAN.gate_profile()[-1] is GateName.SLO
    # Class 1 already answers its performance question with the benchmark; a
    # second timing verdict over the same candidate is exactly what to avoid.
    assert GateName.SLO not in SUM_OF_DIVISORS.gate_profile()
    assert SUM_OF_DIVISORS.slo is None


def test_no_shipped_contract_declares_an_slo_yet() -> None:
    # Adding one changes that contract's exam, which is a human decision.
    assert ROMAN.slo is None


def test_the_slo_module_is_guardrail_code() -> None:
    assert classify("sis/slo.py") is ChangeTier.FORBIDDEN


# --- the verdict (pure) ---------------------------------------------------


def test_percentile_is_nearest_rank_so_it_names_a_measured_latency() -> None:
    values = [0.001 * i for i in range(1, 21)]  # 1ms..20ms
    assert percentile_value(values, 95) == pytest.approx(0.019)
    assert percentile_value(values, 100) == pytest.approx(0.020)
    assert percentile_value([0.5], 95) == 0.5


def test_within_budget_passes() -> None:
    verdict = evaluate_slo(DomainSLO(budget_ms=10, inputs=INPUTS), [0.001, 0.002, 0.009])
    assert verdict.passed
    assert verdict.observed_seconds == pytest.approx(0.009)


def test_over_budget_fails_with_a_reason_that_classifies_as_slo() -> None:
    verdict = evaluate_slo(DomainSLO(budget_ms=5, inputs=INPUTS), [0.001, 0.002, 0.030])
    assert not verdict.passed
    assert gate_from_reason(verdict.reason) == "slo"
    assert "input #2" in verdict.reason
    assert "30.000ms" in verdict.reason
    assert "correct but over budget" in verdict.reason


def test_the_reason_never_reads_as_a_timeout_or_a_correctness_failure() -> None:
    # gate_from_reason checks "timed out"/"timeout" first; an SLO reason that
    # tripped it would be filed as a hung gate, not a slow candidate.
    reason = evaluate_slo(DomainSLO(budget_ms=1, inputs=INPUTS), [1.0, 1.0, 1.0]).reason
    assert "timeout" not in reason.lower() and "timed out" not in reason.lower()
    assert gate_from_reason(reason) not in {"correctness", "acceptance", "invariant"}


def test_the_script_never_evals_the_inputs() -> None:
    script = build_script(
        candidate_path="c.py", oracle_path=None, entry="f",
        slo=DomainSLO(budget_ms=1, inputs=INPUTS),
    )
    assert "ast.literal_eval" in script
    assert "eval(" not in script.replace("literal_eval(", "")


def test_the_script_stops_retrying_an_input_once_a_run_is_under_budget() -> None:
    # What keeps a fast candidate at one timed call per input.
    script = build_script(
        candidate_path="c.py", oracle_path=None, entry="f",
        slo=DomainSLO(budget_ms=1, inputs=INPUTS),
    )
    assert "if best <= budget:" in script and "break" in script


# --- the gate, end to end -------------------------------------------------


def test_a_contract_without_an_slo_skips_the_gate() -> None:
    assert gauntlet.validate(ROMAN_SLOW, contract=ROMAN_NO_LAWS).passed


def test_a_fast_candidate_passes_and_reports_its_latency() -> None:
    result = gauntlet.validate(ROMAN_OK, contract=_with(DomainSLO(budget_ms=50, inputs=INPUTS)))
    assert result.passed, result.reason
    # Class 2 has no benchmark, so the SLO measurement is the cycle's latency.
    assert result.latency_seconds is not None and result.latency_seconds < 0.05


def test_correct_but_slow_is_an_slo_rejection_not_a_correctness_one() -> None:
    """The distinction the ticket exists for.

    ROMAN_SLOW passes interface and acceptance; the only thing wrong with it is
    that it is over budget, and the verdict has to say exactly that.
    """
    result = gauntlet.validate(ROMAN_SLOW, contract=_with(DomainSLO(budget_ms=1, inputs=INPUTS)))
    assert not result.passed
    assert gate_from_reason(result.reason) == "slo"
    assert result.latency_seconds is not None and result.latency_seconds >= 0.005


def test_the_same_slow_candidate_passes_a_budget_it_fits() -> None:
    contract = _with(DomainSLO(budget_ms=200, inputs=INPUTS))
    assert gauntlet.validate(ROMAN_SLOW, contract=contract).passed


def _oracle_with_workload(tmp_path: pathlib.Path, body: str) -> str:
    oracle = tmp_path / "oracle.py"
    oracle.write_text(body, encoding="utf-8")
    return str(oracle)  # absolute: PROJECT_ROOT / <absolute path> is that path


def test_a_named_workload_is_resolved_in_the_oracle(tmp_path: pathlib.Path) -> None:
    oracle = _oracle_with_workload(
        tmp_path, "def roman_workload():\n    return [(n,) for n in range(1, 200, 7)]\n")
    contract = _with(DomainSLO(budget_ms=50, workload="roman_workload"), oracle_path=oracle)
    assert gauntlet.validate(ROMAN_OK, contract=contract).passed


def test_a_missing_workload_is_a_harness_fault(tmp_path: pathlib.Path) -> None:
    oracle = _oracle_with_workload(tmp_path, "X = 1\n")
    contract = _with(DomainSLO(budget_ms=50, workload="roman_workload"), oracle_path=oracle)
    result = gauntlet.validate(ROMAN_OK, contract=contract)
    assert not result.passed
    assert gate_from_reason(result.reason) == "harness"


def test_an_empty_workload_is_a_harness_fault(tmp_path: pathlib.Path) -> None:
    oracle = _oracle_with_workload(tmp_path, "def roman_workload():\n    return []\n")
    contract = _with(DomainSLO(budget_ms=50, workload="roman_workload"), oracle_path=oracle)
    assert gate_from_reason(gauntlet.validate(ROMAN_OK, contract=contract).reason) == "harness"


def test_raising_on_a_workload_input_is_a_wrong_answer_not_a_slow_one() -> None:
    # 0 is outside 1..3999, so a correct to_roman raises. That is not "over
    # budget" and must not get the discounted slo weight.
    contract = _with(DomainSLO(budget_ms=50, inputs=((5,), (0,))))
    result = gauntlet.validate(ROMAN_OK, contract=contract)
    assert not result.passed
    assert gate_from_reason(result.reason) == "slo_error"


# --- the circuit breaker --------------------------------------------------


def test_only_an_slo_rejection_is_discounted() -> None:
    assert failure_weight("slo", slo_failure_weight=0.5) == 0.5
    for gate in ("slo_error", "correctness", "acceptance", "invariant", "harness", None):
        assert failure_weight(gate, slo_failure_weight=0.5) == 1.0


def test_two_over_budget_cycles_count_like_one_wrong_one() -> None:
    base = dict(spent=0.0, budget=5.0, threshold=1, accepted=0,
                max_cost_per_accepted=2.0, slo_min_spend=0.5)
    one = failure_weight("slo", slo_failure_weight=DEFAULT_SLO_FAILURE_WEIGHT)
    assert evaluate_brakes(consecutive_failures=one, **base) is None
    assert evaluate_brakes(consecutive_failures=2 * one, **base) == "consecutive failure threshold"


@pytest.mark.parametrize("weight", [0.0, -0.5, 1.01, 2.0])
def test_the_weight_is_bounded_to_the_unit_interval(weight: float) -> None:
    # 0 would silently switch SLO failures off; above 1 would count a slow
    # cycle for more than a wrong one.
    with pytest.raises(ValueError):
        check_slo_failure_weight(weight)


def test_the_weight_is_configurable_from_the_environment() -> None:
    assert ceo_config_from_env({"SIS_SLO_FAILURE_WEIGHT": "0.25"}).slo_failure_weight == 0.25
    assert CEOConfig().slo_failure_weight == DEFAULT_SLO_FAILURE_WEIGHT == 0.5


@pytest.mark.parametrize("raw", ["0", "1.5", "-1"])
def test_an_out_of_range_weight_in_the_environment_fails_loudly(raw: str) -> None:
    with pytest.raises(ValueError):
        ceo_config_from_env({"SIS_SLO_FAILURE_WEIGHT": raw})


def test_a_weighted_streak_reaches_its_threshold_despite_float_rounding() -> None:
    # sum([0.1] * 10) is 0.9999999999999999; a strict >= would never trip.
    streak = 0.0
    for _ in range(10):
        streak += failure_weight("slo", slo_failure_weight=0.1)
    assert streak < 1.0  # the rounding this guards against is real
    assert evaluate_brakes(
        spent=0.0, budget=5.0, consecutive_failures=streak, threshold=1, accepted=0,
        max_cost_per_accepted=2.0, slo_min_spend=0.5,
    ) == "consecutive failure threshold"
