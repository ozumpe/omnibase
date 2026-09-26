"""sis.gauntlet — validation gauntlet for generated code.

**The contract selects which gates run.** ``validate()`` iterates whatever
``Contract.gate_profile()`` asks for, cheapest first, stopping at the first
failure — so a Class-1 optimisation and a Class-2 feature flow through one
entry point with different profiles (see :mod:`sis.contract`):

- Class 1: ast → no-op → mypy → interface → acceptance → invariant →
  backtest → differential correctness + benchmark.
- Class 2: ast → mypy → interface → acceptance → invariant → backtest. No
  no-op (nothing to be identical to) and no differential/benchmark (no
  reference exists, and "faster" is not what makes a feature correct).

Gate *implementations* all live here, deliberately: this file is
POLICY-FORBIDDEN, and guardrail code concentrated in one place is easier to keep
guarded than guardrail code spread across modules that each have to be
remembered in a list. The contract chooses; the gauntlet implements.

All execution happens in a subprocess so an infinite loop or bad import cannot
hang or corrupt the main process.

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
import json
import math
import os
import pathlib
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import textwrap
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum

from sis import config
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
    DEFAULT_BENCH_BATCH,
    DEFAULT_BENCH_CONFIDENCE,
    DEFAULT_BENCH_MIN_SAMPLES,
    DEFAULT_BENCH_SAMPLES,
    DEFAULT_DIFF_TRIALS,
    DEFAULT_MAX_LATENCY_RATIO,
    Contract,
    Determinism,
    GateName,
    OptimizationContract,
    default_contract,
)
from sis.invariant import (
    DEFAULT_SEED,
    EXIT_STRATEGY_ERROR,
    EXIT_UNRESOLVED,
    EXIT_VIOLATED,
)
from sis.invariant import EXIT_NO_ENTRY as EXIT_NO_ENTRY_INV
from sis.invariant import build_script as invariant_script
from sis.invariant import plan_entry as invariant_plan_entry
from sis.paths import COMPARATORS_PATH, INVARIANTS_PATH, PROJECT_ROOT
from sis.slo import EXIT_BAD_WORKLOAD, EXIT_NO_WORKLOAD, EXIT_RAISED, evaluate_slo
from sis.slo import EXIT_NO_ENTRY as EXIT_NO_ENTRY_SLO
from sis.slo import build_script as slo_script

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


def sandbox_mode() -> str:
    """Which sandbox contains generated code: ``subprocess`` or ``docker``.

    One accessor rather than four ``os.getenv("SIS_SANDBOX")`` calls that each
    had to spell the default identically. The schema restricts the value to the
    two modes, which closes a genuinely nasty failure: ``SIS_SANDBOX=dcoker``
    used to compare unequal to ``"docker"`` and silently run untrusted code in
    the *soft* sandbox, reporting nothing.
    """
    mode: str = config.get("sandbox.mode")
    return mode

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
    ``SIS_SANDBOX_MEMORY`` / ``SIS_SANDBOX_CPUS``). ``--user`` is the host
    user's uid, see :func:`_container_user`.
    """
    args = [
        "docker", "run", "--rm",
        "--name", name,
        "--user", _container_user(),
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--pids-limit", "256",
        "--memory", str(config.get("sandbox.memory")),
        "--cpus", str(config.get("sandbox.cpus")),
        "-v", f"{tmpdir}:{tmpdir}:rw",
        "-w", tmpdir,
    ]
    for key, value in env.items():
        if key != "PATH":  # let the image set its own PATH
            args += ["-e", f"{key}={value}"]
    args.append(image)
    return args


def _container_user() -> str:
    """The ``uid:gid`` the sandbox container runs as: the host user's own.

    The per-validation temp dir comes from ``tempfile``, so it is mode 0700 and
    owned by whoever runs the gauntlet. On native Linux — the AWS run box — a
    container running as any *other* uid cannot even open the candidate file,
    and every docker gate fails with ``Permission denied``. Docker Desktop hid
    this: its file-sharing layer ignores ownership, so the image's fixed
    ``sandbox`` user (uid 10001) passed every local test and the first
    real-life run, and the defect would only have surfaced on EC2. Running as
    the host uid keeps the process unprivileged and able to touch exactly the
    one directory mounted for it.

    Refuses root: there the host uid would make candidate code root inside the
    container. ``--cap-drop ALL`` and ``no-new-privileges`` would still apply,
    but a sandbox that silently loosens itself depending on who launched it is
    the wrong shape — run the loop as an ordinary user instead.
    """
    if os.getuid() == 0:
        raise RuntimeError(
            "sandbox.mode=docker will not run as root: the sandbox container runs "
            "candidate code as the invoking user's uid (the owner of its temp dir), "
            "and as root that would make generated code root inside the container. "
            "Run the loop as an unprivileged user — on the AWS box, `sudo -iu ubuntu`."
        )
    return f"{os.getuid()}:{os.getgid()}"


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
    seconds: float = config.get("sandbox.timeout_seconds")
    return seconds


def _timeout_result(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        cmd, 124, "", f"gauntlet sandbox timed out after {timeout:g}s"
    )


def _run(
    inner: list[str], tmpdir: str, env: dict[str, str], *, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one gate command (argv[0] == _PY) in the configured sandbox.

    A timeout is enforced so an infinite loop or pathologically slow candidate
    is killed rather than hanging the loop; it surfaces as a gate failure
    whose stderr says it timed out.

    *timeout* overrides the per-gate default for callers that must not block
    for the full ``SIS_GAUNTLET_TIMEOUT``. The contract-author's discrimination
    check is one: it runs inside a single-threaded Ray actor, where a drafted
    test containing an infinite loop would otherwise queue every other draft
    behind it for two minutes.
    """
    timeout = _timeout_seconds() if timeout is None else timeout
    if sandbox_mode() == "docker":
        image = str(config.get("sandbox.image"))
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
    # The invariant gate's generation seed, on a violation. Also carried in the
    # reason string so it survives into the episodic log's reject_reason without
    # every caller having to plumb a new field.
    seed: int | None = None


@dataclass
class _GateContext:
    """Everything a gate may need, assembled once by :func:`validate`.

    One context rather than a per-gate argument list, because gates are selected
    by the contract and run through a uniform table — a gate that needed its own
    signature could not be dispatched from one.

    ``baseline`` and ``oracle`` are ``None`` for a contract whose profile does
    not ask for them: a Class-2 feature has no prior version to be a no-op
    against and no reference to differ from. A gate that needs one says so
    itself rather than trusting that ``validate`` prepared it.
    """

    contract: Contract
    code_str: str
    tmp: pathlib.Path
    tmpdir: str
    env: dict[str, str]
    candidate: pathlib.Path
    baseline_code: str | None = None
    baseline: pathlib.Path | None = None
    oracle: pathlib.Path | None = None
    # Chosen once per validation and handed to the invariant gate, so a
    # violation is reproducible from the log alone.
    seed: int = DEFAULT_SEED
    # Filled in by the differential+benchmark gate; reported on success.
    candidate_latency: float | None = None


def ensure_sandbox_allows_proposer() -> None:
    """Refuse to run an untrusted proposer's code without kernel isolation.

    The stub proposer returns a trusted, hand-written candidate, so the soft
    ``subprocess`` sandbox is fine. A real LLM (``proposer.backend=claude``)
    writes **untrusted** code: it must run in the ``docker`` sandbox, whose only
    mount is the per-gate temp dir, so a malicious diff cannot read host
    credentials (``secrets.local.yml``, ``~/.aws``, ...). The subprocess sandbox
    scrubs the env and blocks egress, but the egress block is a monkeypatch
    untrusted code could undo, and the host filesystem stays readable — see
    KNOWN_ISSUES.md M1.

    Raises unless the sandbox is kernel-enforced. Explicit, loud override for
    when you accept the risk: ``sandbox.allow_unsandboxed_llm``.
    """
    proposer = str(config.get("proposer.backend"))
    if proposer == "stub" or sandbox_mode() == "docker":
        return
    if config.get("sandbox.allow_unsandboxed_llm"):
        print(
            f"WARNING: proposer.backend={proposer!r} is running untrusted LLM code in "
            "the soft subprocess sandbox (sandbox.allow_unsandboxed_llm). It can read "
            "host files such as secrets.local.yml. Use sandbox.mode=docker "
            "(--sandbox-mode docker) for any real run.",
            file=sys.stderr,
        )
        return
    raise RuntimeError(
        f"proposer.backend={proposer!r} writes untrusted code, which must run in the "
        "kernel-enforced docker sandbox so it cannot read host credentials. Set "
        "sandbox.mode=docker (--sandbox-mode docker, or SIS_SANDBOX=docker; build the "
        "image once: docker build -t sis-gauntlet:latest -f Dockerfile.gauntlet .), or "
        "set sandbox.allow_unsandboxed_llm=true to accept the risk (not recommended)."
    )


def _gate_invariant(ctx: _GateContext) -> Result | None:
    """Assert the contract's domain laws over generated inputs.

    The Class-2 replacement for differential correctness, and the same
    anti-gaming role: a candidate can special-case the handful of acceptance
    examples, but it cannot special-case an input distribution it never sees.
    Ordered after acceptance (which is cheaper and names the problem more
    precisely) and before backtest.

    The seed is chosen *per run* and reported on failure. Hypothesis's example
    database is disabled in the sandbox, so the seed is the only reproduction
    handle — which is what makes a rejection recorded in the episodic log worth
    anything later.
    """
    spec = ctx.contract
    if not spec.invariants:
        return None

    shared_src = pathlib.Path(INVARIANTS_PATH)
    if not shared_src.exists():
        return Result(
            passed=False,
            reason=f"harness: shared invariants missing at {INVARIANTS_PATH} "
                   "— the invariant gate cannot run",
        )
    shared_mod = ctx.tmp / "invariants.py"
    shared_mod.write_text(shared_src.read_text(encoding="utf-8"), encoding="utf-8")

    seed = ctx.seed
    script = invariant_script(
        candidate_path=str(ctx.candidate),
        shared_path=str(shared_mod),
        oracle_path=str(ctx.oracle) if ctx.oracle is not None else None,
        entry=spec.entry,
        plan=[invariant_plan_entry(inv) for inv in spec.invariants],
        examples=spec.invariant_examples,
        seed=seed,
    )
    result = _run([_PY, "-c", script], ctx.tmpdir, ctx.env)
    if timed_out := _timed_out(result, "invariant"):
        return timed_out

    detail = result.stdout.strip()
    if result.returncode == EXIT_NO_ENTRY_INV:
        return Result(
            passed=False,
            reason=f"interface: candidate does not export {spec.entry!r} "
                   f"(required by contract {spec.name!r})",
            errors=result.stdout.splitlines(),
        )
    if result.returncode == EXIT_UNRESOLVED:
        return Result(
            passed=False,
            reason="harness: an invariant names a strategy or predicate that exists "
                   "in neither the contract's oracle nor specs/invariants.py "
                   f"({detail.removeprefix('UNRESOLVED').strip()})",
            errors=result.stdout.splitlines(),
        )
    if result.returncode == EXIT_STRATEGY_ERROR:
        return Result(
            passed=False,
            reason="harness: an invariant's strategy raised while generating inputs "
                   f"({detail.removeprefix('STRATEGY').strip()})",
            errors=result.stdout.splitlines(),
        )
    if result.returncode == EXIT_VIOLATED:
        return Result(
            passed=False,
            # The seed rides in the reason so it reaches ``reject_reason`` in the
            # episodic log. Without it a recorded violation names a counterexample
            # nobody can regenerate, which is most of the value of recording it.
            reason=f"invariant violated in sandbox (seed={seed}): "
                   f"{detail.removeprefix('VIOLATED').strip()}",
            errors=result.stdout.splitlines(),
            seed=seed,
        )
    if result.returncode != 0:
        return Result(
            passed=False,
            reason="harness: the invariant script crashed",
            errors=result.stderr.splitlines(),
        )
    return None


def _gate_backtest(ctx: _GateContext) -> Result | None:
    """Run the contract's backtests in the sandbox. Returns None when they pass.

    **Ordered after acceptance and before the differential gate**, which differs
    from the list in docs/CLASS2_CONTRACT.md. Cheapest-first: replaying a handful
    of fixtures costs far less than 300 randomised trials plus five benchmark
    repetitions, and it fails with a far sharper message ("did not reproduce q1,
    off by 12%") than a differential mismatch on an opaque random input.

    It is not a *substitute* for the differential gate and does not reorder the
    anti-gaming argument. Fixtures are few and fixed, so a candidate could in
    principle special-case them; unpredictable inputs are what make that not
    worth attempting. Backtest asks "does it match history", differential asks
    "is it right in general", and the second is the moat. The design doc's order
    places backtest after an invariant gate that does not exist yet (OMNI-18);
    revisit once it does.

    Fixtures are parsed **here**, in the main process, before anything is copied
    in. They are trusted data under ``specs/``, and validating them first means a
    malformed fixture is reported as the harness fault it is, naming the file —
    rather than surfacing as an opaque sandbox crash that reads like the
    candidate's fault. Same reason the missing-oracle and missing-tests checks
    fail loudly rather than falling through.
    """
    spec, tmp, tmpdir, env = ctx.contract, ctx.tmp, ctx.tmpdir, ctx.env
    candidate, oracle_mod = ctx.candidate, ctx.oracle
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
        # A Class-2 contract need not ship an oracle at all; when it does, its
        # comparators take precedence over the shared library.
        oracle_path=str(oracle_mod) if oracle_mod is not None else None,
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


def ensure_sandbox_ready() -> None:
    """Every precondition for executing generated code. Call before any ``_run``.

    Two checks that used to live inline in ``validate()``, hoisted because
    ``validate`` stopped being the only caller that executes generated code:
    ``sis.contract_author.check_discrimination`` runs a *drafted* test module,
    which is generated code by any reasonable reading, and it initially ran it
    without either check. A precondition that only one call site remembers is a
    precondition waiting to be skipped, so there is now one function to call and
    a test asserting both callers call it.
    """
    ensure_sandbox_allows_proposer()
    if sandbox_mode() == "docker" and shutil.which("docker") is None:
        raise RuntimeError(
            "sandbox.mode=docker but the docker CLI was not found. Install Docker "
            "and build the image (docker build -t sis-gauntlet:latest -f "
            "Dockerfile.gauntlet .), or set sandbox.mode=subprocess for the "
            "subprocess sandbox."
        )
    if sandbox_mode() == "docker":
        _container_user()  # refuses root here, before a cycle does any work


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


# Exit codes ``docker run`` uses for its *own* failures: 125 = the daemon or
# the run itself failed, 126 = the command could not be invoked, 127 = not
# found. Reported in a harness reason as a diagnosis, never used as the
# decision: docker passes a container's exit code straight through, so a
# candidate that calls ``sys.exit(125)`` produces exactly the same number, and
# trusting it would let a bad candidate launder its failure into "harness" and
# out of the circuit breaker. The probe below decides (OMNI-37).
DOCKER_FAULT_CODES = frozenset({125, 126, 127})

# Gates that never touch the sandbox; a failure there cannot be a sandbox fault.
_IN_PROCESS_GATES = frozenset({GateName.AST, GateName.NOOP})

# The probe is a handful of file operations; it must not inherit a two-minute
# gate timeout when the sandbox it is checking may be the thing that hangs.
PROBE_TIMEOUT_SECONDS = 30.0

_PROBE_TOKEN = "sis-sandbox-probe-ok"


def probe_sandbox(tmpdir: str, env: dict[str, str]) -> str | None:
    """Run a known-good program in the sandbox. Returns None if it works, else why not.

    Exercises exactly what every gate needs and a candidate can't influence:
    read a file the *host* wrote into the temp dir (the check that would have
    caught the OMNI-29 uid bug, which surfaced as mypy's own "can't read file"),
    write a file back that the host then reads, and import a module from the
    temp dir. Trusted code only — nothing of the candidate's is loaded — so if
    this fails, whatever a gate just said about the candidate is not evidence.

    Uses its own files under a probe subdirectory, so it cannot disturb a
    validation that is still using *tmpdir*.
    """
    probe_dir = pathlib.Path(tmpdir) / "_sis_probe"
    probe_dir.mkdir(exist_ok=True)
    (probe_dir / "probe_in.txt").write_text(_PROBE_TOKEN, encoding="utf-8")
    (probe_dir / "sis_probe_mod.py").write_text(f"TOKEN = {_PROBE_TOKEN!r}\n", encoding="utf-8")
    out = probe_dir / "probe_out.txt"
    script = textwrap.dedent(
        f"""\
        import sys
        sys.path.insert(0, {str(probe_dir)!r})
        with open({str(probe_dir / "probe_in.txt")!r}, encoding="utf-8") as fh:
            token = fh.read()
        import sis_probe_mod
        assert token == sis_probe_mod.TOKEN, "probe token mismatch"
        with open({str(out)!r}, "w", encoding="utf-8") as fh:
            fh.write(token)
        """
    )
    result = _run([_PY, "-c", script], tmpdir, env,
                  timeout=min(PROBE_TIMEOUT_SECONDS, _timeout_seconds()))
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["no output"]
        # Safe to name here and only here: the probe is trusted code, so a
        # docker exit code from *it* really is docker's.
        what = (
            "docker could not run the container"
            if sandbox_mode() == "docker" and result.returncode in DOCKER_FAULT_CODES
            else "self-check"
        )
        return f"{what} exited {result.returncode} ({tail[0][:200]})"
    try:
        written = out.read_text(encoding="utf-8")
    except OSError as exc:
        return f"self-check wrote nothing the host can read back ({exc})"
    if written != _PROBE_TOKEN:
        return "self-check wrote back the wrong content"
    return None


def _attribute(failure: Result, gate: GateName, ctx: _GateContext) -> Result:
    """Decide whether a gate's failure is the candidate's or the sandbox's.

    Probe-before-blame (OMNI-37): a gate that ran in the sandbox and failed is
    only the candidate's fault if a known-good program still runs there. Costs
    one probe on the failure path and nothing when the candidate passes.

    Left alone: in-process gates (they never touched the sandbox), results that
    already name the harness, and timeouts (their own reject gate, and a probe
    against a hung sandbox would only add a second wait).
    """
    reason = failure.reason.lower()
    if gate in _IN_PROCESS_GATES or reason.startswith("harness:") or "timed out" in reason:
        return failure
    fault = probe_sandbox(ctx.tmpdir, ctx.env)
    if fault is None:
        return failure
    return Result(
        passed=False,
        reason=f"harness: the sandbox failed its self-check after the {gate.value} gate "
               f"failed — {fault}; the candidate was not judged",
        errors=[f"original {gate.value} verdict: {failure.reason}", *failure.errors],
    )


def _gate_ast(ctx: _GateContext) -> Result | None:
    """Syntax. The nearest thing Python has to "does it compile"."""
    try:
        ast.parse(ctx.code_str)
    except SyntaxError as exc:
        return Result(passed=False, reason=f"SyntaxError: {exc}")
    return None


def _gate_noop(ctx: _GateContext) -> Result | None:
    """A candidate identical to the baseline can never be an improvement.

    Rejected before the benchmark because an identical re-proposal (the stub
    after its own optimisation has merged) would otherwise race the margin on
    µs-scale timing noise and pass or fail at random. See KNOWN_ISSUES.md M3.

    Only in the Class-1 profile: a feature being built for the first time has no
    prior version to be identical to.
    """
    if ctx.baseline_code is None:
        return Result(
            passed=False,
            reason="harness: the no-op gate needs a baseline and none was prepared",
        )
    if ctx.code_str.strip() == ctx.baseline_code.strip():
        return Result(passed=False, reason="no change: candidate is identical to the baseline")
    return None


def _gate_mypy(ctx: _GateContext) -> Result | None:
    """Static types. Generated code must be fully annotated — see DESIGN.md §5."""
    result = _run([_PY, "-m", "mypy", "--strict", str(ctx.candidate)], ctx.tmpdir, ctx.env)
    if timed_out := _timed_out(result, "mypy"):
        return timed_out
    if result.returncode != 0:
        return Result(
            passed=False,
            reason="mypy --strict failed",
            errors=result.stdout.splitlines() + result.stderr.splitlines(),
        )
    return None


def _gate_interface(ctx: _GateContext) -> Result | None:
    """The candidate exports every symbol the contract's ``public_api`` names.

    Cheap (one import) and it fails with a statement about the *shape* of the
    diff. Without it a wrong-API candidate surfaces as a wall of acceptance-test
    failures that never names the actual problem, which is why it runs before
    acceptance (docs/CLASS2_CONTRACT.md: "fails fast if the LLM built the wrong
    shape").

    For a **stochastic** contract it additionally requires the entry point to
    accept a ``seed`` parameter. That is the one place determinism changes the
    gate stack rather than a comparator: without a seed the gauntlet cannot
    reproduce a failure, and every distributional gate downstream is measuring
    noise it cannot distinguish from a real regression.

    Note this checks *presence*, not full signatures. "Does it have the shape we
    asked for" is a structural-typing question, and mypy --strict against the
    contract's ``protocol`` is the right tool for it; duplicating that here in
    ``inspect`` would be a second, weaker implementation of the same idea.
    """
    spec = ctx.contract
    needs_seed = spec.determinism is Determinism.STOCHASTIC
    script = textwrap.dedent(
        f"""\
        import sys, inspect, importlib.util
        s = importlib.util.spec_from_file_location("candidate", {str(ctx.candidate)!r})
        m = importlib.util.module_from_spec(s)
        s.loader.exec_module(m)

        missing = [n for n in {list(spec.public_api)!r} if not hasattr(m, n)]
        if missing:
            print("MISSING", ",".join(missing))
            sys.exit(4)

        entry = getattr(m, {spec.entry!r})
        if not callable(entry):
            print("NOTCALLABLE", {spec.entry!r})
            sys.exit(6)

        if {needs_seed!r}:
            try:
                params = inspect.signature(entry).parameters
            except (TypeError, ValueError):
                params = {{}}
            if "seed" not in params:
                print("NOSEED", {spec.entry!r})
                sys.exit(5)
        """
    )
    result = _run([_PY, "-c", script], ctx.tmpdir, ctx.env)
    if timed_out := _timed_out(result, "interface"):
        return timed_out
    detail = result.stdout.strip()
    if result.returncode == 4:
        return Result(
            passed=False,
            reason=f"interface: candidate does not export "
                   f"{detail.removeprefix('MISSING').strip()!r} "
                   f"(required by contract {spec.name!r})",
        )
    if result.returncode == 6:
        return Result(
            passed=False,
            reason=f"interface: candidate's {spec.entry!r} is not callable "
                   f"(required by contract {spec.name!r})",
        )
    if result.returncode == 5:
        return Result(
            passed=False,
            reason=f"interface: contract {spec.name!r} is stochastic, so {spec.entry!r} must "
                   "accept a 'seed' parameter — without it a failure cannot be reproduced",
        )
    if result.returncode != 0:
        return Result(
            passed=False,
            reason="interface: candidate could not be imported",
            errors=result.stderr.splitlines(),
        )
    return None


def _gate_acceptance(ctx: _GateContext) -> Result | None:
    """The contract's trusted-authored acceptance tests, run against the candidate.

    Only they go into the sandbox: they ``import target``, which resolves to the
    candidate. The harness's own tests import sis modules that are not present
    there and are not the subject of validation.

    The suite is **required, not optional**. When it was missing this used to
    fall through to ``pytest <nonexistent dir>``, which exits non-zero and was
    reported as a candidate failure — blaming the candidate for a broken
    harness. Still fails closed (an unrun correctness gate must never read as a
    pass), but now says which side is at fault.
    """
    spec = ctx.contract
    tests_src = pathlib.Path(spec.tests_file)
    if not tests_src.exists():
        return Result(
            passed=False,
            reason=f"harness: contract acceptance tests missing at {spec.tests_path} "
                   "— the acceptance gate cannot run",
        )
    tests_dst = ctx.tmp / "tests"
    tests_dst.mkdir(exist_ok=True)
    (tests_dst / "__init__.py").write_text("", encoding="utf-8")
    (tests_dst / "test_target.py").write_text(
        tests_src.read_text(encoding="utf-8"), encoding="utf-8"
    )

    result = _run(
        [_PY, "-m", "pytest", str(tests_dst), "-q", "--tb=short"], ctx.tmpdir, ctx.env
    )
    if timed_out := _timed_out(result, "acceptance"):
        return timed_out
    if result.returncode != 0:
        return Result(
            passed=False,
            reason="acceptance tests failed",
            errors=result.stdout.splitlines() + result.stderr.splitlines(),
        )
    return None


class BenchmarkVerdict(str, Enum):
    """What a set of paired timing rounds supports concluding (OMNI-41).

    ``INCONCLUSIVE`` is the point of the enum. "Not faster" and "could not tell"
    are different facts, and the old gate had no way to say the second: it
    compared one candidate block against one baseline block and read the sign,
    so a machine under load could make an identical candidate look 10% faster.
    """

    ACCEPT = "accept"
    REJECT = "reject"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class BenchmarkDecision:
    """The benchmark verdict plus the evidence it was read from."""

    verdict: BenchmarkVerdict
    median_ratio: float
    ci_low: float
    ci_high: float
    rounds: int
    coverage: float

    def describe(self) -> str:
        """One-line summary of the measurement, for a reject reason."""
        return (
            f"median ratio {self.median_ratio:.4f}, "
            f"{self.coverage:.0%} interval [{self.ci_low:.4f}, {self.ci_high:.4f}] "
            f"over {self.rounds} paired rounds"
        )


def _median_interval_rank(n: int, confidence: float) -> tuple[int, float]:
    """Order-statistic rank ``k`` for a distribution-free median interval.

    The interval is the 1-based ``[x_(k), x_(n-k+1)]``, whose exact coverage is
    ``1 - 2 * P(Bin(n, 1/2) <= k-1)`` — no distributional assumption, which
    matters because timing ratios under contention are skewed, not Gaussian.
    Returns the largest ``k`` still covering ``confidence``, with its coverage.

    ``k`` rises with ``n``, so more rounds buy tolerance to outlier rounds (a GC
    pause, a scheduler hiccup) rather than just a tighter interval. ``k = 1`` is
    the full range; when even that undercovers (tiny ``n``) it is returned
    anyway, and the honest coverage comes back with it for the caller to judge.
    """
    total = float(2**n)
    best_k, best_coverage = 1, 1.0 - 2.0 / total
    for k in range(2, n // 2 + 2):
        coverage = 1.0 - 2.0 * sum(math.comb(n, i) for i in range(k)) / total
        if coverage < confidence:
            break
        best_k, best_coverage = k, coverage
    return best_k, best_coverage


def benchmark_decision(
    ratios: Sequence[float],
    *,
    max_ratio: float,
    confidence: float = DEFAULT_BENCH_CONFIDENCE,
) -> BenchmarkDecision:
    """Decide from per-round candidate/baseline latency ratios. Pure.

    Each ratio comes from one *interleaved pair* — candidate and baseline timed
    adjacently over the same freshly generated inputs — so load drifting across
    the run scales both sides of a ratio and largely cancels, instead of being
    charged to whichever side happened to run while the machine was busy.

    The verdict is read from a distribution-free interval around the *median*
    ratio, never from the mean: one pathological round should not decide a
    cycle, and the median with an outlier discarded at each end is what makes
    the gate's answer stable enough to rely on.

    - ``ACCEPT``      — the whole interval clears the margin.
    - ``REJECT``      — the whole interval misses it.
    - ``INCONCLUSIVE``— the interval straddles the margin, or there are too few
      usable rounds to form one. Not the candidate's fault, and deliberately not
      reported as "no improvement".
    """
    usable = sorted(r for r in ratios if math.isfinite(r) and r > 0.0)
    n = len(usable)
    if n < 3:
        return BenchmarkDecision(
            verdict=BenchmarkVerdict.INCONCLUSIVE,
            median_ratio=statistics.median(usable) if usable else float("nan"),
            ci_low=float("nan"),
            ci_high=float("nan"),
            rounds=n,
            coverage=0.0,
        )
    k, coverage = _median_interval_rank(n, confidence)
    low, high = usable[k - 1], usable[n - k]
    if high <= max_ratio:
        verdict = BenchmarkVerdict.ACCEPT
    elif low > max_ratio:
        verdict = BenchmarkVerdict.REJECT
    else:
        verdict = BenchmarkVerdict.INCONCLUSIVE
    return BenchmarkDecision(
        verdict=verdict,
        median_ratio=statistics.median(usable),
        ci_low=low,
        ci_high=high,
        rounds=n,
        coverage=coverage,
    )


def _gate_differential_benchmark(ctx: _GateContext) -> Result | None:
    """Differential correctness over random inputs, then a like-for-like benchmark.

    Two anti-gaming defences beyond the static acceptance cases:

    (a) Agreement with an **independent reference** over **randomised** inputs
        the candidate cannot predict — catches a diff that special-cases the
        known test and benchmark inputs but is wrong elsewhere.
    (b) The harness drives the entry function itself (never the candidate's own
        ``benchmark()``) and times candidate and baseline back-to-back on the
        **same fresh** input, many times, deciding from the paired ratios via
        :func:`benchmark_decision` (OMNI-41). Fresh inputs matter as much as
        pairing: a fixed workload timed repeatedly measures a cache, not an
        algorithm.

    Class-1 only: both halves presuppose a reference that can be evaluated on
    demand, which is exactly what a Class-2 feature does not have.
    """
    spec = ctx.contract
    if ctx.oracle is None:
        return Result(
            passed=False,
            reason=f"harness: contract oracle missing at {spec.oracle_path} "
                   "— correctness and benchmark gates cannot run",
        )
    if ctx.baseline is None:
        return Result(
            passed=False,
            reason="harness: the benchmark gate needs a baseline and none was prepared",
        )
    trials = getattr(spec, "diff_trials", DEFAULT_DIFF_TRIALS)
    max_ratio = getattr(spec, "max_latency_ratio", DEFAULT_MAX_LATENCY_RATIO)
    samples = getattr(spec, "bench_samples", DEFAULT_BENCH_SAMPLES)
    min_samples = getattr(spec, "bench_min_samples", DEFAULT_BENCH_MIN_SAMPLES)
    batch = getattr(spec, "bench_batch", DEFAULT_BENCH_BATCH)
    confidence = getattr(spec, "bench_confidence", DEFAULT_BENCH_CONFIDENCE)
    # Seeded so a verdict is reproducible, and reported on rejection — the same
    # convention the invariant gate uses, and for the same reason: without the
    # seed a surprising measurement cannot be re-run.
    bench_seed = ctx.seed
    script = textwrap.dedent(
        f"""\
        import sys, time, random, importlib.util

        def _load(path, name):
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        cand = _load({str(ctx.candidate)!r}, "candidate")
        base = _load({str(ctx.baseline)!r}, "baseline")
        oracle = _load({str(ctx.oracle)!r}, "oracle")

        entry = {spec.entry!r}
        if not hasattr(cand, entry):
            print("NOENTRY", entry)
            sys.exit(4)
        cand_fn = getattr(cand, entry)
        base_fn = getattr(base, entry)

        rng = random.Random()  # system entropy: inputs are unpredictable
        for _ in range({trials}):
            args = oracle.random_input(rng)
            if cand_fn(*args) != oracle.reference(*args):
                print("MISMATCH", args)
                sys.exit(3)

        # Tightly interleaved pairs over FRESH inputs (OMNI-41).
        #
        # Fresh inputs, not oracle.BENCH_INPUTS replayed: a fixed workload timed
        # repeatedly rewards memoisation rather than speed, so a naive
        # implementation under functools.cache measured as near-free and passed
        # every gate. An argument is now used once, so a cache can never hit.
        #
        # One pair = candidate and baseline timed back-to-back on the SAME fresh
        # input. Shared drift cancels in a ratio only in so far as the two halves
        # are adjacent in time, so the window is kept as small as the clock
        # allows and the sample count carries the precision instead. The order
        # alternates so neither side systematically runs second on warm caches.
        bench_rng = random.Random({bench_seed!r})

        def timed(fn, batch):
            start = time.perf_counter()
            for args in batch:
                fn(*args)
            return time.perf_counter() - start

        ratios = []
        for sample_i in range({samples}):
            batch = [oracle.random_input(bench_rng) for _ in range({batch})]
            if sample_i % 2 == 0:
                t_cand = timed(cand_fn, batch)
                t_base = timed(base_fn, batch)
            else:
                t_base = timed(base_fn, batch)
                t_cand = timed(cand_fn, batch)
            # A window the clock could not resolve measures nothing. Dropping it
            # is honest; benchmark_decision calls too few usable samples
            # inconclusive rather than deciding on whatever is left.
            if t_base <= 0.0 or t_cand <= 0.0:
                continue
            ratios.append(t_cand / t_base)
            # Stop early once the answer cannot change. If every sample so far
            # sits on one side of the margin then the full-range interval does
            # too, and the full-range interval is the widest one
            # benchmark_decision will ever use — so collecting more samples can
            # only narrow an interval that already decides. Most candidates are
            # either a clear win or clearly no faster, and those now cost ~{min_samples}
            # samples instead of {samples}; only a genuinely borderline one pays full
            # price. This cannot disagree with the verdict, it only stops paying
            # for confidence already bought.
            if len(ratios) >= {min_samples}:
                if max(ratios) <= {max_ratio!r} or min(ratios) > {max_ratio!r}:
                    break

        # Reported latency is measured over oracle.BENCH_INPUTS, NOT over the
        # randomised samples above, because callers compare it against
        # measure_baseline() — which uses BENCH_INPUTS — and two numbers taken
        # over different input distributions are not comparable. For `sort`,
        # whose input lengths span five orders of magnitude, that mismatch made
        # the reported candidate look slower than the baseline it beat.
        #
        # This pass is for *reporting only*; the verdict above is already fixed.
        # Keeping the gamed-workload replay out of the decision is the whole
        # point, so it must not leak back in here.
        INPUTS = oracle.BENCH_INPUTS
        print("RATIOS", " ".join(repr(r) for r in ratios))
        print("TIMES",
              repr(timed(cand_fn, INPUTS) / len(INPUTS)),
              repr(timed(base_fn, INPUTS) / len(INPUTS)))
        """
    )
    result = _run([_PY, "-c", script], ctx.tmpdir, ctx.env)
    if timed_out := _timed_out(result, "benchmark"):
        return timed_out
    if result.returncode == 4:
        return Result(
            passed=False,
            reason=f"interface: candidate does not export {spec.entry!r} "
                   f"(required by contract {spec.name!r})",
            errors=result.stdout.splitlines(),
        )
    if result.returncode == 3:
        return Result(
            passed=False,
            reason="correctness mismatch (candidate disagrees with reference — "
                   "possible benchmark gaming)",
            errors=result.stdout.splitlines(),
        )
    if result.returncode != 0:
        return Result(
            passed=False,
            reason="benchmark script crashed",
            errors=result.stderr.splitlines(),
        )

    ratios: list[float] = []
    candidate_latency = measured_baseline = float("nan")
    for line in result.stdout.splitlines():
        if line.startswith("RATIOS"):
            try:
                ratios = [float(x) for x in line.split()[1:]]
            except ValueError:
                return Result(passed=False, reason="benchmark produced non-numeric output")
        elif line.startswith("TIMES"):
            try:
                candidate_latency, measured_baseline = (float(x) for x in line.split()[1:3])
            except ValueError:
                return Result(passed=False, reason="benchmark produced non-numeric output")
    if not ratios or math.isnan(candidate_latency):
        return Result(
            passed=False,
            reason="harness: the benchmark gate produced no usable timing rounds",
            errors=result.stdout.splitlines(),
        )

    ctx.candidate_latency = candidate_latency
    decision = benchmark_decision(ratios, max_ratio=max_ratio, confidence=confidence)
    if decision.verdict is BenchmarkVerdict.ACCEPT:
        return None
    detail = (
        f"candidate {candidate_latency:.6f}s vs baseline {measured_baseline:.6f}s "
        f"per call (need ≤ {max_ratio:.0%}); {decision.describe()}; seed={bench_seed}"
    )
    if decision.verdict is BenchmarkVerdict.REJECT:
        return Result(
            passed=False,
            reason=f"no improvement: {detail}",
            latency_seconds=candidate_latency,
            seed=bench_seed,
        )
    # Inconclusive: the measurement could not separate the candidate from the
    # margin. Reported under its own reject gate rather than as "no improvement",
    # because blaming the candidate for the machine's noise is what sent us
    # chasing a flaky test instead of a noisy gate. The org records it like a
    # no-op (episodic.NEUTRAL_OUTCOMES): spend counted, no bug, no breaker
    # increment. A box that can never measure cleanly is still bounded — by the
    # CEO's hard spend cap rather than the breaker.
    return Result(
        passed=False,
        reason=f"benchmark inconclusive: {detail}",
        latency_seconds=candidate_latency,
        seed=bench_seed,
    )


# Which gate name runs which implementation. The *contract* chooses the profile
# (``Contract.gate_profile``); this table is the only place an implementation is
# named, so a gate cannot be selected that does not exist.
def _gate_slo(ctx: _GateContext) -> Result | None:
    """Time the candidate against the spec's latency budget (OMNI-24).

    Last in the profile, so everything reaching it is already correct; a
    rejection here means "correct but over budget", reported under its own
    reject gate (``slo``) so the CEO can weigh it below a correctness failure.
    The timing runs in the sandbox like every other gate; the verdict is
    computed here by :func:`sis.slo.evaluate_slo`, a pure function.

    A candidate that *raises* on a workload input is not an SLO miss — it is a
    wrong answer the correctness gates happened not to cover — so it is
    reported under ``slo_error`` and weighed as a full failure.
    """
    spec = ctx.contract
    slo = spec.slo
    if slo is None:
        return None
    if slo.workload is not None and ctx.oracle is None:
        return Result(
            passed=False,
            reason=f"harness: the SLO names workload {slo.workload!r} but the contract's "
                   "oracle module is missing — the slo gate cannot run",
        )

    script = slo_script(
        candidate_path=str(ctx.candidate),
        oracle_path=str(ctx.oracle) if ctx.oracle is not None else None,
        entry=spec.entry,
        slo=slo,
    )
    result = _run([_PY, "-c", script], ctx.tmpdir, ctx.env)
    if timed_out := _timed_out(result, "slo"):
        return timed_out
    if result.returncode == EXIT_NO_ENTRY_SLO:
        return Result(
            passed=False,
            reason=f"interface: candidate does not export {spec.entry!r} "
                   f"(required by contract {spec.name!r})",
            errors=result.stdout.splitlines(),
        )
    if result.returncode in (EXIT_NO_WORKLOAD, EXIT_BAD_WORKLOAD):
        return Result(
            passed=False,
            reason=f"harness: the SLO workload {slo.workload!r} could not be produced "
                   f"({result.stdout.strip()}) — the slo gate cannot run",
            errors=result.stdout.splitlines(),
        )
    if result.returncode == EXIT_RAISED:
        return Result(
            passed=False,
            reason="slo workload raised: the candidate raised on an SLO workload input "
                   f"({result.stdout.strip().removeprefix('RAISED').strip()})",
            errors=result.stdout.splitlines(),
        )
    out = result.stdout.strip()
    if result.returncode != 0 or not out.startswith("TIMINGS"):
        return Result(
            passed=False,
            reason="harness: the slo timing script crashed",
            errors=result.stderr.splitlines(),
        )
    try:
        bests = [float(t) for t in json.loads(out.removeprefix("TIMINGS"))]
    except (ValueError, TypeError):
        return Result(
            passed=False,
            reason="harness: the slo timing script returned unreadable timings",
            errors=result.stdout.splitlines(),
        )
    verdict = evaluate_slo(slo, bests)
    if not verdict.passed:
        return Result(passed=False, reason=verdict.reason, latency_seconds=verdict.observed_seconds)
    # A Class-2 profile has no benchmark, so this is the only latency the
    # cycle has to report; never overwrite a benchmark's measurement.
    if ctx.candidate_latency is None:
        ctx.candidate_latency = verdict.observed_seconds
    return None


_GATES: dict[GateName, Callable[[_GateContext], Result | None]] = {
    GateName.AST: _gate_ast,
    GateName.NOOP: _gate_noop,
    GateName.MYPY: _gate_mypy,
    GateName.INTERFACE: _gate_interface,
    GateName.ACCEPTANCE: _gate_acceptance,
    GateName.INVARIANT: _gate_invariant,
    GateName.BACKTEST: _gate_backtest,
    GateName.DIFFERENTIAL_BENCHMARK: _gate_differential_benchmark,
    GateName.SLO: _gate_slo,
}

# Gates that need the baseline written into the sandbox.
_NEEDS_BASELINE = frozenset({GateName.NOOP, GateName.DIFFERENTIAL_BENCHMARK})


def validate(
    code_str: str,
    baseline_latency: float = 0.0,
    *,
    baseline_source: str | None = None,
    contract: Contract | None = None,
    seed: int | None = None,
) -> Result:
    """Validate *code_str* against *contract* and return a Result.

    **The contract selects the gates.** ``validate`` runs whatever
    ``contract.gate_profile()`` asks for, cheapest first, and stops at the first
    failure. A Class-1 ``OptimizationContract`` asks for the full stack ending in
    differential correctness and a benchmark; a Class-2 ``FeatureContract`` omits
    the two gates that presuppose a reference implementation. Both classes flow
    through this one entry point — see :mod:`sis.contract`.

    *baseline_source* is the code the candidate must beat — the code the cycle is
    actually based on (the merged target), which the loop passes in. It is
    written into the sandbox and benchmarked against; the candidate never
    competes with a stale copy on disk (see docs/KNOWN_ISSUES.md H1). When
    omitted it falls back to the contract's target file on disk, so direct
    callers and tests still work. Ignored entirely by a profile with no
    baseline-dependent gate.

    *baseline_latency* is advisory only (kept for logging/compat); the pass/fail
    comparison uses a baseline measured in-sandbox over the same workload as the
    candidate, which is far less noisy.
    """
    spec: Contract = contract if contract is not None else default_contract()
    profile = spec.gate_profile()
    # A fresh seed per validation unless the caller pins one. Fresh because a
    # fixed seed means a fixed input set, and an input set a candidate could
    # learn is exactly the hole the invariant gate exists to close; pinnable
    # because reproducing a recorded violation is the point of logging the seed.
    run_seed = random.randrange(2**31) if seed is None else seed

    # Backstop: never execute untrusted proposer code in a soft sandbox (M1).
    ensure_sandbox_ready()

    # The code the candidate must beat: what the caller says the cycle is based
    # on (the merged target), NOT whatever happens to be on disk. Resolved only
    # when a gate in the profile actually needs it — a Class-2 feature has no
    # target file yet, and reading one that does not exist would fail a cycle
    # for a file whose absence is the whole point.
    baseline_code: str | None = None
    if _NEEDS_BASELINE & set(profile):
        baseline_code = (
            baseline_source if baseline_source is not None
            else pathlib.Path(spec.target_file).read_text(encoding="utf-8")
        )

    # Everything lives under the temp dir so the sandbox is self-contained
    # (in docker mode only this dir is mounted — nothing reaches the host).
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = pathlib.Path(tmpdir)
        candidate = tmp / "target.py"
        candidate.write_text(code_str, encoding="utf-8")

        # sitecustomize.py runs at interpreter startup in every gate that has
        # tmp on PYTHONPATH, installing the network guard before any candidate
        # code runs. In docker mode --network none enforces this in the kernel
        # too; the guard stays as defence in depth.
        (tmp / "sitecustomize.py").write_text(_NETWORK_GUARD, encoding="utf-8")

        baseline_mod: pathlib.Path | None = None
        if baseline_code is not None:
            # Copied into the sandbox and loaded from the mount, never from an
            # external host path.
            baseline_mod = tmp / "baseline.py"
            baseline_mod.write_text(baseline_code, encoding="utf-8")

        # The contract's oracle: reference implementation, benchmark inputs and
        # random-input generator. It is *code* and has to run beside the
        # candidate, so it is copied in as a module rather than interpolated
        # into a script as literals (which is what tied the whole gauntlet to
        # one target — L5). Optional: a Class-2 contract may declare none, and
        # the gate that requires one says so itself.
        oracle_mod: pathlib.Path | None = None
        oracle_path = spec.oracle_path
        if oracle_path is not None:
            oracle_src = pathlib.Path(PROJECT_ROOT / oracle_path)
            if oracle_src.exists():
                oracle_mod = tmp / "oracle.py"
                oracle_mod.write_text(
                    oracle_src.read_text(encoding="utf-8"), encoding="utf-8"
                )

        ctx = _GateContext(
            contract=spec, code_str=code_str, tmp=tmp, tmpdir=tmpdir,
            env=_sandbox_env(home=tmpdir, pythonpath=tmpdir), candidate=candidate,
            baseline_code=baseline_code, baseline=baseline_mod, oracle=oracle_mod,
            seed=run_seed,
        )

        for gate_name in profile:
            if failure := _GATES[gate_name](ctx):
                return _attribute(failure, gate_name, ctx)

        return Result(
            passed=True, reason="all gates passed", latency_seconds=ctx.candidate_latency
        )


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
            # Say whether the sandbox itself is broken: in the OMNI-29 rehearsal
            # this printed returncode=125 and the cycle carried on to blame the
            # candidate at a later gate (OMNI-37). validate() now probes before
            # blaming; this makes the advisory number's failure legible too.
            fault = probe_sandbox(tmpdir, env)
            diagnosis = f" The sandbox itself is broken: {fault}." if fault else ""
            print(
                "WARNING: measure_baseline() could not parse a latency from the "
                f"sandbox (returncode={result.returncode}); returning 0.0.{diagnosis} "
                f"stderr: {result.stderr.strip()[:200]}",
                file=sys.stderr,
            )
            return 0.0
