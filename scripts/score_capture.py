"""scripts/score_capture.py — score the predictor on what a capture made scorable (OMNI-198).

    poetry run python scripts/score_capture.py                  # the EIA trace, the live predictor
    poetry run python scripts/score_capture.py --trace PATH --from 2022-08-01

Reads the trace back through the door, asks the predictor (in the sandbox)
about each week that has become scorable since the last scoring, and appends
one row per week to the episodic store's error series
(``runtime/episodic_scored.jsonl``, or the duckdb table ``scored_weeks``). A
week already in the series is not asked again; a later vintage of its actual
is a revision row. ``scripts/capture_sensor.py`` runs this after each capture.

The predictor is ``runtime/ca_price_persistence.py`` if one has been built,
else the shipped persistence stub; ``--predictor`` names another file. New
weeks start where the series starts, or with no series yet 13 weeks before the
trace's last question; ``--from`` sets the start. Nothing acts on the series
yet: that is OMNI-197.

Exit 1 if there is no trace, it does not read back, or the store is ``none``.
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys

# Allow running as a plain script: put the repo root (parent of scripts/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sis import config, episodic, error_series  # noqa: E402
from sis.adapters import InMemoryTelemetry  # noqa: E402
from sis.adapters_real import EIA_RULES, EIA_TRACE  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/score_capture.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trace", type=pathlib.Path, default=EIA_TRACE)
    parser.add_argument("--predictor", type=pathlib.Path, default=None)
    parser.add_argument("--from", dest="start",
                        help="the first decision day to score, YYYY-MM-DD (default: where "
                        "the series starts, or 13 weeks before the last question)")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    since = datetime.datetime.combine(datetime.date.fromisoformat(args.start), datetime.time(),
                                      tzinfo=datetime.UTC) if args.start else None
    store = episodic.get_episodic_store(str(config.get("episodic.store")))
    try:
        scored = error_series.score_trace(
            args.trace, rules=EIA_RULES, predictor=args.predictor or error_series.live_predictor(),
            store=store, since=since, timeout_s=args.timeout, telemetry=InMemoryTelemetry())
    except ValueError as exc:  # TraceRefused is one
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(scored.report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
