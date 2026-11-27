"""scripts/capture_sensor.py — fetch the real sensor's source once, and append to its trace.

The capture half of the EIA sensor (OMNI-33): downloads EIA's bulk petroleum
file (about 56 MB, no key), reads the declared series through the sanitising
door, and appends what is new or changed to ``runtime/sensor_traces/eia.jsonl``.
The first run backfills every series' history, with ``known_at`` reconstructed
from EIA's release schedule; later runs add each new week, known at the time
it was fetched. Run it after each Wednesday's release.

It then derives the refinery-outage events from the trace (``sis.events``,
OMNI-227) into ``runtime/sensor_traces/events.jsonl``. ``--no-events`` skips
that; an event capture that fails is printed and leaves the EIA capture as it
is.

After the capture it scores the predictor on the weeks that became scorable
(``scripts/score_capture.py``, OMNI-198) and keeps them in the episodic
store's error series. ``--no-score`` skips that. A scoring that fails is
printed and leaves the capture as it is; the exit code is the capture's.

Usage:
    poetry run python scripts/capture_sensor.py [--no-events] [--no-score]

Needs the ``real`` group (``poetry install --with real``). Exits 1 if any value
was refused, a period the trace holds was no longer sent, or a declared series
was missing from the file: what was accepted is kept either way, and the
capture line in the trace says what was not.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

# Allow running as a plain script: put the repo root (parent of scripts/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sis.adapters_real import EIA_RULES, EIASensor  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/capture_sensor.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-score", action="store_true",
                        help="capture only; do not score the predictor afterwards")
    parser.add_argument("--no-events", action="store_true",
                        help="do not derive the refinery-outage events afterwards")
    args = parser.parse_args(argv)
    sensor = EIASensor()
    report = sensor.capture()
    print(report.report())
    print(f"trace: {sensor.trace}")
    if not args.no_events:
        events(sensor.trace)
    if not args.no_score:
        score(sensor.trace)
    return 1 if report.refused or report.vanished or report.missing else 0


def events(trace: pathlib.Path) -> None:
    """Derive the event series from *trace*. Never fails the capture: it is kept."""
    from sis.events import EventSensor

    try:
        sensor = EventSensor(eia_trace=trace)
        print(sensor.capture().report())
        print(f"events trace: {sensor.trace}")
    except Exception as exc:  # noqa: BLE001 - the EIA capture is appended; say why not this
        print(f"event capture failed, the EIA capture is kept: {type(exc).__name__}: {exc}",
              file=sys.stderr)


def score(trace: pathlib.Path) -> None:
    """Score what the capture made scorable. Never fails the capture: it is kept."""
    from sis import config, episodic, error_series
    from sis.adapters import InMemoryTelemetry

    try:
        store = episodic.get_episodic_store(str(config.get("episodic.store")))
        scored = error_series.score_trace(trace, rules=EIA_RULES,
                                          predictor=error_series.live_predictor(),
                                          store=store, telemetry=InMemoryTelemetry())
        print(scored.report())
    except Exception as exc:  # noqa: BLE001 - the capture is appended; say why scoring was not
        print(f"scoring failed, the capture is kept: {type(exc).__name__}: {exc}",
              file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
