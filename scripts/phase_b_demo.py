"""scripts/phase_b_demo.py — Phase B's milestone: a supply shock starts a cycle (OMNI-199).

    poetry run python scripts/phase_b_demo.py                  # supply shock, then a quiet market
    poetry run python scripts/phase_b_demo.py --seed 3 --model-error-mae-budget 0.20
    poetry run python scripts/phase_b_demo.py --cycle          # also run the cycle, stub proposer

Replays a generated scenario week by week. Each week the predictor (the built
``runtime/ca_price_persistence.py`` if any, else the shipped stub, in the
sandbox) is scored on the week that just became scorable, and
``main.py --loop --loop-trigger model-error``'s own trigger
(:func:`sis.loop.model_error_trigger`) is ticked through :func:`sis.loop.run_loop`
on the series so far. The table shows each week's error, the window the
trigger judged and its verdict; a sustained breach pages and starts a cycle.
Then a quiet scenario is replayed the same way and starts nothing.

The ``model_error.*`` settings in force are printed, and every one can be
overridden as for ``main.py`` (``--model-error-mae-budget``,
``--model-error-breach-weeks``, ...). No credentials, no network, no LLM:
the page goes to the console, and without ``--cycle`` the cycle is shown, not
run. With ``--cycle`` each started cycle runs in-memory with the stub
proposer.

**A scenario refutes, it never promotes (D4).** The scenario only starts the
cycle. The cycle's candidate is judged by the contract's own gates on its
held-out history, never on the scenario's prices, and a promotion still
follows only a human merge.

Exit 0 if the shock started at least one cycle and the quiet scenario none;
1 otherwise, saying which.
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

# Allow running as a plain script: put the repo root (parent of scripts/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sis import config, error_series, loop, model_error, prediction_error, scenarios  # noqa: E402
from sis.contract import CA_PRICE_PERSISTENCE  # noqa: E402
from sis.prediction_error import Question, Step  # noqa: E402

Scenario = tuple[str, int, dict[str, Any]]
SHOCK: Scenario = ("supply_shock", 1, {})
# A small national move: the California price drifts and persistence keeps up.
QUIET: Scenario = ("national_move", 1, {"magnitude": 0.20})
WEEKS = 26
TITLE = "Improve the California price predictor"
BODY = ("The predictor's error has been out of bounds for long enough: propose a better "
        "ca_price_persistence.")

Ask = Callable[[Sequence[Question]], Sequence[Any]]


@dataclass(frozen=True)
class Week:
    """One week of the replay: its error, and what the trigger made of the series so far."""

    week: datetime.date
    error: float | None
    window_mae: float | None
    verdict: str
    fired: str = ""


@dataclass
class Replay:
    """A scenario replayed week by week through scoring and the trigger."""

    label: str
    weeks: list[Week] = field(default_factory=list)
    pages: list[tuple[str, str]] = field(default_factory=list)
    cycles: list[loop.Work] = field(default_factory=list)


def scored(kind: str, seed: int, options: dict[str, Any], *, weeks: int,
           ask: Ask) -> list[Step]:
    """The predictor's scored steps on the scenario, in week order."""
    generated = scenarios.generate(kind, seed=seed, weeks=weeks, **options)
    asked = prediction_error.questions(generated.readings, generated.absent)
    report = prediction_error.score(asked, list(ask(asked)))
    return sorted(report.steps, key=lambda step: step.week)


def replay(label: str, steps: Sequence[Step], settings: Any, *,
           run_cycle: Callable[[loop.Work], dict[str, Any]] | None = None) -> Replay:
    """Tick the --loop trigger once a week over *steps*, through :func:`loop.run_loop`.

    At week *i* the series holds the steps up to *i*: a week is scored once
    its next week's price is known, so the trigger never sees a week early.
    """
    budget, floor, window = loop.model_error_rules(settings)
    out = Replay(label)
    shown: list[Step] = []
    trigger = loop.model_error_trigger(lambda: list(shown), budget=budget, floor=floor,
                                       window_weeks=window, title=TITLE, body=BODY,
                                       page=lambda t, b: out.pages.append((t, b)))
    pending = list(steps)
    fired: list[str] = []

    if not pending:
        return out

    def poll() -> loop.Tick:
        shown.append(pending.pop(0))
        return loop.Tick(breaker_open=False, budget_ok=True, work=trigger())

    def cycle(work: loop.Work) -> dict[str, Any]:
        out.cycles.append(work)
        fired.append(work.trigger)
        return run_cycle(work) if run_cycle is not None else {"status": "shown"}

    stop = threading.Event()

    def after_tick(_: float) -> None:
        decisions = model_error.replay(shown, budget, floor, window_weeks=window)
        last = decisions[-1][0] if decisions else None
        step = shown[-1]
        out.weeks.append(Week(week=step.week.date(), error=step.error,
                              window_mae=last.stats.mae if last else None,
                              verdict=last.reason if last else "fewer weeks than one window",
                              fired=fired.pop() if fired else ""))
        if not pending:
            stop.set()

    loop.run_loop(poll, cycle, interval_s=0, stop_event=stop, sleep=after_tick)
    return out


def settings_line(settings: Any) -> str:
    budget, floor, window = loop.model_error_rules(settings)
    return (f"model_error in force: mae_budget {budget.mae_budget:g} (acceptable error), "
            f"breach_weeks {settings.breach_weeks} (how long: {budget.windows} window(s) of "
            f"{window} week(s) that share no week), skill_floor {floor.skill_floor:+g}, "
            f"min_weeks {budget.min_scored}")


def table(result: Replay) -> str:
    lines = [f"--- {result.label}", "week        |error|  window MAE  trigger"]
    for w in result.weeks:
        err = f"{abs(w.error):6.3f}" if w.error is not None else "     -"
        mae = f"{w.window_mae:10.3f}" if w.window_mae is not None else "         -"
        mark = f"  >>> CYCLE ({w.fired})" if w.fired else ""
        lines.append(f"{w.week.isoformat()}  {err}  {mae}  {w.verdict}{mark}")
    for title, body in result.pages:
        lines.append(f"[page WARNING] {title}: {body}")
    for work in result.cycles:
        lines.append(f"[cycle] {work.title} ({work.trigger}), evidence {work.evidence}")
    if not result.cycles:
        lines.append("[loop] no cycle started")
    return "\n".join(lines)


def _sandbox_ask(timeout_s: float) -> Ask:
    source = error_series.live_predictor().read_text(encoding="utf-8")

    def ask(asked: Sequence[Question]) -> list[Any]:
        return prediction_error.ask_in_sandbox(source, CA_PRICE_PERSISTENCE.entry, asked,
                                               timeout_s=timeout_s)

    return ask


def _org_cycle() -> Callable[[loop.Work], dict[str, Any]]:
    """Runs a started cycle in-memory with the stub proposer, Ray started on first use."""
    from sis import org

    handles: list[dict[str, Any]] = []

    def run(work: loop.Work) -> dict[str, Any]:
        if not handles:
            handles.append(org.bootstrap())
        result = org.run_cycle(handles[0], work.title, work.body,
                               contract_name=CA_PRICE_PERSISTENCE.name)
        print(org.cycle_summary(result), flush=True)
        return result

    return run


def main(argv: list[str] | None = None) -> int:
    args_in = sys.argv[1:] if argv is None else argv
    # The model_error.* flags are main.py's own (sis.config), so the demo runs
    # with the values a --loop would use, overrides included.
    overrides = config.parse_cli(args_in)
    config.apply_cli_overrides(overrides)
    parser = argparse.ArgumentParser(prog="scripts/phase_b_demo.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=SHOCK[1], help="the supply shock's seed")
    parser.add_argument("--weeks", type=int, default=WEEKS)
    parser.add_argument("--cycle", action="store_true",
                        help="run each started cycle in-memory with the stub proposer")
    parser.add_argument("--timeout", type=float, default=120.0)
    args, unknown = parser.parse_known_args(args_in)
    if len(unknown) != 2 * len(config.parse_cli(unknown)):
        parser.error(f"unrecognised arguments: {' '.join(unknown)}")
    settings = config.config().model_error
    if config.config().proposer.backend != "stub":
        print("refused: the demo runs only with the stub proposer (proposer.backend stub)",
              file=sys.stderr)
        return 1
    print(settings_line(settings))
    ask = _sandbox_ask(args.timeout)
    run_cycle = _org_cycle() if args.cycle else None
    shock = replay(f"supply_shock seed {args.seed}, {args.weeks} weeks",
                   scored(SHOCK[0], args.seed, SHOCK[2], weeks=args.weeks, ask=ask),
                   settings, run_cycle=run_cycle)
    print(table(shock))
    quiet = replay(f"{QUIET[0]} seed {QUIET[1]} (a quiet market), {args.weeks} weeks",
                   scored(QUIET[0], QUIET[1], QUIET[2], weeks=args.weeks, ask=ask),
                   settings, run_cycle=run_cycle)
    print(table(quiet))
    print("A scenario only starts a cycle: its candidate is judged on the contract's own "
          "held-out history and promoted only after a human merge (D4).")
    problems = ([] if shock.cycles else ["the supply shock started no cycle"]) + (
        [f"the quiet scenario started {len(quiet.cycles)} cycle(s)"] if quiet.cycles else [])
    for problem in problems:
        print(f"[demo] {problem}: try other model_error settings", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
