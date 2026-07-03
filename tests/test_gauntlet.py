"""Tests for the gauntlet validator."""

from sis import gauntlet
from sis.paths import OPTIMISED_CANDIDATE_PATH, TARGET_PATH

_GOOD_CODE = OPTIMISED_CANDIDATE_PATH.read_text(encoding="utf-8")
_BASELINE = 0.05  # advisory baseline (decision now uses an in-sandbox measurement)

# A candidate that hardcodes exactly the inputs the static pytest suite checks
# (and the old fixed benchmark input) but returns garbage everywhere else.
# It PASSES mypy + pytest, so only the differential-correctness gate can catch it.
_GAMING_CODE = '''
import time

_KNOWN = {1: 1, 6: 12, 12: 28, 28: 56, 7: 8, 13: 14, 496: 992, 8128: 16256}


def sum_of_divisors(n: int) -> int:
    return _KNOWN.get(n, 0)


def benchmark(n: int = 10_000, repetitions: int = 5) -> float:
    return 1e-9  # pretends to be blazing fast
'''


def test_good_code_passes() -> None:
    result = gauntlet.validate(_GOOD_CODE, _BASELINE)
    assert result.passed, f"Expected pass, got: {result.reason}\n{result.errors}"
    assert result.latency_seconds is not None
    assert result.latency_seconds < _BASELINE


def test_syntax_error_fails() -> None:
    result = gauntlet.validate("def foo(: pass", 1.0)
    assert not result.passed
    assert "SyntaxError" in result.reason


def test_no_improvement_fails() -> None:
    # The current naive target can't beat itself by the required margin.
    naive_source = TARGET_PATH.read_text(encoding="utf-8")
    result = gauntlet.validate(naive_source, _BASELINE)
    assert not result.passed
    assert "no improvement" in result.reason


def test_benchmark_gaming_is_rejected() -> None:
    # Passes mypy + the static pytest cases, but disagrees with the reference
    # on randomised inputs → must be caught by differential correctness.
    result = gauntlet.validate(_GAMING_CODE, _BASELINE)
    assert not result.passed
    assert "correctness mismatch" in result.reason


_NETWORK_CODE = '''
import socket


def sum_of_divisors(n: int) -> int:
    socket.create_connection(("example.com", 80))  # try to phone home
    return n


def benchmark(n: int = 10_000, repetitions: int = 5) -> float:
    return 1e-9
'''


def test_measure_baseline_runs_in_sandbox() -> None:
    # The committed target benchmarks to a positive latency, measured in the
    # gauntlet sandbox — never exec'd in this (main) process.
    b = gauntlet.measure_baseline()
    assert isinstance(b, float) and b > 0


def test_measure_baseline_unmeasurable_returns_zero() -> None:
    # Source lacking sum_of_divisors → advisory 0.0, not a crash.
    assert gauntlet.measure_baseline("x = 1\n") == 0.0


def test_sandbox_env_scrubs_credentials(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sis.gauntlet import _sandbox_env

    monkeypatch.setenv("SIS_ATLASSIAN_API_TOKEN", "secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    env = _sandbox_env(home="/tmp/x", pythonpath="/tmp/x")
    assert "SIS_ATLASSIAN_API_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["PATH"]  # still runnable


def test_docker_args_are_locked_down() -> None:
    from sis.gauntlet import _docker_args

    args = _docker_args("/tmp/sandbox123", {"PYTHONPATH": "/tmp/sandbox123",
                                            "PATH": "/usr/bin"}, "sis-gauntlet:latest")
    joined = " ".join(args)
    assert "--network none" in joined            # no egress
    assert "--cap-drop ALL" in joined            # no capabilities
    assert "no-new-privileges" in joined
    assert "--read-only" in joined               # immutable rootfs
    assert "-v /tmp/sandbox123:/tmp/sandbox123:rw" in joined  # only the temp dir
    assert args[-1] == "sis-gauntlet:latest"
    # PATH is left to the image; PYTHONPATH is forwarded for sitecustomize.
    assert "-e PYTHONPATH=/tmp/sandbox123" in joined
    assert "-e PATH=" not in joined


def test_docker_args_forward_no_host_credentials() -> None:
    from sis.gauntlet import _docker_args

    # Only the scrubbed env keys are forwarded — a stray token must not appear.
    env = {"PYTHONPATH": "/t", "HOME": "/t"}  # what _sandbox_env produces
    joined = " ".join(_docker_args("/t", env, "img"))
    assert "TOKEN" not in joined and "SECRET" not in joined and "KEY" not in joined


def test_network_egress_is_blocked() -> None:
    # A candidate that tries to open a connection is stopped by the sandbox.
    result = gauntlet.validate(_NETWORK_CODE, _BASELINE)
    assert not result.passed
    assert any("network egress blocked" in line for line in result.errors)


def test_mypy_failure_fails() -> None:
    bad_typed = "def sum_of_divisors(n): return n\ndef benchmark(n=1,repetitions=1): return 0.0\n"
    result = gauntlet.validate(bad_typed, 1.0)
    assert not result.passed
    assert "mypy" in result.reason
