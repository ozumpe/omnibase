"""sis.gauntlet — validation gauntlet for generated code.

Runs cheapest-first gates: ast.parse → mypy --strict → pytest → benchmark.
All execution happens in a subprocess so an infinite loop or bad import
cannot hang or corrupt the main process.

Gate 5 (sandbox) has two modes, chosen by ``SIS_SANDBOX``:

- ``subprocess`` (default): each gate runs in a host subprocess with a
  **credential-scrubbed environment** (allowlist only — no AWS/Atlassian/
  GitHub tokens, no ``SIS_*``) and a **network-egress block** injected via
  ``sitecustomize.py``. A soft, in-process guard — good for local dev.
- ``docker``: each gate runs inside ``docker run --network none --cap-drop
  ALL --read-only`` with **only the temp dir mounted** (no host filesystem,
  no credentials). Egress is **kernel-enforced**, not monkeypatched — the
  right mode for AWS/CI once an LLM is writing the code. Needs an image with
  python+mypy+pytest (``SIS_SANDBOX_IMAGE``; build from ``Dockerfile.gauntlet``).

Either way the candidate, the baseline, the tests, and the benchmark all live
under the temp dir, so the sandbox is fully self-contained.

Returns a Result dataclass with pass/fail and the benchmark latency when
the candidate passes all gates.
"""

import ast
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field

from sis.paths import TARGET_PATH, TARGET_TEST_PATH

# A candidate must run in at most this fraction of the baseline's time
# (i.e. be at least 10% faster) — "beat the baseline by a defined margin".
IMPROVEMENT_MARGIN = 0.90

# Only these env vars survive into the sandbox. Everything else — crucially
# every credential — is dropped, so generated code can't read or exfiltrate a
# token even before the network block kicks in.
_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT", "TERM")

# Installed at interpreter startup (via PYTHONPATH→sitecustomize) in every
# subprocess that runs candidate code. Blocks outbound connections while
# leaving socket objects creatable, to minimise collateral on test plugins.
_NETWORK_GUARD = textwrap.dedent(
    """\
    import socket

    def _deny(*args, **kwargs):
        raise OSError("network egress blocked in gauntlet sandbox")

    socket.socket.connect = _deny          # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = _deny       # type: ignore[method-assign,assignment]
    socket.create_connection = _deny       # type: ignore[assignment]
    """
)


def _sandbox_env(home: str, pythonpath: str) -> dict[str, str]:
    """A minimal, credential-free environment for a gauntlet subprocess."""
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env.setdefault("PATH", os.defpath)
    env["HOME"] = home
    env["TMPDIR"] = home
    env["PYTHONPATH"] = pythonpath  # so the injected sitecustomize.py loads
    env["PYTHONHASHSEED"] = "0"
    return env


# "subprocess" (default, soft guard) | "docker" (kernel-enforced isolation)
DEFAULT_SANDBOX_IMAGE = "sis-gauntlet:latest"

# The python executable inside a gate command. _run() substitutes the real
# path (host) or "python" (container) for this sentinel as command argv[0].
_PY = "PYTHON"


def _docker_args(tmpdir: str, env: dict[str, str], image: str) -> list[str]:
    """Build the ``docker run`` wrapper: no network, no caps, only tmpdir mounted.

    Kernel-enforced: ``--network none`` (no egress), ``--cap-drop ALL`` +
    ``--security-opt no-new-privileges``, ``--read-only`` rootfs, and only the
    temp dir bind-mounted. No host credentials or filesystem are visible.
    """
    args = [
        "docker", "run", "--rm",
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--pids-limit", "256",
        "-v", f"{tmpdir}:{tmpdir}:rw",
        "-w", tmpdir,
    ]
    for key, value in env.items():
        if key != "PATH":  # let the image set its own PATH
            args += ["-e", f"{key}={value}"]
    args.append(image)
    return args


def _timeout_seconds() -> float:
    """Wall-clock cap per gate — contains infinite loops in generated code."""
    return float(os.getenv("SIS_GAUNTLET_TIMEOUT", "120"))


def _run(inner: list[str], tmpdir: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run one gate command (argv[0] == _PY) in the configured sandbox.

    A timeout is enforced so an infinite loop or pathologically slow candidate
    is killed rather than hanging the loop; it surfaces as a gate failure
    whose stderr says it timed out.
    """
    timeout = _timeout_seconds()
    mode = os.getenv("SIS_SANDBOX", "subprocess")
    try:
        if mode == "docker":
            image = os.getenv("SIS_SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE)
            cmd = _docker_args(tmpdir, env, image) + ["python", *inner[1:]]
            # docker isolates env/cwd itself; don't leak the host's.
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        cmd = [sys.executable, *inner[1:]]
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=tmpdir, env=env
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, 124, "", f"gauntlet sandbox timed out after {timeout:g}s"
        )


@dataclass
class Result:
    passed: bool
    reason: str
    latency_seconds: float | None = None
    errors: list[str] = field(default_factory=list)


def validate(code_str: str, baseline_latency: float) -> Result:
    """Validate *code_str* and return a Result.

    The candidate must:
    1. Parse as valid Python.
    2. Pass mypy --strict.
    3. Pass the target test suite.
    4a. Agree with an independent reference on randomised inputs (anti-gaming).
    4b. Beat the freshly-measured baseline by ``IMPROVEMENT_MARGIN``.

    *baseline_latency* is advisory only (kept for logging/compat); the
    pass/fail comparison uses a baseline measured in-sandbox over the same
    workload as the candidate, which is far less noisy.
    """
    if os.getenv("SIS_SANDBOX") == "docker" and shutil.which("docker") is None:
        raise RuntimeError(
            "SIS_SANDBOX=docker but the docker CLI was not found. Install Docker "
            "and build the image (docker build -t sis-gauntlet:latest -f "
            "Dockerfile.gauntlet .), or unset SIS_SANDBOX for the subprocess sandbox."
        )

    # --- Gate 1: syntax ---
    try:
        ast.parse(code_str)
    except SyntaxError as exc:
        return Result(passed=False, reason=f"SyntaxError: {exc}")

    # Everything lives under the temp dir so the sandbox is self-contained
    # (in docker mode only this dir is mounted — nothing reaches the host).
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = pathlib.Path(tmpdir)
        candidate = tmp / "target.py"
        candidate.write_text(code_str, encoding="utf-8")

        # The current live target, copied in as the baseline to benchmark
        # against — loaded from the mount, never from an external host path.
        baseline_mod = tmp / "baseline.py"
        baseline_mod.write_text(TARGET_PATH.read_text(encoding="utf-8"), encoding="utf-8")

        # --- Gate 5 (sandbox): network-egress block + scrubbed env ---
        # sitecustomize.py runs at interpreter startup in every gate that has
        # tmp on PYTHONPATH, installing the network guard before any candidate
        # code runs. In docker mode --network none enforces this in the kernel
        # too; the guard stays as defence in depth.
        (tmp / "sitecustomize.py").write_text(_NETWORK_GUARD, encoding="utf-8")
        env = _sandbox_env(home=tmpdir, pythonpath=tmpdir)

        # --- Gate 2: mypy --strict ---
        mypy_result = _run([_PY, "-m", "mypy", "--strict", str(candidate)], tmpdir, env)
        if mypy_result.returncode != 0:
            return Result(
                passed=False,
                reason="mypy --strict failed",
                errors=mypy_result.stdout.splitlines() + mypy_result.stderr.splitlines(),
            )

        # --- Gate 3: pytest — run test_target.py against the candidate.
        # Only the target test goes into the sandbox: it does `import target`,
        # which resolves to the candidate written above. The harness's own
        # tests import sis modules not present here and are not the subject of
        # validation.
        if TARGET_TEST_PATH.exists():
            tests_dst = tmp / "tests"
            tests_dst.mkdir()
            (tests_dst / "__init__.py").write_text("", encoding="utf-8")
            (tests_dst / "test_target.py").write_text(
                TARGET_TEST_PATH.read_text(encoding="utf-8"), encoding="utf-8"
            )

        pytest_result = _run(
            [_PY, "-m", "pytest", str(tmp / "tests"), "-q", "--tb=short"], tmpdir, env
        )
        if pytest_result.returncode != 0:
            return Result(
                passed=False,
                reason="pytest failed",
                errors=pytest_result.stdout.splitlines() + pytest_result.stderr.splitlines(),
            )

        # --- Gate 4: differential correctness + benchmark ---
        # Two anti-gaming defences beyond the static pytest cases:
        #   (a) Differential correctness against an INDEPENDENT reference over
        #       RANDOMISED inputs the candidate cannot predict — catches a diff
        #       that special-cases the known test/benchmark inputs but is wrong
        #       elsewhere ("benchmark gaming").
        #   (b) The harness drives sum_of_divisors directly (never the
        #       candidate's own benchmark()) and times the candidate AND the
        #       current target over the SAME fixed workload, so the comparison
        #       is fair and the candidate can't fake its own timing.
        bench_script = textwrap.dedent(
            f"""\
            import sys, time, random, importlib.util

            def _load(path, name):
                spec = importlib.util.spec_from_file_location(name, path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod

            cand = _load({str(candidate)!r}, "candidate")
            base = _load({str(baseline_mod)!r}, "baseline")

            def reference(n):  # trusted, independent O(n) implementation
                return sum(i for i in range(1, n + 1) if n % i == 0)

            rng = random.Random()  # system entropy: inputs are unpredictable
            for _ in range(300):
                n = rng.randint(2, 20_000)
                if cand.sum_of_divisors(n) != reference(n):
                    print("MISMATCH", n)
                    sys.exit(3)

            INPUTS = [3, 97, 997, 5000, 8128, 9973, 10_000, 12345, 16384, 19_999]

            def bench(fn):
                best = float("inf")
                for _ in range(5):
                    start = time.perf_counter()
                    for n in INPUTS:
                        fn(n)
                    best = min(best, time.perf_counter() - start)
                return best / len(INPUTS)

            print(bench(cand.sum_of_divisors), bench(base.sum_of_divisors))
            """
        )
        bench_result = _run([_PY, "-c", bench_script], tmpdir, env)
        if bench_result.returncode == 3:
            return Result(
                passed=False,
                reason="correctness mismatch (candidate disagrees with reference — "
                       "possible benchmark gaming)",
                errors=bench_result.stdout.splitlines(),
            )
        if bench_result.returncode != 0:
            return Result(
                passed=False,
                reason="benchmark script crashed",
                errors=bench_result.stderr.splitlines(),
            )

        try:
            candidate_latency, measured_baseline = (
                float(x) for x in bench_result.stdout.split()
            )
        except ValueError:
            return Result(passed=False, reason="benchmark produced non-numeric output")

        # Must beat the freshly-measured baseline by a defined margin.
        if candidate_latency > measured_baseline * IMPROVEMENT_MARGIN:
            return Result(
                passed=False,
                reason=(
                    f"no improvement: candidate {candidate_latency:.6f}s vs baseline "
                    f"{measured_baseline:.6f}s (need ≤ {IMPROVEMENT_MARGIN:.0%})"
                ),
                latency_seconds=candidate_latency,
            )

        return Result(passed=True, reason="all gates passed", latency_seconds=candidate_latency)


def measure_baseline(source: str | None = None) -> float:
    """Benchmark the current target's ``sum_of_divisors`` **inside the sandbox**.

    Used by the loop for the baseline it reports and shows the proposer. Like
    every gate, the code runs in the sandbox (scrubbed env + egress block, or
    docker) — never in the main process. ``validate()`` still measures its own
    baseline for the authoritative pass/fail decision; this is the advisory
    number for the prompt and the episodic log. Returns 0.0 if unmeasurable.
    """
    code = source if source is not None else TARGET_PATH.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = pathlib.Path(tmpdir)
        (tmp / "sitecustomize.py").write_text(_NETWORK_GUARD, encoding="utf-8")
        module = tmp / "baseline.py"
        module.write_text(code, encoding="utf-8")
        env = _sandbox_env(home=tmpdir, pythonpath=tmpdir)
        script = textwrap.dedent(
            f"""\
            import time, importlib.util
            spec = importlib.util.spec_from_file_location("m", {str(module)!r})
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)

            INPUTS = [3, 97, 997, 5000, 8128, 9973, 10_000, 12345, 16384, 19_999]
            best = float("inf")
            for _ in range(5):
                start = time.perf_counter()
                for n in INPUTS:
                    m.sum_of_divisors(n)
                best = min(best, time.perf_counter() - start)
            print(best / len(INPUTS))
            """
        )
        result = _run([_PY, "-c", script], tmpdir, env)
        try:
            return float(result.stdout.strip())
        except ValueError:
            return 0.0
