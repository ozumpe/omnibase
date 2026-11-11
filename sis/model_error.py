"""sis.model_error — when a predictor is wrong enough to act on (OMNI-196, Phase B).

D9 asks two questions of a predictor's error series, and keeps them apart:

- **Error budget** (absolute): is the twin fit for its purpose right now? The
  mean absolute error over a window of scored weeks is above the actor's
  budget. A harder world counts: the budget can fire while the skill holds.
  Its consequence (OMNI-197) is a page *and* an improvement cycle.
- **Skill decay**: is there headroom a code change could recover? The skill
  against climatology (``1 - MAE(model) / MAE(climatology)``, as
  :func:`sis.prediction_error.skill` computes it) is below the actor's floor.
  It can fire while the absolute error still looks fine. Its consequence is a
  cycle, paid for through the CEO's budget gate.

They are separate triggers with separate rules, so one cannot stand in for the
other, and each :class:`Decision` names which one it is and why.

**Sustained, never a spike**, in the shape of :func:`sis.loop.serve_breach`:
a window must be in breach for ``windows`` consecutive ticks before a trigger
fires, and firing resets the streak, so a long breach starts one cycle per
``windows`` ticks, not one per tick. **Too few samples is never a breach**:
a window with fewer scored weeks than the rule's minimum is healthy, the safe
direction, as an empty ``live_metrics`` window is for the SLO trigger. Skipped
weeks (nothing to compare with, a next week already known, a failed answer)
never count: they carry no error.

Thresholds are per actor (D9), so every one is an argument; nothing here has a
default threshold. Pure: no Ray, no clock, no I/O. Reading the series and
acting on a decision is OMNI-197's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from sis.prediction_error import Step, skill


class Trigger(StrEnum):
    ERROR_BUDGET = "error_budget"
    SKILL_DECAY = "skill_decay"


@dataclass(frozen=True)
class WindowStats:
    """What a window of weeks says, over the weeks that were scored."""

    weeks: int                     # weeks in the window, skipped ones included
    scored: int                    # weeks with a model error
    mae: float | None              # the model's MAE over the scored weeks
    paired: int                    # weeks with both a model and a climatology error
    skill: float | None            # skill against climatology over the paired weeks


def window_stats(steps: Sequence[Step]) -> WindowStats:
    """The statistics both triggers judge, from one window of steps. Pure."""
    errors = [abs(step.error) for step in steps if step.error is not None]
    pairs = [(abs(step.error), abs(step.climatology_error)) for step in steps
             if step.error is not None and step.climatology_error is not None]
    model_paired = sum(m for m, _ in pairs) / len(pairs) if pairs else None
    climatology_paired = sum(c for _, c in pairs) / len(pairs) if pairs else None
    return WindowStats(weeks=len(steps), scored=len(errors),
                       mae=sum(errors) / len(errors) if errors else None,
                       paired=len(pairs), skill=skill(model_paired, climatology_paired))


def latest_window(steps: Sequence[Step], weeks: int) -> tuple[Step, ...]:
    """The last *weeks* steps by week, skipped ones included. Pure.

    A window is a span of the calendar, not a count of scored weeks: a run of
    skipped weeks shrinks what it holds, and :func:`window_stats`'s minimums
    then keep it from judging on too little.
    """
    if weeks < 1:
        raise ValueError(f"a window needs at least one week, got {weeks}")
    return tuple(sorted(steps, key=lambda step: step.week)[-weeks:])


@dataclass(frozen=True)
class BudgetRule:
    """An actor's absolute error budget, in the series' unit."""

    mae_budget: float              # a window's MAE above this is a breach
    min_scored: int                # fewer scored weeks than this is never a breach
    windows: int                   # consecutive breaching ticks before it fires

    def __post_init__(self) -> None:
        _check(self.min_scored, self.windows)
        if not self.mae_budget > 0:
            raise ValueError(f"an error budget must be above 0, got {self.mae_budget}")


@dataclass(frozen=True)
class SkillRule:
    """An actor's floor on skill against climatology."""

    skill_floor: float             # a window's skill below this is a breach
    min_paired: int                # fewer paired weeks than this is never a breach
    windows: int                   # consecutive breaching ticks before it fires

    def __post_init__(self) -> None:
        _check(self.min_paired, self.windows)
        if not self.skill_floor < 1:
            raise ValueError(f"a skill floor must be below 1, got {self.skill_floor}")


def _check(minimum: int, windows: int) -> None:
    if minimum < 1:
        raise ValueError(f"the minimum number of weeks must be >= 1, got {minimum}")
    if windows < 1:
        raise ValueError(f"windows must be >= 1, got {windows}")


@dataclass(frozen=True)
class Decision:
    """One trigger's verdict on one tick.

    ``in_breach`` is this window alone; ``fire`` is the sustained verdict.
    ``consecutive`` is the streak to carry into the next tick: 0 after firing
    or after a healthy window. ``reason`` names the trigger and the numbers.
    """

    trigger: Trigger
    in_breach: bool
    fire: bool
    consecutive: int
    reason: str
    stats: WindowStats


def budget_in_breach(stats: WindowStats, rule: BudgetRule) -> tuple[bool, str]:
    """Is this one window over the actor's error budget, and why. Pure."""
    if stats.scored < rule.min_scored or stats.mae is None:
        return False, (f"{stats.scored} scored week(s), fewer than the "
                       f"{rule.min_scored} the error budget needs")
    if stats.mae > rule.mae_budget:
        return True, (f"MAE {stats.mae:.4g} over {stats.scored} scored week(s) is above "
                      f"the budget {rule.mae_budget:.4g}")
    return False, (f"MAE {stats.mae:.4g} over {stats.scored} scored week(s) is within "
                   f"the budget {rule.mae_budget:.4g}")


def skill_in_breach(stats: WindowStats, rule: SkillRule) -> tuple[bool, str]:
    """Is this one window's skill below the actor's floor, and why. Pure.

    A window with no skill to claim (no paired week, or a perfect
    climatology) is never a breach: there is no headroom it can show.
    """
    if stats.paired < rule.min_paired:
        return False, (f"{stats.paired} week(s) scored against climatology, fewer than "
                       f"the {rule.min_paired} the skill floor needs")
    if stats.skill is None:
        return False, "no skill to judge: climatology was perfect over the window"
    if stats.skill < rule.skill_floor:
        return True, (f"skill {stats.skill:+.3f} over {stats.paired} week(s) is below "
                      f"the floor {rule.skill_floor:+.3f}")
    return False, (f"skill {stats.skill:+.3f} over {stats.paired} week(s) is at or above "
                   f"the floor {rule.skill_floor:+.3f}")


def _sustain(trigger: Trigger, stats: WindowStats, in_breach: bool, why: str,
             consecutive: int, windows: int) -> Decision:
    if consecutive < 0:
        raise ValueError(f"a streak cannot be negative, got {consecutive}")
    if not in_breach:
        return Decision(trigger, False, False, 0, why, stats)
    streak = consecutive + 1
    if streak >= windows:
        return Decision(trigger, True, True, 0,
                        f"{trigger.value}: {why}, {streak} window(s) in a row", stats)
    return Decision(trigger, True, False, streak,
                    f"{why}, {streak} of {windows} window(s) in a row", stats)


def error_budget(steps: Sequence[Step], rule: BudgetRule, *, consecutive: int) -> Decision:
    """The error-budget trigger on one window. Pure.

    *consecutive* is how many prior ticks were already in breach; the caller
    keeps it (OMNI-197's trigger), and passes back the returned one.
    """
    stats = window_stats(steps)
    breach, why = budget_in_breach(stats, rule)
    return _sustain(Trigger.ERROR_BUDGET, stats, breach, why, consecutive, rule.windows)


def skill_decay(steps: Sequence[Step], rule: SkillRule, *, consecutive: int) -> Decision:
    """The skill-decay trigger on one window. Pure; see :func:`error_budget`."""
    stats = window_stats(steps)
    breach, why = skill_in_breach(stats, rule)
    return _sustain(Trigger.SKILL_DECAY, stats, breach, why, consecutive, rule.windows)


__all__ = ["BudgetRule", "Decision", "SkillRule", "Trigger", "WindowStats",
           "budget_in_breach", "error_budget", "latest_window", "skill_decay",
           "skill_in_breach", "window_stats"]
