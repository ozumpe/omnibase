"""sis.run_lock — one loop per box (OMNI-151, KNOWN_ISSUES M28).

Two processes running ``main.py`` on one box each bootstrap their own Ray
cluster and their own CEO, whose brakes are restored from the same
``runtime/episodic_state.json``. Neither enforces the spend cap for both, the
persisted spend is whichever wrote last, and both append to one episodic log
and one console log. AWS run #7 had one of each: a loop started with ``nohup``
inside tmux survived the attempt to stop it and ran beside the next.

The guard is an exclusive ``flock`` on ``runtime/loop.lock``, held for the life
of the process. The kernel drops it when the process exits, however it exits,
so a crash or a SIGKILL leaves no stale lock to clear by hand. The file says
who holds it, for the refusal a second start prints.
"""

from __future__ import annotations

import fcntl
import os
import sys
import time
from pathlib import Path
from typing import IO

from sis.paths import RUNTIME_DIR

LOCK_FILE = RUNTIME_DIR / "loop.lock"


class AlreadyRunning(RuntimeError):
    """Another process on this box holds the run lock."""


def acquire(path: Path | None = None) -> IO[str]:
    """Take the run lock, or raise :class:`AlreadyRunning` naming its holder.

    Keep the returned file open for as long as the run lasts: closing it, or
    the process ending, releases the lock.
    """
    path = path or LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")  # noqa: SIM115 - held for the run's life
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        holder = " ".join(handle.read().split()) or "holder unknown"
        handle.close()
        raise AlreadyRunning(
            f"another loop is running on this box ({holder}). Two loops would not "
            "share the spend cap (M28). Stop it first, with Ctrl-C in its tmux pane; "
            "`pgrep -af main.py` lists it."
        ) from None
    handle.seek(0)
    handle.truncate()
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    handle.write(f"pid={os.getpid()} started={started} "
                 f"cmd={' '.join(sys.argv)[:200]}\n")
    handle.flush()
    return handle
