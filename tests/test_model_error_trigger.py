"""OMNI-197: the model-error trigger starts a cycle in --loop on a sustained breach.

What these pin: the loop, driven with a canned error series, starts exactly
one cycle on a sustained breach and none on a spike; an error-budget breach
pages and skill decay does not; the same series starts a cycle under a tight
setting and none under a loose one; the verdict is the series', so a restart
reaches the same one, and the same evidence read again starts nothing.
"""

from __future__ import annotations

import dataclasses
import datetime
import pathlib
import threading
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from sis import config, episodic, error_series, loop, model_error
from sis.episodic import ScoredWeek
from sis.model_error import BudgetRule
from sis.prediction_error import CALIFORNIA, Step

START = datetime.datetime(2026, 1, 5, tzinfo=datetime.UTC)


def _step(i: int, error: float, climatology_error: float | None = None) -> Step:
    week = START + datetime.timedelta(weeks=i)
    return Step(week=week, at=week + datetime.timedelta(days=2), predicted=5.0 + error,
                actual=5.0, error=error, climatology=None,
                climatology_error=0.20 if climatology_error is None else climatology_error)


def _series(errors: Sequence[float], climatology: float = 0.20) -> list[Step]:
    return [_step(i, e, climatology) for i, e in enumerate(errors)]


# Tight: refine often. Loose: leave the model alone until it can't be ignored.
TIGHT = SimpleNamespace(mae_budget=0.10, breach_weeks=4, skill_floor=0.0, window_weeks=2,
                        min_weeks=2)
LOOSE = SimpleNamespace(mae_budget=0.30, breach_weeks=12, skill_floor=0.0, window_weeks=4,
                        min_weeks=3)


def _drive(steps: Sequence[Step], settings: Any, *, ticks: int = 6,
           grow: Sequence[Sequence[Step]] | None = None) -> tuple[list[loop.Work],
                                                                   list[tuple[str, str]]]:
    """Run the loop over the trigger for *ticks* polls; returns the cycles run and pages."""
    budget, floor, window = loop.model_error_rules(settings)
    pages: list[tuple[str, str]] = []
    seen = list(grow) if grow is not None else []

    def read() -> Sequence[Step]:
        return seen.pop(0) if seen else steps

    trigger = loop.model_error_trigger(read, budget=budget, floor=floor, window_weeks=window,
                                       title="Improve the predictor", body="Make it better.",
                                       page=lambda t, b: pages.append((t, b)))
    ran: list[loop.Work] = []
    polls = [0]

    def poll() -> loop.Tick:
        polls[0] += 1
        return loop.Tick(breaker_open=False, budget_ok=True, work=trigger())

    def run_cycle(work: loop.Work) -> dict[str, Any]:
        ran.append(work)
        return {"status": "rolled_back"}

    stop = threading.Event()

    def sleep(_: float) -> None:
        if polls[0] >= ticks:
            stop.set()

    loop.run_loop(poll, run_cycle, interval_s=0, stop_event=stop, sleep=sleep)
    return ran, pages


# Eight weeks well over budget, then healthy: sustained under TIGHT (two
# windows of two weeks), and at a skill below climatology's.
SUSTAINED = _series([0.25] * 8 + [0.02] * 2)
# One bad week among good ones.
SPIKE = _series([0.02] * 4 + [0.90] + [0.02] * 5)


def test_a_sustained_breach_starts_exactly_one_cycle_and_pages() -> None:
    ran, pages = _drive(SUSTAINED[:4], TIGHT)
    assert len(ran) == 1
    work = ran[0]
    assert work.trigger == "error_budget+skill_decay"
    assert work.evidence["error_budget"]["mae"] == pytest.approx(0.25)
    assert "Why this cycle runs (model-error trigger)" in work.body
    assert "above the budget 0.1" in work.body
    assert len(pages) == 1 and pages[0][0] == "model error over budget: the twin is unfit"


def test_a_spike_starts_no_cycle() -> None:
    ran, pages = _drive(SPIKE, TIGHT)
    assert ran == [] and pages == []


def test_skill_decay_starts_a_cycle_without_a_page() -> None:
    # Within the budget, but worse than climatology: headroom, not an unfit twin.
    drift = _series([0.08] * 4, climatology=0.05)
    ran, pages = _drive(drift, TIGHT)
    assert [w.trigger for w in ran] == ["skill_decay"]
    assert pages == []


def test_the_same_series_starts_a_cycle_tight_and_none_loose() -> None:
    series = SUSTAINED[:8]
    assert len(_drive(series, TIGHT)[0]) == 1
    assert _drive(series, LOOSE) == ([], [])


def test_a_long_breach_starts_one_cycle_per_fresh_stretch() -> None:
    # The series grows by a week per tick; each firing needs breach_weeks of
    # fresh weeks after the last, never one per tick.
    errors = [0.25] * 12
    grow = [_series(errors[:n]) for n in range(1, 13)]
    ran, pages = _drive(grow[-1], TIGHT, ticks=14, grow=grow)
    assert len(ran) == 3 and len(pages) == 3
    assert [w.evidence["error_budget"]["through"] for w in ran] == [
        (START + datetime.timedelta(weeks=i)).date().isoformat() for i in (3, 7, 11)]


def test_the_verdict_is_the_series_so_a_restart_reaches_it_again() -> None:
    budget, floor, window = loop.model_error_rules(TIGHT)
    once = model_error.replay(SUSTAINED[:4], budget, floor, window_weeks=window)
    again = model_error.replay(list(reversed(SUSTAINED[:4])), budget, floor,
                               window_weeks=window)
    assert once == again and once[-1][0].fire
    # A restarted loop, with nothing remembered, acts once on what still stands.
    assert len(_drive(SUSTAINED[:5], TIGHT)[0]) == 1


def test_a_firing_missed_between_ticks_still_stands_until_the_breach_ends() -> None:
    budget, floor, window = loop.model_error_rules(TIGHT)
    # Fired at week 3; weeks 4 and 5 still in breach: it stands.
    still = model_error.standing(model_error.replay(SUSTAINED[:6], budget, floor,
                                                    window_weeks=window))
    assert [d.trigger.value for d in still] == ["error_budget", "skill_decay"]
    # The error came back below the budget at the end: nothing is owed.
    assert model_error.standing(model_error.replay(SUSTAINED, budget, floor,
                                                   window_weeks=window)) == ()


def test_a_series_shorter_than_a_window_has_no_decision() -> None:
    budget, floor, window = loop.model_error_rules(LOOSE)
    assert model_error.replay(SUSTAINED[:3], budget, floor, window_weeks=window) == []
    assert _drive([], LOOSE) == ([], [])


@pytest.mark.parametrize(("breach", "window", "windows"),
                         [(8, 4, 2), (4, 2, 2), (1, 4, 1), (9, 4, 3), (4, 4, 1)])
def test_how_long_is_counted_in_whole_windows(breach: int, window: int, windows: int) -> None:
    assert model_error.windows_for(breach, window) == windows


def test_a_setting_no_window_could_breach_is_refused() -> None:
    with pytest.raises(ValueError, match="min_weeks"):
        loop.model_error_rules(dataclasses.replace(config.config().model_error,
                                                   min_weeks=5, window_weeks=4))
    with pytest.raises(ValueError, match="below 1"):
        loop.model_error_rules(dataclasses.replace(config.config().model_error,
                                                   skill_floor=1.0))


def test_the_trigger_is_off_by_default_and_its_knobs_are_config() -> None:
    settings = config.config()
    assert settings.loop.trigger == "repeat"
    budget, floor, window = loop.model_error_rules(settings.model_error)
    assert (budget.mae_budget, budget.windows, window) == (0.15, 2, 4)
    tight = config.config(env={"SIS_MODEL_ERROR_MAE_BUDGET": "0.05",
                               "SIS_MODEL_ERROR_BREACH_WEEKS": "2",
                               "SIS_LOOP_TRIGGER": "model-error"})
    assert tight.loop.trigger == "model-error"
    assert loop.model_error_rules(tight.model_error)[0] == BudgetRule(0.05, 3, 1)
    with pytest.raises(ValueError):
        config.config(env={"SIS_LOOP_TRIGGER": "model_error"})


def _row(predictor: str, i: int, error: float) -> ScoredWeek:
    week = START + datetime.timedelta(weeks=i)
    return ScoredWeek(target=CALIFORNIA, predictor=predictor, capture="c",
                      week=week.isoformat(), at=week.isoformat(),
                      next_week=(week + datetime.timedelta(weeks=1)).isoformat(),
                      predicted=5.0 + error, actual=5.0, actual_known_at=None, error=error,
                      climatology=None, climatology_error=0.2, skill=None)


def test_only_the_live_predictors_series_is_judged(tmp_path: pathlib.Path) -> None:
    # A merged predictor has a new version, and its own fresh series: the one
    # that started the cycle is no longer judged.
    store = episodic.JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    predictor = tmp_path / "predictor.py"
    predictor.write_text("def predict(x): return x\n")
    old = error_series.version(predictor.read_text())
    store.append_scored([_row(old, i, 0.25) for i in range(4)]
                        + [_row("someone-else", i, 0.01) for i in range(4)])
    read = loop.scored_steps(store, lambda: predictor)
    assert [s.error for s in read()] == [0.25] * 4
    predictor.write_text("def predict(x): return x + 0\n")
    assert list(read()) == []


def test_main_wires_the_trigger_the_config_names(monkeypatch: pytest.MonkeyPatch) -> None:
    import main

    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    trigger = main._trigger({"Workspace": None}, "roman")
    assert isinstance(trigger(), loop.Work)            # repeat: never runs dry
    monkeypatch.setenv("SIS_LOOP_TRIGGER", "model-error")
    monkeypatch.setattr(episodic, "get_episodic_store",
                        lambda *a, **k: episodic.NullEpisodicStore())
    trigger = main._trigger({"Workspace": None}, "roman")
    assert trigger() is None                           # no series: nothing to act on
    assert any("model-error trigger: MAE budget 0.15" in line for line in printed)
