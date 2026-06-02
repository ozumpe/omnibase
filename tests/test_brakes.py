"""Tests for the CEO's cost/regression brakes (pure logic, no Ray)."""

from sis.roles import evaluate_brakes

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
