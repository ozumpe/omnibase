"""OMNI-196: the two model-error triggers, error budget and skill decay, as pure decisions.

What these pin (D9): the triggers are separate, a harder world fires the
budget and not the skill, drift fires the skill and not the budget; neither
fires on a single spike or on too few samples; a sustained breach fires once
and resets its streak; skipped weeks never count. No Ray and no I/O.
"""

from __future__ import annotations

import datetime
import subprocess
import sys
from collections.abc import Callable

import pytest

from sis import model_error, policy
from sis.model_error import BudgetRule, SkillRule, Trigger
from sis.paths import PROJECT_ROOT
from sis.prediction_error import Step

START = datetime.datetime(2022, 8, 1, tzinfo=datetime.UTC)


def _week(i: int) -> datetime.datetime:
    return START + datetime.timedelta(weeks=i)


def _step(i: int, error: float | None, climatology_error: float | None = None) -> Step:
    return Step(week=_week(i), at=_week(i) + datetime.timedelta(days=2),
                predicted=None if error is None else 5.0 + error,
                actual=None if error is None else 5.0, error=error,
                note="" if error is not None else "skipped",
                climatology=None, climatology_error=climatology_error)


def _window(errors: list[float], climatology: list[float] | None = None,
            first: int = 0) -> list[Step]:
    clim: list[float | None] = list(climatology) if climatology else [None] * len(errors)
    return [_step(first + i, e, c) for i, (e, c) in enumerate(zip(errors, clim, strict=True))]


BUDGET = BudgetRule(mae_budget=0.20, min_scored=4, windows=3)
SKILL = SkillRule(skill_floor=0.0, min_paired=4, windows=3)


def _run(decide: Callable[..., model_error.Decision], windows: list[list[Step]],
         rule: BudgetRule | SkillRule) -> list[model_error.Decision]:
    """Feed windows tick by tick, carrying the streak as OMNI-197's trigger will."""
    out: list[model_error.Decision] = []
    streak = 0
    for steps in windows:
        decision = decide(steps, rule, consecutive=streak)
        out.append(decision)
        streak = decision.consecutive
    return out


# -- one window ----------------------------------------------------------------

def test_window_stats_skip_unscored_weeks_and_pair_for_skill() -> None:
    steps = [_step(0, 0.1, 0.2), _step(1, -0.3, 0.1), _step(2, None), _step(3, 0.2)]
    stats = model_error.window_stats(steps)
    assert (stats.weeks, stats.scored, stats.paired) == (4, 3, 2)
    assert stats.mae == pytest.approx(0.2)
    # Skill over the two paired weeks: 1 - 0.2 / 0.15.
    assert stats.skill == pytest.approx(1 - 0.2 / 0.15)


def test_latest_window_is_a_span_of_weeks_skips_included() -> None:
    steps = [_step(i, None if i == 7 else 0.1) for i in range(10)][::-1]
    window = model_error.latest_window(steps, 4)
    assert [s.week for s in window] == [_week(i) for i in range(6, 10)]
    assert model_error.window_stats(window).scored == 3
    with pytest.raises(ValueError):
        model_error.latest_window(steps, 0)


def test_too_few_samples_is_never_a_breach() -> None:
    # Huge errors, but three scored weeks against a minimum of four.
    steps = [_step(0, 5.0, 0.1), _step(1, 5.0, 0.1), _step(2, None), _step(3, 5.0, 0.1)]
    budget = model_error.error_budget(steps, BUDGET, consecutive=2)
    skill = model_error.skill_decay(steps, SKILL, consecutive=2)
    assert not budget.in_breach and not budget.fire and budget.consecutive == 0
    assert not skill.in_breach and not skill.fire and skill.consecutive == 0
    assert "fewer than the 4" in budget.reason and "fewer than the 4" in skill.reason


def test_a_perfect_climatology_claims_no_skill_and_no_breach() -> None:
    steps = _window([0.3] * 4, [0.0] * 4)
    decision = model_error.skill_decay(steps, SKILL, consecutive=0)
    assert decision.stats.skill is None and not decision.in_breach
    assert "climatology was perfect" in decision.reason


# -- sustained, never a spike ------------------------------------------------------

def test_a_single_spike_never_fires() -> None:
    quiet, spike = _window([0.05] * 6), _window([0.05] * 5 + [2.0])
    decisions = _run(model_error.error_budget, [quiet, spike, quiet, quiet], BUDGET)
    assert [d.in_breach for d in decisions] == [False, True, False, False]
    assert not any(d.fire for d in decisions)
    assert decisions[2].consecutive == 0     # a healthy window resets the streak


def test_a_sustained_breach_fires_once_and_the_streak_resets() -> None:
    bad = _window([0.5] * 6)
    decisions = _run(model_error.error_budget, [bad] * 7, BUDGET)
    assert [d.fire for d in decisions] == [False, False, True, False, False, True, False]
    assert [d.consecutive for d in decisions] == [1, 2, 0, 1, 2, 0, 1]
    fired = decisions[2]
    assert fired.trigger is Trigger.ERROR_BUDGET
    assert fired.reason.startswith("error_budget: MAE 0.5")
    assert "3 window(s) in a row" in fired.reason
    assert "1 of 3" in decisions[0].reason


def test_windows_of_one_fires_on_the_first_breach() -> None:
    rule = BudgetRule(mae_budget=0.2, min_scored=1, windows=1)
    decision = model_error.error_budget(_window([0.5]), rule, consecutive=0)
    assert decision.fire and decision.consecutive == 0


# -- the two triggers are not the same trigger (D9) -----------------------------------

def test_a_harder_world_fires_the_budget_and_not_the_skill() -> None:
    # Everyone got worse: the model misses by 0.5, climatology by 0.6. The
    # twin is unfit, yet it still beats climatology, so no headroom.
    harder = _window([0.5, -0.5, 0.5, -0.5, 0.5, -0.5], [0.6, -0.6, 0.6, -0.6, 0.6, -0.6])
    budget = _run(model_error.error_budget, [harder] * 3, BUDGET)
    skill = _run(model_error.skill_decay, [harder] * 3, SKILL)
    assert budget[-1].fire
    assert not any(d.in_breach for d in skill)
    assert skill[-1].stats.skill == pytest.approx(1 - 0.5 / 0.6)


def test_drift_fires_the_skill_and_not_the_budget() -> None:
    # Small errors, within budget, but climatology does better: headroom.
    drift = _window([0.10, -0.10] * 3, [0.05, -0.05] * 3)
    budget = _run(model_error.error_budget, [drift] * 3, BUDGET)
    skill = _run(model_error.skill_decay, [drift] * 3, SKILL)
    assert not any(d.in_breach for d in budget)
    assert skill[-1].fire and skill[-1].trigger is Trigger.SKILL_DECAY
    assert "skill -1.000" in skill[-1].reason and "below the floor +0.000" in skill[-1].reason


def test_each_trigger_keeps_its_own_streak() -> None:
    # Alternate a harder world and drift: neither trigger sees three in a row.
    harder = _window([0.5] * 6, [0.6] * 6)
    drift = _window([0.1] * 6, [0.05] * 6)
    ticks = [harder, drift, harder, drift, harder, drift]
    assert not any(d.fire for d in _run(model_error.error_budget, ticks, BUDGET))
    assert not any(d.fire for d in _run(model_error.skill_decay, ticks, SKILL))


# -- rules ---------------------------------------------------------------------------

@pytest.mark.parametrize("make", [
    lambda: BudgetRule(mae_budget=0.0, min_scored=4, windows=3),
    lambda: BudgetRule(mae_budget=0.2, min_scored=0, windows=3),
    lambda: BudgetRule(mae_budget=0.2, min_scored=4, windows=0),
    lambda: SkillRule(skill_floor=1.0, min_paired=4, windows=3),
    lambda: SkillRule(skill_floor=0.0, min_paired=0, windows=3),
])
def test_a_rule_that_could_never_mean_anything_is_refused(
        make: Callable[[], BudgetRule | SkillRule]) -> None:
    with pytest.raises(ValueError):
        make()


def test_a_negative_streak_is_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        model_error.error_budget(_window([0.5] * 4), BUDGET, consecutive=-1)


def test_the_decisions_are_pure() -> None:
    probe = ("import sys\nimport sis.model_error\n"
             "bad = [m for m in ('ray', 'sis.org', 'sis.roles', 'sis.loop', 'sis.sandbox_worker',"
             " 'requests', 'boto3') if m in sys.modules]\nprint('LOADED', bad)\n")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         cwd=PROJECT_ROOT, timeout=60, check=True)
    assert "LOADED []" in out.stdout, out.stdout + out.stderr


def test_the_triggers_are_guardrail_code() -> None:
    assert policy.classify("sis/model_error.py") is policy.ChangeTier.FORBIDDEN
