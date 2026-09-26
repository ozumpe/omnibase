"""CI fast path (OMNI-60): classify a PR's diff, and decide the required `test` check.

`.github/workflows/ci.yml` runs lint, the fast suite and the Serve suite as
parallel jobs, and skips all three for a docs-only PR. Two decisions make that
safe, and both live here as pure functions so they are unit-tested rather than
trusted to YAML expressions (``tests/test_ci_gate.py``):

- :func:`docs_only` — does this diff touch nothing but documentation? An
  *allowlist* of suffixes, so a file type nobody thought about runs everything.
- :func:`verdict` — did the jobs this diff needed succeed? It backs the single
  required status check, ``test``. That check must always report (a skipped
  required check leaves a PR waiting forever, which is why this is not
  ``paths-ignore``), and it must never go green because the jobs that should
  have run were skipped — so it checks that the *right* jobs succeeded, not
  merely that none failed.

A docs-only diff is not an untested one: the ``docs`` job still runs every test
marked ``docs`` — the ones that read the repository's Markdown (a doc naming a
``SIS_*`` variable the config schema lacks, a KNOWN_ISSUES entry without its
ticket). ``tests/test_test_layout.py`` keeps that marker honest.

Stdlib only: the classifier job runs before any dependency is installed.

    python scripts/ci_gate.py changes <base-sha> <head-sha>   # -> docs_only=true|false
    python scripts/ci_gate.py verdict                         # reads $NEEDS (toJSON(needs))
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

# Suffixes a docs-only PR may touch. Deliberately narrow: `.txt` is out
# (requirements files), and so is everything under docs/ that is not one of
# these — a new kind of file there runs the full suite until someone decides
# otherwise.
DOCS_SUFFIXES = frozenset({".md", ".svg", ".png"})

CODE_JOBS = ("lint", "fast", "serve")
DOCS_JOBS = ("docs",)


def docs_only(paths: Sequence[str]) -> bool:
    """True if every changed path is documentation. An empty diff is not."""
    return bool(paths) and all(
        PurePosixPath(path).suffix.lower() in DOCS_SUFFIXES for path in paths
    )


def verdict(needs: Mapping[str, Mapping[str, Any]]) -> tuple[bool, str]:
    """Whether the required ``test`` check passes, and why. Pure.

    *needs* is GitHub's ``needs`` context: job id → ``{"result": ..., "outputs":
    {...}}``. The classifier must have succeeded; then every job its answer
    selected must have *succeeded* — skipped counts as missing, not passed.
    """
    changes = needs.get("changes", {})
    if changes.get("result") != "success":
        return False, f"the diff classifier did not succeed ({changes.get('result')!r})"
    is_docs = changes.get("outputs", {}).get("docs_only") == "true"
    required = DOCS_JOBS if is_docs else CODE_JOBS
    missing = [
        f"{job}={needs.get(job, {}).get('result', 'absent')}"
        for job in required
        if needs.get(job, {}).get("result") != "success"
    ]
    kind = "docs-only" if is_docs else "code"
    if missing:
        return False, f"{kind} change, but required jobs did not succeed: {', '.join(missing)}"
    return True, f"{kind} change: {', '.join(required)} passed"


def _changed_paths(base: str, head: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def main(argv: Sequence[str]) -> int:
    if len(argv) == 3 and argv[0] == "changes":
        paths = _changed_paths(argv[1], argv[2])
        result = "true" if docs_only(paths) else "false"
        print(f"{len(paths)} changed path(s); docs_only={result}")
        for path in paths:
            print(f"  {path}")
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with open(output, "a", encoding="utf-8") as fh:
                fh.write(f"docs_only={result}\n")
        return 0
    if len(argv) == 1 and argv[0] == "verdict":
        ok, reason = verdict(json.loads(os.environ["NEEDS"]))
        print(reason)
        return 0 if ok else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
