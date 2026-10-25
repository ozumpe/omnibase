"""scripts/prediction_error.py — how wrong a predictor was, week by week (OMNI-36).

    poetry run python scripts/prediction_error.py                       # autumn 2022, the EIA trace
    poetry run python scripts/prediction_error.py --trace PATH --from 2022-08-01 --to 2022-12-01
    poetry run python scripts/prediction_error.py --scenario supply_shock --seed 7
    poetry run python scripts/prediction_error.py --scenario outage --seed 3 --events missing_week

Replays a recorded trace (read back through the sanitising door) or a
generated scenario, asks the predictor California's next-week price at each
moment a week's price first became known, and prints the signed error per week
with its bias, MAE and RMSE. The predictor is ``runtime/ca_price_persistence.py``
if one has been built, else the shipped persistence stub; ``--predictor`` names
another file. It runs in the sandbox, never in this process.

The errors are also emitted to telemetry (``prediction.error``,
``prediction.summary``). Nothing acts on them: this starts no cycle, files
nothing and pages no one. That is Phase B (D9).

Exit 1 if there is nothing to replay (no trace, an empty window) or the trace
does not read back, with the reason.
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys

# Allow running as a plain script: put the repo root (parent of scripts/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sis import prediction_error, sensor_trace  # noqa: E402
from sis.adapters import InMemoryTelemetry  # noqa: E402
from sis.adapters_real import EIA_RULES, EIA_TRACE  # noqa: E402
from sis.contract import CA_PRICE_PERSISTENCE  # noqa: E402
from sis.paths import PROJECT_ROOT  # noqa: E402
from sis.ports import Reading  # noqa: E402
from sis.sensor_input import Absent  # noqa: E402

AUTUMN_2022 = (datetime.date(2022, 8, 1), datetime.date(2022, 12, 1))


def default_predictor() -> pathlib.Path:
    """The built predictor if there is one, else the shipped stub."""
    built = PROJECT_ROOT / CA_PRICE_PERSISTENCE.entry_module
    if built.exists():
        return built
    assert CA_PRICE_PERSISTENCE.stub_candidate_path is not None
    return PROJECT_ROOT / CA_PRICE_PERSISTENCE.stub_candidate_path


def _day(text: str) -> datetime.datetime:
    return datetime.datetime.combine(datetime.date.fromisoformat(text), datetime.time(),
                                     tzinfo=datetime.UTC)


def load(args: argparse.Namespace) -> tuple[str, tuple[Reading, ...], tuple[Absent, ...]]:
    """The source label, readings and absences the arguments name."""
    if args.scenario is not None:
        from sis import scenarios

        generated = scenarios.generate(args.scenario, seed=args.seed,
                                       data_events=tuple(args.events))
        return (f"scenario {args.scenario}:{args.seed}", generated.readings,
                generated.absent)
    lines = sensor_trace.load(args.trace)
    if not lines:
        raise sensor_trace.TraceRefused(
            f"no trace at {args.trace}: capture one first (scripts/capture_sensor.py)")
    readings, absent = sensor_trace.read_back(lines, EIA_RULES)
    return f"trace {args.trace}", tuple(readings), tuple(absent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/prediction_error.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trace", type=pathlib.Path, default=EIA_TRACE)
    parser.add_argument("--scenario", help="a generated scenario kind instead of a trace")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--events", nargs="*", default=[],
                        help="data events for --scenario (missing_week, republished, withdrawn)")
    parser.add_argument("--from", dest="start", help="first decision day, YYYY-MM-DD "
                        "(a trace defaults to autumn 2022, a scenario to all of it)")
    parser.add_argument("--to", dest="end", help="day after the last decision, YYYY-MM-DD")
    parser.add_argument("--predictor", type=pathlib.Path, default=None)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    start = _day(args.start) if args.start else None
    end = _day(args.end) if args.end else None
    if args.scenario is None and args.start is None and args.end is None:
        start, end = (_day(day.isoformat()) for day in AUTUMN_2022)
    predictor = args.predictor or default_predictor()
    try:
        source, readings, absent = load(args)
    except ValueError as exc:  # TraceRefused is one
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    asked = prediction_error.questions(readings, absent, start=start, end=end)
    if not asked:
        print(f"refused: {source} asks no question in that window", file=sys.stderr)
        return 1
    answers = prediction_error.ask_in_sandbox(
        predictor.read_text(encoding="utf-8"), CA_PRICE_PERSISTENCE.entry, asked,
        timeout_s=args.timeout)
    units = {r.unit for r in readings if r.series == prediction_error.CALIFORNIA}
    report = prediction_error.score(asked, answers, unit=", ".join(sorted(units)),
                                    source=source)
    prediction_error.publish(report, InMemoryTelemetry(), ref=source)
    shown = predictor.relative_to(PROJECT_ROOT) if predictor.is_relative_to(PROJECT_ROOT) \
        else predictor
    print(f"{shown} on {source}")
    print(prediction_error.table(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
