"""sis.memory — append-only JSONL episodic log.

Every cycle appends one record: prompt → diff → metrics → outcome.
"""

import json
from typing import Any

from sis.paths import LOG_PATH


def append(record: dict[str, Any]) -> None:
    """Append *record* as a single JSON line to the episodic log."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def read_all() -> list[dict[str, Any]]:
    """Return all records in chronological order."""
    if not LOG_PATH.exists():
        return []
    records: list[dict[str, Any]] = []
    with LOG_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records
