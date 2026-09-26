"""sis.version — which code is running, for provenance (OMNI-63).

A run's record has to name the code that produced it. The AWS box used to clone
whatever ``develop`` held at ``tofu apply`` time, so the episodic log synced to
S3 could not say which of 160-odd unreleased commits made its decisions.
:func:`code_version` answers that from the checkout itself; ``org.bootstrap``
records it in the SelfModel's provenance and the episodic store.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from sis.paths import PROJECT_ROOT


def code_version(repo: Path = PROJECT_ROOT) -> dict[str, str]:
    """``{"sha": ..., "describe": ...}`` for *repo*'s checkout; "unknown" if not a repo.

    ``describe`` is ``git describe --tags --always --dirty`` — ``v0.2.0`` on a
    release, ``v0.2.0-3-gabc1234`` three commits past one, and ``-dirty`` when
    the working tree has uncommitted changes, which on a run box is worth
    knowing before trusting anything it reports.
    """
    return {
        "sha": _git(repo, "rev-parse", "HEAD"),
        "describe": _git(repo, "describe", "--tags", "--always", "--dirty"),
    }


def _git(repo: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(repo), *args],
                             check=True, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"
