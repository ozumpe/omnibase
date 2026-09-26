"""Tests for the CEO's cost/regression brakes (pure logic, no Ray)."""

import pytest

from sis.roles import (
    DEFAULT_BUDGET_USD,
    CEOConfig,
    ceo_config_from_env,
    evaluate_brakes,
)

BASE = dict(budget=5.0, threshold=3, max_cost_per_accepted=2.0, slo_min_spend=0.5)


def test_healthy_loop_does_not_trip() -> None:
    assert evaluate_brakes(spent=1.0, consecutive_failures=0, accepted=2, **BASE) is None


def test_hard_spend_cap() -> None:
    assert evaluate_brakes(spent=5.01, consecutive_failures=0, accepted=10,
                           **BASE) == "hard spend cap exceeded"


def test_consecutive_failures() -> None:
    assert evaluate_brakes(spent=0.0, consecutive_failures=3, accepted=0,
                           **BASE) == "consecutive failure threshold"


def test_cost_per_accepted_slo() -> None:
    # $4 spent, only 1 acceptance → $4/accepted > $2 ceiling, and spend ≥ SLO floor.
    assert evaluate_brakes(spent=4.0, consecutive_failures=0, accepted=1,
                           **BASE) == "cost-per-accepted-improvement SLO breached"


def test_slo_not_judged_on_pennies() -> None:
    # Spent below the SLO floor: don't trip on cost-per-accepted yet.
    assert evaluate_brakes(spent=0.40, consecutive_failures=0, accepted=0, **BASE) is None


def test_stub_loop_is_free_and_safe() -> None:
    # The stub never spends, so cost brakes never fire regardless of acceptances.
    assert evaluate_brakes(spent=0.0, consecutive_failures=0, accepted=0, **BASE) is None


# --- M5: env-configurable CEO budget/brakes ------------------------------


def test_ceo_config_defaults_when_unset() -> None:
    # No env vars → the documented defaults (was the only possible config).
    assert ceo_config_from_env({}) == CEOConfig(
        budget_usd=DEFAULT_BUDGET_USD, breaker_threshold=3,
        max_cost_per_accepted_usd=2.0, slo_min_spend_usd=0.50)


def test_ceo_config_reads_a_tiny_budget() -> None:
    # The whole point of M5: set a deliberately tiny budget without editing source.
    cfg = ceo_config_from_env({"SIS_BUDGET_USD": "0.10", "SIS_BREAKER_THRESHOLD": "1"})
    assert cfg.budget_usd == 0.10
    assert cfg.breaker_threshold == 1
    assert cfg.max_cost_per_accepted_usd == 2.0  # untouched → default


def test_ceo_config_rejects_a_garbled_budget() -> None:
    # A typo'd cap must fail loudly, never silently fall back to the $5 default.
    with pytest.raises(ValueError, match="SIS_BUDGET_USD"):
        ceo_config_from_env({"SIS_BUDGET_USD": "0.1O"})  # letter O, not zero


def test_ceo_config_rejects_a_negative_budget() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        ceo_config_from_env({"SIS_MAX_COST_PER_ACCEPTED_USD": "-1"})
