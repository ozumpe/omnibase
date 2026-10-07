"""OMNI-151 (KNOWN_ISSUES M28): one loop per box.

AWS run #7 had two loops on one box, each with its own CEO restored from one
state file, so neither enforced the spend cap for both.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import main
from sis import org, run_lock
from sis.paths import PROJECT_ROOT


def test_a_second_taker_is_refused_and_told_who_holds_it(tmp_path: Path) -> None:
    lock = tmp_path / "loop.lock"
    held = run_lock.acquire(lock)
    try:
        with pytest.raises(run_lock.AlreadyRunning, match=f"pid={os.getpid()} "):
            run_lock.acquire(lock)
    finally:
        held.close()


def test_closing_the_lock_releases_it(tmp_path: Path) -> None:
    lock = tmp_path / "loop.lock"
    run_lock.acquire(lock).close()
    run_lock.acquire(lock).close()


def test_a_killed_holder_leaves_no_stale_lock(tmp_path: Path) -> None:
    # The kernel drops a flock when its process dies, SIGKILL included: no lock
    # file to clear by hand after a crash.
    lock = tmp_path / "loop.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import pathlib, sys, time; from sis import run_lock; "
         f"h = run_lock.acquire(pathlib.Path({str(lock)!r})); "
         "print('locked', flush=True); time.sleep(60)"],
        cwd=PROJECT_ROOT, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        with pytest.raises(run_lock.AlreadyRunning, match=f"pid={holder.pid} "):
            run_lock.acquire(lock)
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)
    run_lock.acquire(lock).close()


def test_main_refuses_a_second_loop_before_any_cluster_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    lock = tmp_path / "loop.lock"
    monkeypatch.setattr(run_lock, "LOCK_FILE", lock)
    monkeypatch.setattr(sys, "argv", ["main.py", "--loop"])

    def _no_bootstrap() -> Any:
        raise AssertionError("bootstrap reached: the second loop was not refused")

    monkeypatch.setattr(org, "bootstrap", _no_bootstrap)
    held = run_lock.acquire(lock)
    try:
        with pytest.raises(SystemExit) as exited:
            main.main()
        assert exited.value.code == 3
    finally:
        held.close()
