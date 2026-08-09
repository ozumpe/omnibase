"""sis.gauntlet — validation gauntlet for generated code.

Runs cheapest-first gates: ast.parse → mypy --strict → interface → pytest →
backtest → differential correctness + benchmark. All execution happens in a
subprocess so an infinite loop or bad import cannot hang or corrupt the main
process.

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
import uuid
from dataclasses import dataclass, field

from sis.backtest import (
    EXIT_BAD_FIXTURE,
    EXIT_MISMATCH,
    EXIT_NO_COMPARATOR,
    EXIT_NO_ENTRY,
    build_script,
    parse_expectation,
    parse_fixture,
    plan_entry,
)
from sis.contract import (
    DEFAULT_MAX_LATENCY_RATIO,
    OptimizationContract,
    default_contract,
)
from sis.paths import COMPARATORS_PATH, PROJECT_ROOT

# Back-compat alias: the margin is now per-contract
# (``OptimizationContract.max_latency_ratio``), because what counts as a
# meaningful win is a property of the target, not of the engine.
IMPROVEMENT_MARGIN = DEFAULT_MAX_LATENCY_RATIO

# Only these env vars survive into the sandbox. Everything else — crucially
# every credential — is dropped, so generated code can't read or exfiltrate a
# token even before the network block kicks in.
#
# Public because ``sis.serving`` scrubs a green canary replica's environment
# against this same list (widened there for the vars a Ray worker needs to
# boot). One definition of "which env vars are safe to expose to generated
# code", so tightening it here tightens it everywhere.
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT", "TERM")

# Installed at interpreter startup (via PYTHONPATH→sitecustomize) in every
# subprocess that runs candidate code. Blocks outbound connections while
# leaving socket objects creatable, to minimise collateral on test plugins.
_NETWORK_GUARD = textwrap.dedent(
    """\
    import socket

    def _deny(*args, **kwargs):
        raise OSError("network egress blocked in gauntlet sandbox")

    # TCP connect paths.
    socket.socket.connect = _deny          # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = _deny       # type: ignore[method-assign,assignment]
    socket.create_connection = _deny       # type: ignore[assignment]
    # UDP has no connect, so sendto/sendmsg would otherwise slip past (L13).
    socket.socket.sendto = _deny           # type: ignore[method-assign,assignment]
    socket.socket.sendmsg = _deny          # type: ignore[method-assign,assignment]
    # DNS resolution is itself egress (a UDP query, and a data-exfil channel).
    socket.getaddrinfo = _deny             # type: ignore[assignment]
    """
)


def _sandbox_env(home: str, pythonpath: str) -> dict[str, str]:
    """A minimal, credential-free environment for a gauntlet subprocess."""
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
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


def _docker_args(tmpdir: str, env: dict[str, str], image: str, name: str) -> list[str]:
    """Build the ``docker run`` wrapper: no network, no caps, only tmpdir mounted.

    Kernel-enforced: ``--network none`` (no egress), ``--cap-drop ALL`` +
    ``--security-opt no-new-privileges``, ``--read-only`` rootfs, and only the
    temp dir bind-mounted. No host credentials or filesystem are visible.

    ``--name`` lets the timeout handler kill the container by name (SIGKILL to
    the ``docker run`` client does not stop the container). ``--memory`` /
    ``--cpus`` bound a runaway candidate's resource use (override via
    ``SIS_SANDBOX_MEMORY`` / ``SIS_SANDBOX_CPUS``).
    """
    args = [
        "docker", "run", "--rm",
        "--name", name,
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--pids-limit", "256",
        "--memory", os.getenv("SIS_SANDBOX_MEMORY", "1g"),
        "--cpus", os.getenv("SIS_SANDBOX_CPUS", "2"),
        "-v", f"{tmpdir}:{tmpdir}:rw",
        "-w", tmpdir,
    ]
    for key, value in env.items():
        if key != "PATH":  # let the image set its own PATH
            args += ["-e", f"{key}={value}"]
    args.append(image)
    return args


def _docker_kill(name: str) -> None:
    """Best-effort stop of a container after a timeout.

    ``subprocess.run(timeout=)`` SIGKILLs the ``docker run`` *client*, but the
    container keeps running detached — so an infinite-loop candidate would burn
    host CPU forever despite the gate reporting a timeout. Killing it by name
    stops it (``--rm`` then removes it). Swallows errors: if the container
    already exited or was never created, there is nothing to clean up.
    """
    try:
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        pass


def _timeout_seconds() -> float:
    """Wall-clock cap per gate — contains infinite loops in generated code."""
    return float(os.getenv("SIS_GAUNTLET_TIMEOUT", "120"))


def _timeout_result(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        cmd, 124, "", f"gauntlet sandbox timed out after {timeout:g}s"
    )


def _run(inner: list[str], tmpdir: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run one gate command (argv[0] == _PY) in the configured sandbox.

    A timeout is enforced so an infinite loop or pathologically slow candidate
    is killed rather than hanging the loop; it surfaces as a gate failure
    whose stderr says it timed out.
    """
    timeout = _timeout_seconds()
    mode = os.getenv("SIS_SANDBOX", "subprocess")
    if mode == "docker":
        image = os.getenv("SIS_SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE)
        name = f"sis-gauntlet-{uuid.uuid4().hex[:12]}"
        cmd = _docker_args(tmpdir, env, image, name) + ["python", *inner[1:]]
        try:
            # docker isolates env/cwd itself; don't leak the host's.
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            _docker_kill(name)  # SIGKILL hit the client, not the container
            return _timeout_result(cmd, timeout)
    cmd = [sys.executable, *inner[1:]]
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=tmpdir, env=env
        )
    except subprocess.TimeoutExpired:
        return _timeout_result(cmd, timeout)


@dataclass
class Result:
    passed: bool
    reason: str
    latency_seconds: float | None = None
    errors: list[str] = field(default_factory=list)


def ensure_sandbox_allows_proposer() -> None:
    """Refuse to run an untrusted proposer's code without kernel isolation.

    The stub proposer returns a trusted, hand-written candidate, so the soft
    ``subprocess`` sandbox is fine. A real LLM (``SIS_PROPOSER=claude``) writes
    **untrusted** code: it must run in the ``docker`` sandbox, whose only mount
    is the per-gate temp dir, so a malicious diff cannot read host credentials
    (``secrets.local.yml``, ``~/.aws``, ...). The subprocess sandbox scrubs the
    env and blocks egress, but the egress block is a monkeypatch untrusted code
    could undo, and the host filesystem stays readable — see KNOWN_ISSUES.md M1.

    Raises unless the sandbox is kernel-enforced. Explicit, loud override for
    when you accept the risk: ``SIS_ALLOW_UNSANDBOXED_LLM=1``.
    """
    proposer = os.getenv("SIS_PROPOSER", "stub")
    if proposer == "stub" or os.getenv("SIS_SANDBOX") == "docker":
        return
    if os.getenv("SIS_ALLOW_UNSANDBOXED_LLM") == "1":
        print(
            f"WARNING: SIS_PROPOSER={proposer!r} is running untrusted LLM code in the "
            "soft subprocess sandbox (SIS_ALLOW_UNSANDBOXED_LLM=1). It can read host "
            "files such as secrets.local.yml. Use SIS_SANDBOX=docker for any real run.",
            file=sys.stderr,
        )
        return
    raise RuntimeError(
        f"SIS_PROPOSER={proposer!r} writes untrusted code, which must run in the "
        "kernel-enforced docker sandbox so it cannot read host credentials. Set "
        "SIS_SANDBOX=docker (build it once: docker build -t sis-gauntlet:latest -f "
        "Dockerfile.gauntlet .), or set SIS_ALLOW_UNSANDBOXED_LLM=1 to accept the "
        "risk (not recommended)."
    )


def _backtest_gate(
    spec: OptimizationContract,
    *,
    tmp: pathlib.Path,
    candidate: pathlib.Path,
    oracle_mod: pathlib.Path,
    tmpdir: str,
    env: dict[str, str],
) -> Result | None:
    """Run the contract's backtests in the sandbox. Returns None when they pass.

    Fixtures are parsed **here**, in the main process, before anything is copied
    in. They are trusted data under ``specs/``, and validating them first means a
    malformed fixture is reported as the harness fault it is, naming the file —
    rather than surfacing as an opaque sandbox crash that reads like the
    candidate's fault. Same reason the missing-oracle and missing-tests checks
    fail loudly rather than falling through.
    """
    if not spec.backtests:
        return None

    comparators_src = pathlib.Path(COMPARATORS_PATH)
    if not comparators_src.exists():
        return Result(
            passed=False,
            reason=f"harness: shared comparators missing at {COMPARATORS_PATH} "
                   "— the backtest gate cannot run",
        )
    comparators_mod = tmp / "comparators.py"
    comparators_mod.write_text(comparators_src.read_text(encoding="utf-8"), encoding="utf-8")

    fixtures_dir = tmp / "fixtures"
    fixtures_dir.mkdir(exist_ok=True)
    plan: list[dict[str, object]] = []
    for index, bt in enumerate(spec.backtests):
        fixture_src = PROJECT_ROOT / bt.fixture
        expect_src = PROJECT_ROOT / bt.expect
        for path in (fixture_src, expect_src):
            if not path.exists():
                return Result(
                    passed=False,
                    reason=f"harness: backtest {bt.name!r} references a missing file "
                           f"({path}) — the backtest gate cannot run",
                )
        try:
            # Parsed for validation, then copied verbatim: the sandbox re-reads
            # the file, so re-serialising here would let the two views drift.
            parse_fixture(fixture_src.read_text(encoding="utf-8"), where=bt.fixture)
            parse_expectation(expect_src.read_text(encoding="utf-8"), where=bt.expect)
        except ValueError as exc:
            return Result(
                passed=False,
                reason=f"harness: backtest {bt.name!r} has a malformed fixture — {exc}",
            )
        # Indexed filenames, not bt.name: a name is human-authored and may
        # contain a path separator or a character the filesystem dislikes.
        fixture_dst = fixtures_dir / f"{index}_fixture.json"
        expect_dst = fixtures_dir / f"{index}_expect.json"
        fixture_dst.write_text(fixture_src.read_text(encoding="utf-8"), encoding="utf-8")
        expect_dst.write_text(expect_src.read_text(encoding="utf-8"), encoding="utf-8")
        plan.append(plan_entry(bt, fixture_path=fixture_dst, expect_path=expect_dst))

    script = build_script(
        candidate_path=str(candidate),
        comparators_path=str(comparators_mod),
        oracle_path=str(oracle_mod),
        entry=spec.entry,
        plan=plan,
    )
    result = _run([_PY, "-c", script], tmpdir, env)
    if timed_out := _timed_out(result, "backtest"):
        return timed_out
    if result.returncode == EXIT_NO_ENTRY:
        return Result(
            passed=False,
            reason=f"interface: candidate does not export {spec.entry!r} "
                   f"(required by contract {spec.name!r})",
            errors=result.stdout.splitlines(),
        )
    if result.returncode == EXIT_NO_COMPARATOR:
        return Result(
            passed=False,
            reason="harness: a backtest names a comparator that does not exist in the "
                   "contract's oracle or in specs/comparators.py",
            errors=result.stdout.splitlines(),
        )
    if result.returncode == EXIT_BAD_FIXTURE:
        return Result(
            passed=False,
            reason="harness: a backtest fixture could not be read in the sandbox",
            errors=result.stdout.splitlines(),
        )
    if result.returncode == EXIT_MISMATCH:
        detail = result.stdout.strip().removeprefix("MISMATCH").strip()
        return Result(
            passed=False,
            reason=f"backtest failed: candidate did not reproduce recorded history — {detail}",
            errors=result.stdout.splitlines(),
        )
    if result.returncode != 0:
        return Result(
            passed=False,
            reason="harness: the backtest script crashed",
            errors=result.stderr.splitlines(),
        )
    return None


def _timed_out(result: subprocess.CompletedProcess[str], gate: str) -> Result | None:
    """If *result* is a gate timeout (returncode 124 from _timeout_result), return
    a Result whose reason maps to the ``timeout`` episodic gate; else None.

    Without this, a timed-out gate returns the gate's *generic* failure reason
    ("mypy --strict failed", "pytest failed", …) and the timeout only survives in
    ``errors`` — so ``reject_gate`` is misattributed and the documented ``timeout``
    value never appears in the episodic dataset (KNOWN_ISSUES.md L12).
    """
    if result.returncode == 124:
        return Result(passed=False, reason=f"{gate} gate timed out",
                      errors=result.stderr.splitlines())
    return None


def validate(
    code_str: str,
    baseline_latency: float = 0.0,
    *,
    baseline_source: str | None = None,
    contract: OptimizationContract | None = None,
) -> Result:
    """Validate *code_str* against *contract* and return a Result.

    The candidate must:
    1. Parse as valid Python.
    2. Not be identical to the baseline (a no-op is not an improvement).
    3. Pass mypy --strict.
    4. Pass the contract's acceptance tests.
    4b. Reproduce every recorded episode the contract declares, within the
        comparator and tolerance that episode names (skipped when the contract
        declares none, which is the case for both shipped targets).
    5a. Export the contract's ``entry`` function (interface).
    5b. Agree with the contract's independent reference on randomised inputs
        (anti-gaming).
    5c. Beat the freshly-measured baseline by the contract's
        ``max_latency_ratio``.

    *contract* says what "correct" and "better" mean for this target — the
    reference, the inputs, the margin (see :mod:`sis.contract`). Omitting it
    falls back to the bootstrap ``sum_of_divisors`` contract, so existing
    callers and tests keep working unchanged.

    *baseline_source* is the code the candidate must beat — the code the cycle
    is actually based on (the merged target), which the loop passes in. It is
    written into the sandbox and benchmarked against; the candidate never
    competes with a stale copy on disk (see docs/KNOWN_ISSUES.md H1). When
    omitted, it falls back to the contract's target file on disk so direct
    callers and tests still work.

    *baseline_latency* is advisory only (kept for logging/compat); the pass/fail
    comparison uses a baseline measured in-sandbox over the same workload as the
    candidate, which is far less noisy.
    """
    spec = contract if contract is not None else default_contract()
    # Backstop: never execute untrusted proposer code in a soft sandbox (M1).
    ensure_sandbox_allows_proposer()

    if os.getenv("SIS_SANDBOX") == "docker" and shutil.which("docker") is None:
        raise RuntimeError(
            "SIS_SANDBOX=docker but the docker CLI was not found. Install Docker "
            "and build the image (docker build -t sis-gauntlet:latest -f "
            "Dockerfile.gauntlet .), or unset SIS_SANDBOX for the subprocess sandbox."
        )

    # The code the candidate must beat: what the caller says the cycle is based
    # on (the merged target), NOT whatever happens to be on disk. Fall back to
    # the local target only when no source is supplied. See KNOWN_ISSUES.md H1.
    baseline_code = (
        baseline_source if baseline_source is not None
        else pathlib.Path(spec.target_file).read_text(encoding="utf-8")
    )

    # --- Gate 1: syntax ---
    try:
        ast.parse(code_str)
    except SyntaxError as exc:
        return Result(passed=False, reason=f"SyntaxError: {exc}")

    # --- Gate 1b: no-op — a candidate identical to the baseline can never be an
    # improvement, so reject it before the benchmark. Without this, an identical
    # re-proposal (e.g. the stub after its own optimisation has merged) would
    # race the ≥10% margin on µs-scale timing noise. See KNOWN_ISSUES.md M3.
    if code_str.strip() == baseline_code.strip():
        return Result(passed=False, reason="no change: candidate is identical to the baseline")

    # Everything lives under the temp dir so the sandbox is self-contained
    # (in docker mode only this dir is mounted — nothing reaches the host).
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = pathlib.Path(tmpdir)
        candidate = tmp / "target.py"
        candidate.write_text(code_str, encoding="utf-8")

        # The baseline the candidate must beat, copied into the sandbox — loaded
        # from the mount, never from an external host path.
        baseline_mod = tmp / "baseline.py"
        baseline_mod.write_text(baseline_code, encoding="utf-8")

        # The contract's oracle: reference implementation, benchmark inputs and
        # random-input generator. It is *code* and has to run beside the
        # candidate, so it is copied in as a module rather than interpolated
        # into the benchmark script as literals (which is what tied the whole
        # gauntlet to one target — L5).
        oracle_src = pathlib.Path(spec.oracle_file)
        if not oracle_src.exists():
            return Result(
                passed=False,
                reason=f"harness: contract oracle missing at {spec.oracle_path} "
                       "— correctness and benchmark gates cannot run",
            )
        oracle_mod = tmp / "oracle.py"
        oracle_mod.write_text(oracle_src.read_text(encoding="utf-8"), encoding="utf-8")

        # --- Gate 5 (sandbox): network-egress block + scrubbed env ---
        # sitecustomize.py runs at interpreter startup in every gate that has
        # tmp on PYTHONPATH, installing the network guard before any candidate
        # code runs. In docker mode --network none enforces this in the kernel
        # too; the guard stays as defence in depth.
        (tmp / "sitecustomize.py").write_text(_NETWORK_GUARD, encoding="utf-8")
        env = _sandbox_env(home=tmpdir, pythonpath=tmpdir)

        # --- Gate 2: mypy --strict ---
        mypy_result = _run([_PY, "-m", "mypy", "--strict", str(candidate)], tmpdir, env)
        if timed_out := _timed_out(mypy_result, "mypy"):
            return timed_out
        if mypy_result.returncode != 0:
            return Result(
                passed=False,
                reason="mypy --strict failed",
                errors=mypy_result.stdout.splitlines() + mypy_result.stderr.splitlines(),
            )

        # --- Gate 2b: interface — the candidate must export the contract's
        # entry point. Cheap (one import), and it fails with a statement about
        # the *shape* of the diff; without it a wrong-API candidate surfaces as
        # a wall of acceptance-test failures that never names the actual
        # problem. Ordered before acceptance per docs/CLASS2_CONTRACT.md's gate
        # stack — "fails fast if the LLM built the wrong shape".
        iface_script = textwrap.dedent(
            f"""\
            import sys, importlib.util
            s = importlib.util.spec_from_file_location("candidate", {str(candidate)!r})
            m = importlib.util.module_from_spec(s)
            s.loader.exec_module(m)
            if not callable(getattr(m, {spec.entry!r}, None)):
                sys.exit(4)
            """
        )
        iface_result = _run([_PY, "-c", iface_script], tmpdir, env)
        if timed_out := _timed_out(iface_result, "interface"):
            return timed_out
        if iface_result.returncode == 4:
            return Result(
                passed=False,
                reason=f"interface: candidate does not export a callable "
                       f"{spec.entry!r} (required by contract {spec.name!r})",
            )
        if iface_result.returncode != 0:
            return Result(
                passed=False,
                reason="interface: candidate could not be imported",
                errors=iface_result.stderr.splitlines(),
            )

        # --- Gate 3: pytest — the contract's acceptance tests, against the
        # candidate. Only they go into the sandbox: they do `import target`,
        # which resolves to the candidate written above. The harness's own tests
        # import sis modules not present here and are not the subject of
        # validation.
        #
        # The suite is required, not optional. When it was missing this used to
        # fall through to `pytest <nonexistent dir>`, which exits non-zero and
        # was reported as "pytest failed" — blaming the candidate for a broken
        # harness. Still fails closed (an unrun correctness gate must never read
        # as a pass), but now says which side is at fault.
        tests_src = pathlib.Path(spec.tests_file)
        if not tests_src.exists():
            return Result(
                passed=False,
                reason=f"harness: contract acceptance tests missing at {spec.tests_path} "
                       "— the pytest gate cannot run",
            )
        tests_dst = tmp / "tests"
        tests_dst.mkdir()
        (tests_dst / "__init__.py").write_text("", encoding="utf-8")
        (tests_dst / "test_target.py").write_text(
            tests_src.read_text(encoding="utf-8"), encoding="utf-8"
        )

        pytest_result = _run(
            [_PY, "-m", "pytest", str(tmp / "tests"), "-q", "--tb=short"], tmpdir, env
        )
        if timed_out := _timed_out(pytest_result, "pytest"):
            return timed_out
        if pytest_result.returncode != 0:
            return Result(
                passed=False,
                reason="pytest failed",
                errors=pytest_result.stdout.splitlines() + pytest_result.stderr.splitlines(),
            )

        # --- Gate 3b: backtest — does the candidate reproduce recorded reality?
        # Placed after the acceptance tests and before the differential gate on
        # the cheapest-first rule: replaying a handful of fixtures costs far less
        # than 300 randomised trials plus five benchmark repetitions, and it
        # fails with a far sharper message ("did not reproduce 2026Q1, off by
        # 12%") than a differential mismatch on an opaque random input.
        #
        # It is not a *substitute* for the differential gate and does not reorder
        # the anti-gaming argument. Fixtures are few and fixed, so a candidate
        # could in principle special-case them; unpredictable inputs are what
        # make that not worth attempting. Backtest asks "does it match history",
        # differential asks "is it right in general", and the second is the moat.
        #
        # docs/CLASS2_CONTRACT.md lists backtest *after* the invariant gate. That
        # ordering can be revisited when OMNI-17/18 split this pipeline into
        # discrete Gate objects; today differential-correctness and the benchmark
        # share one script, so there is no seam between them to sit in.
        if backtest_failure := _backtest_gate(
            spec, tmp=tmp, candidate=candidate, oracle_mod=oracle_mod,
            tmpdir=tmpdir, env=env,
        ):
            return backtest_failure

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
            oracle = _load({str(oracle_mod)!r}, "oracle")

            entry = {spec.entry!r}
            if not hasattr(cand, entry):
                print("NOENTRY", entry)
                sys.exit(4)
            cand_fn = getattr(cand, entry)
            base_fn = getattr(base, entry)

            rng = random.Random()  # system entropy: inputs are unpredictable
            for _ in range({spec.diff_trials}):
                args = oracle.random_input(rng)
                if cand_fn(*args) != oracle.reference(*args):
                    print("MISMATCH", args)
                    sys.exit(3)

            INPUTS = oracle.BENCH_INPUTS

            def bench(fn):
                best = float("inf")
                for _ in range(5):
                    start = time.perf_counter()
                    for args in INPUTS:
                        fn(*args)
                    best = min(best, time.perf_counter() - start)
                return best / len(INPUTS)

            print(bench(cand_fn), bench(base_fn))
            """
        )
        bench_result = _run([_PY, "-c", bench_script], tmpdir, env)
        if timed_out := _timed_out(bench_result, "benchmark"):
            return timed_out
        if bench_result.returncode == 4:
            return Result(
                passed=False,
                reason=f"interface: candidate does not export {spec.entry!r} "
                       f"(required by contract {spec.name!r})",
                errors=bench_result.stdout.splitlines(),
            )
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

        # Must beat the freshly-measured baseline by the contract's margin.
        if candidate_latency > measured_baseline * spec.max_latency_ratio:
            return Result(
                passed=False,
                reason=(
                    f"no improvement: candidate {candidate_latency:.6f}s vs baseline "
                    f"{measured_baseline:.6f}s (need ≤ {spec.max_latency_ratio:.0%})"
                ),
                latency_seconds=candidate_latency,
            )

        return Result(passed=True, reason="all gates passed", latency_seconds=candidate_latency)


def measure_baseline(
    source: str | None = None, *, contract: OptimizationContract | None = None
) -> float:
    """Benchmark the contract's entry function **inside the sandbox**.

    Used by the loop for the baseline it reports and shows the proposer. Like
    every gate, the code runs in the sandbox (scrubbed env + egress block, or
    docker) — never in the main process. ``validate()`` still measures its own
    baseline for the authoritative pass/fail decision; this is the advisory
    number for the prompt and the episodic log. Returns 0.0 if unmeasurable.
    """
    spec = contract if contract is not None else default_contract()
    code = (
        source if source is not None
        else pathlib.Path(spec.target_file).read_text(encoding="utf-8")
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = pathlib.Path(tmpdir)
        (tmp / "sitecustomize.py").write_text(_NETWORK_GUARD, encoding="utf-8")
        module = tmp / "baseline.py"
        module.write_text(code, encoding="utf-8")
        oracle_src = pathlib.Path(spec.oracle_file)
        if not oracle_src.exists():
            print(
                f"WARNING: measure_baseline() found no contract oracle at "
                f"{spec.oracle_path}; returning 0.0.",
                file=sys.stderr,
            )
            return 0.0
        oracle_mod = tmp / "oracle.py"
        oracle_mod.write_text(oracle_src.read_text(encoding="utf-8"), encoding="utf-8")
        env = _sandbox_env(home=tmpdir, pythonpath=tmpdir)
        script = textwrap.dedent(
            f"""\
            import time, importlib.util

            def _load(path, name):
                s = importlib.util.spec_from_file_location(name, path)
                mod = importlib.util.module_from_spec(s)
                s.loader.exec_module(mod)
                return mod

            m = _load({str(module)!r}, "m")
            oracle = _load({str(oracle_mod)!r}, "oracle")

            fn = getattr(m, {spec.entry!r})
            best = float("inf")
            for _ in range(5):
                start = time.perf_counter()
                for args in oracle.BENCH_INPUTS:
                    fn(*args)
                best = min(best, time.perf_counter() - start)
            print(best / len(oracle.BENCH_INPUTS))
            """
        )
        result = _run([_PY, "-c", script], tmpdir, env)
        try:
            return float(result.stdout.strip())
        except ValueError:
            # Advisory only (validate() measures its own baseline), so a failure
            # falls back to 0.0 — but say so loudly instead of silently feeding a
            # bogus number into prompts and the episodic log (L7).
            print(
                "WARNING: measure_baseline() could not parse a latency from the "
                f"sandbox (returncode={result.returncode}); returning 0.0. "
                f"stderr: {result.stderr.strip()[:200]}",
                file=sys.stderr,
            )
            return 0.0
