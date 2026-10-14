"""scripts/capture_sensor.py — fetch the real sensor's source once, and append to its trace.

The capture half of the EIA sensor (OMNI-33): downloads EIA's bulk petroleum
file (about 56 MB, no key), reads the declared series through the sanitising
door, and appends what is new or changed to ``runtime/sensor_traces/eia.jsonl``.
The first run backfills every series' history, with ``known_at`` reconstructed
from EIA's release schedule; later runs add each new week, known at the time
it was fetched. Run it after each Wednesday's release.

Usage:
    poetry run python scripts/capture_sensor.py

Needs the ``real`` group (``poetry install --with real``). Exits 1 if any value
was refused, a period the trace holds was no longer sent, or a declared series
was missing from the file: what was accepted is kept either way, and the
capture line in the trace says what was not.
"""

from __future__ import annotations

import pathlib
import sys

# Allow running as a plain script: put the repo root (parent of scripts/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sis.adapters_real import EIASensor  # noqa: E402


def main() -> int:
    sensor = EIASensor()
    report = sensor.capture()
    print(report.report())
    print(f"trace: {sensor.trace}")
    return 1 if report.refused or report.vanished or report.missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
