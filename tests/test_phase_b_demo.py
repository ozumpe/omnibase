"""OMNI-199: Phase B's demo, a supply shock that breaches and starts a cycle.

The demo is ``scripts/phase_b_demo.py``. These tests pin its outcome for a
fixed seed under the shipped ``model_error`` defaults: the shock starts one
error-budget cycle and pages once, and a quiet market starts none. The
predictor is the shipped stub, asked in the subprocess sandbox.
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import sys
import types
from collections.abc import Iterator

import pytest

from sis import config
from sis.paths import PROJECT_ROOT


@pytest.fixture(autouse=True)
def _isolate_config() -> Iterator[None]:
    saved = {key.env: os.environ.get(key.env) for key in config.SCHEMA}
    config.clear_cli_overrides()
    config.reset_config_cache()
    yield
    config.clear_cli_overrides()
    config.reset_config_cache()
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(scope="module")
def demo() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "phase_b_demo", PROJECT_ROOT / "scripts" / "phase_b_demo.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses look their module up in sys.modules while they are built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _replay(demo: types.ModuleType, kind: str, seed: int, options: dict[str, float],
            budget: float | None = None) -> types.SimpleNamespace:
    if budget is not None:
        config.apply_cli_overrides({"model_error.mae_budget": budget})
        config.reset_config_cache()
    settings = config.config().model_error
    steps = demo.scored(kind, seed, options, weeks=demo.WEEKS, ask=demo._sandbox_ask(120.0))
    result = demo.replay(kind, steps, settings)
    return types.SimpleNamespace(result=result, steps=steps)


def test_the_supply_shock_starts_one_error_budget_cycle_and_pages_once(
        demo: types.ModuleType) -> None:
    run = _replay(demo, *demo.SHOCK)
    [cycle] = run.result.cycles
    assert cycle.trigger == "error_budget"
    assert cycle.title == demo.TITLE
    assert len(run.result.pages) == 1
    fired = [w for w in run.result.weeks if w.fired]
    assert [w.week for w in fired] == [datetime.date(2019, 3, 11)]
    assert cycle.evidence["error_budget"]["through"] == "2019-03-11"


def test_the_quiet_market_starts_nothing(demo: types.ModuleType) -> None:
    run = _replay(demo, *demo.QUIET)
    assert run.result.cycles == [] and run.result.pages == []
    assert "[loop] no cycle started" in demo.table(run.result)


def test_the_trigger_sees_one_more_week_each_tick(demo: types.ModuleType) -> None:
    run = _replay(demo, *demo.SHOCK)
    assert [w.week for w in run.result.weeks] == [s.week.date() for s in run.steps]
    early = [w for w in run.result.weeks if w.week < datetime.date(2019, 3, 11)]
    assert early and not any(w.fired for w in early)


def test_a_looser_budget_lets_the_shock_pass(demo: types.ModuleType) -> None:
    run = _replay(demo, *demo.SHOCK, budget=0.30)
    assert run.result.cycles == []


def test_no_steps_is_no_cycle(demo: types.ModuleType) -> None:
    result = demo.replay("empty", [], config.config().model_error)
    assert result.weeks == [] and result.cycles == []


def test_main_passes_on_the_defaults(demo: types.ModuleType,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    assert demo.main([]) == 0
    out = capsys.readouterr().out
    assert "model_error in force: mae_budget 0.15" in out
    assert out.count(">>> CYCLE (error_budget)") == 1
    assert "(D4)" in out


def test_main_says_which_scenario_failed(demo: types.ModuleType,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    assert demo.main(["--model-error-mae-budget", "0.30"]) == 1
    assert "the supply shock started no cycle" in capsys.readouterr().err


def test_main_refuses_a_real_proposer(demo: types.ModuleType,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    assert demo.main(["--proposer-backend", "llm"]) == 1
    assert "stub proposer" in capsys.readouterr().err


def test_main_rejects_an_unknown_flag(demo: types.ModuleType) -> None:
    with pytest.raises(SystemExit):
        demo.main(["--bogus"])
