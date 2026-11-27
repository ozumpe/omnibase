"""scripts/export_site_data.py — write the landing page's data from one run's records (OMNI-229).

Reads a run's episodic log (and the error series beside it, if any) and writes
``site/data/site.json``, the one file the public page reads. What may go
through is decided in ``sis.site_export``, which refuses a licensed series and
never copies a gate's reason: see its docstring.

Usage:
    poetry run python scripts/export_site_data.py \\
        --episodic runtime/episodic.jsonl [--contract ca_price_wholesale] \\
        [--budget 2.50] [--code-version v0.9.0] [--out site/data/site.json]

The error series is read from ``<episodic stem>_scored.jsonl`` next to the log.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
from typing import Any

# Allow running as a plain script: put the repo root (parent of scripts/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sis.episodic import JsonlEpisodicStore  # noqa: E402
from sis.prediction_error import CALIFORNIA  # noqa: E402
from sis.site_export import ExportRefused, page_data  # noqa: E402

REPO_URL = "https://github.com/ozumpe/omnibase"
TEST_REPO_URL = "https://github.com/ozumpe/testrun"
DEFAULT_OUT = pathlib.Path(__file__).resolve().parent.parent / "site" / "data" / "site.json"


def _records(path: pathlib.Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            if isinstance(record, dict):
                out.append(record)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/export_site_data.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episodic", type=pathlib.Path, required=True,
                        help="the run's episodic.jsonl")
    parser.add_argument("--contract", default="ca_price_wholesale")
    parser.add_argument("--budget", type=float, default=2.50, help="the run's budget in USD")
    parser.add_argument("--code-version", default="", help="the release the run used")
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    store = JsonlEpisodicStore(args.episodic)
    try:
        data = page_data(
            records=_records(args.episodic), series=store.scored(), contract=args.contract,
            target=CALIFORNIA, budget_usd=args.budget,
            generated_at=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            code_version=args.code_version, repo_url=REPO_URL, test_repo_url=TEST_REPO_URL)
    except ExportRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}: {len(data['tried'])} cycle(s), "
          f"{len(data['scoreboard']['weeks'])} scored week(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
