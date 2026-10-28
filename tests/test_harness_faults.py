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

import os
import pathlib
import subprocess
import sys
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


# Docker's sandbox runs the image's system Python, which reads a user site
# directory under HOME; a virtualenv's interpreter does not. The probe only
# needs the standard library, so it runs here under the base interpreter too.
_SYSTEM_PYTHON = getattr(sys, "_base_executable", sys.executable)


def _user_site(home: str) -> pathlib.Path:
    """The user site directory a sandboxed interpreter with this ``HOME`` reads."""
    out = subprocess.run(
        [_SYSTEM_PYTHON, "-c", "import site; print(site.getusersitepackages())"],
        env={"HOME": home, "PATH": os.environ.get("PATH", os.defpath)},
        capture_output=True, text=True, check=True,
    )
    return pathlib.Path(out.stdout.strip())


@pytest.mark.parametrize("plant", ["usercustomize", "pth", "probe_out"])
def test_a_candidate_cannot_break_the_probe_through_the_scratch(
    plant: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # L16 (OMNI-65): a candidate that runs with the sandbox's HOME (the SLO
    # gate's does) can leave something there for the probe that follows its
    # failure. The probe must not read it, or the candidate's rejection becomes
    # "harness:" and pages a human about a sandbox that is fine.
    monkeypatch.setattr(sys, "executable", _SYSTEM_PYTHON)
    with tempfile.TemporaryDirectory() as exam, tempfile.TemporaryDirectory() as scratch:
        env = gauntlet._sandbox_env(home=scratch, pythonpath=exam)
        if plant == "probe_out":
            (pathlib.Path(scratch) / "probe_out.txt").mkdir()
        else:
            site_dir = _user_site(scratch)
            site_dir.mkdir(parents=True)
            if plant == "usercustomize":
                (site_dir / "usercustomize.py").write_text("raise SystemExit(3)\n")
            else:
                (site_dir / "planted.pth").write_text("import sys; sys.exit(3)\n")
        assert gauntlet.probe_sandbox(exam, env) is None


def test_the_probe_runs_with_a_scratch_of_its_own(
    docker_mode: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same property in docker mode, where HOME is what gets mounted
    # read-write: the probe's must be a fresh directory, not the gates'.
    seen: dict[str, str] = {}

    def run(inner: list[str], tmpdir: str, env: dict[str, str], **_: Any
            ) -> subprocess.CompletedProcess[str]:
        seen.update(env)
        (pathlib.Path(env["HOME"]) / "probe_out.txt").write_text(
            gauntlet._PROBE_TOKEN, encoding="utf-8")
        return _cp(0)

    monkeypatch.setattr(gauntlet, "_run", run)
    with tempfile.TemporaryDirectory() as exam, tempfile.TemporaryDirectory() as scratch:
        env = gauntlet._sandbox_env(home=scratch, pythonpath=exam)
        assert gauntlet.probe_sandbox(exam, env) is None
    assert seen["HOME"] not in (scratch, exam)
    assert seen["TMPDIR"] == seen["HOME"]
    assert seen["PYTHONNOUSERSITE"] == "1"
    assert seen["PYTHONPATH"] == exam  # the exam is still what it checks it can read


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
    # Reported, not just asserted: this failed once under full-suite load
    # (2026-09-26) with only "the sandbox probe must not run here" — i.e. some
    # gate failed a good candidate — and no way to tell which. The probe stub
    # raises before validate() returns, so the reason is captured here instead.
    monkeypatch.setattr(gauntlet, "_attribute", lambda failure, gate, ctx: failure)
    result = gauntlet.validate(GOOD)
    assert result.passed, f"a good candidate failed at gate: {result.reason}"


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
