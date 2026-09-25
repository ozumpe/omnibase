"""OMNI-37: a broken sandbox is the harness's fault, never the candidate's.

Found in the OMNI-29 rehearsal: the docker sandbox could not read its own temp
dir, mypy said "cannot read file", and the gauntlet reported ``mypy --strict
failed`` — every real proposal would have been billed, filed as a bug and
counted toward the breaker as the model's fault.

Two shapes are pinned here, per the ticket: docker failing to start a
container (exit 125), and a sandbox whose temp dir can't be read. Plus the
property that makes the fix safe rather than an escape hatch: an exit code is
never trusted on its own, so a candidate that exits 125 is still judged.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from typing import Any

import pytest

from sis import gauntlet
from sis.episodic import gate_from_reason
from sis.paths import OPTIMISED_CANDIDATE_PATH
from sis.roles import failure_weight

GOOD = OPTIMISED_CANDIDATE_PATH.read_text(encoding="utf-8")

RealRun = Callable[..., subprocess.CompletedProcess[str]]
_REAL_RUN: RealRun = gauntlet._run


def _script(inner: list[str]) -> str:
    return " ".join(inner)


def _fake_run(
    decide: Callable[[list[str]], subprocess.CompletedProcess[str] | None],
) -> RealRun:
    """A ``_run`` that answers from *decide*, falling through to the real sandbox."""
    def run(inner: list[str], tmpdir: str, env: dict[str, str], **kw: Any
            ) -> subprocess.CompletedProcess[str]:
        return decide(inner) or _REAL_RUN(inner, tmpdir, env, **kw)
    return run


def _cp(code: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["python"], code, "", stderr)


@pytest.fixture
def docker_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the sandbox is docker, without needing a daemon or a non-root uid."""
    monkeypatch.setattr(gauntlet, "sandbox_mode", lambda: "docker")
    monkeypatch.setattr(gauntlet, "ensure_sandbox_ready", lambda: None)


def _no_probe(*_: Any, **__: Any) -> str | None:
    raise AssertionError("the sandbox probe must not run here")


# --- the probe ------------------------------------------------------------


def test_a_healthy_sandbox_passes_the_probe() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        env = gauntlet._sandbox_env(home=tmpdir, pythonpath=tmpdir)
        assert gauntlet.probe_sandbox(tmpdir, env) is None


# --- shape 1: docker cannot start a container -----------------------------


def test_docker_failing_to_start_is_a_harness_fault_not_a_mypy_failure(
    docker_mode: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon_down = _cp(125, "docker: Error response from daemon: failed to create task")
    monkeypatch.setattr(gauntlet, "_run", _fake_run(lambda inner: daemon_down))

    result = gauntlet.validate(GOOD)

    assert not result.passed
    assert gate_from_reason(result.reason) == "harness"
    assert "docker could not run the container" in result.reason
    assert "exited 125" in result.reason
    # The gate's original verdict is kept for whoever debugs it.
    assert any("mypy --strict failed" in line for line in result.errors)


# --- shape 2: the sandbox cannot read its temp dir (the actual OMNI-29 bug) --


def test_an_unreadable_temp_dir_is_a_harness_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    denied = "PermissionError: [Errno 13] Permission denied"

    def decide(inner: list[str]) -> subprocess.CompletedProcess[str] | None:
        if "_sis_probe" in _script(inner):
            return _cp(1, f"{denied}: '/tmp/x/_sis_probe/probe_in.txt'")
        if "mypy" in inner:  # how it surfaced: mypy's own "can't read file"
            return _cp(2, f"error: cannot read file 'target.py': {denied}")
        return None

    monkeypatch.setattr(gauntlet, "_run", _fake_run(decide))
    result = gauntlet.validate(GOOD)

    assert gate_from_reason(result.reason) == "harness"
    assert "after the mypy gate failed" in result.reason
    assert "Permission denied" in result.reason


# --- the exit code is a hint, never the decision --------------------------


def test_a_candidate_that_exits_125_is_still_judged() -> None:
    """The reason the probe decides and the exit code doesn't.

    Docker passes a container's exit code straight through, so this candidate
    produces docker's "could not start" number from inside the sandbox. If
    that alone meant "harness", a bad candidate could launder its failure out
    of the circuit breaker. The probe passes, so the verdict stays its own.
    """
    exits = GOOD + "\nimport sys\nsys.exit(125)\n"
    result = gauntlet.validate(exits)
    assert not result.passed
    assert gate_from_reason(result.reason) != "harness"


# --- where the probe does not run -----------------------------------------


def test_a_passing_candidate_is_never_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole cost of the fix sits on the failure path.
    monkeypatch.setattr(gauntlet, "probe_sandbox", _no_probe)
    assert gauntlet.validate(GOOD).passed


def test_an_in_process_gate_failure_is_not_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    # ast.parse never touched the sandbox, so the sandbox can't be to blame.
    monkeypatch.setattr(gauntlet, "probe_sandbox", _no_probe)
    result = gauntlet.validate("def broken(:\n")
    assert gate_from_reason(result.reason) == "ast"


def test_a_timeout_stays_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gauntlet, "probe_sandbox", _no_probe)
    hung = _cp(124, "gauntlet sandbox timed out after 120s")
    monkeypatch.setattr(gauntlet, "_run", _fake_run(lambda inner: hung))
    assert gate_from_reason(gauntlet.validate(GOOD).reason) == "timeout"


def test_an_existing_harness_verdict_is_not_reprobed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gauntlet, "probe_sandbox", _no_probe)
    failure = gauntlet.Result(passed=False, reason="harness: comparators missing")
    ctx = gauntlet._GateContext(
        contract=gauntlet.default_contract(), code_str="", tmp=gauntlet.pathlib.Path("."),
        tmpdir=".", env={}, candidate=gauntlet.pathlib.Path("x.py"),
    )
    assert gauntlet._attribute(failure, gauntlet.GateName.BACKTEST, ctx) is failure


# --- the advisory baseline says why it is 0.0 ------------------------------


def test_measure_baseline_names_a_broken_sandbox(
    docker_mode: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(gauntlet, "_run", _fake_run(lambda inner: _cp(125, "daemon down")))
    assert gauntlet.measure_baseline() == 0.0
    assert "sandbox itself is broken" in capsys.readouterr().err


# --- the breaker decision --------------------------------------------------


def test_a_harness_fault_still_counts_in_full_toward_the_breaker() -> None:
    # Documented choice: a broken sandbox fails every cycle, and each one
    # spends on a proposal first, so stopping the loop after N is right. What
    # OMNI-37 changes is who the bug blames, not whether it counts.
    assert failure_weight("harness", slo_failure_weight=0.5) == 1.0
