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
import contextlib
import hashlib
import json
import math
import os
import pathlib
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sis import canonical, config
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
    # A .pyc under a directory on sys.path is code the next import may load
    # without reading its source (an unchecked hash-based one is trusted as is),
    # so nothing in the sandbox writes one (OMNI-45, M9).
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Hypothesis keeps a cache under ./.hypothesis even with its example database
    # off; the working directory is the exam, so it goes to the scratch instead.
    env["HYPOTHESIS_STORAGE_DIRECTORY"] = os.path.join(home, ".hypothesis")
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

    **The exam directory is read-only** whenever the sandbox has a scratch
    directory of its own, i.e. ``env["HOME"]`` is not *tmpdir* (OMNI-45, M9):
    the oracle, baseline, tests and the candidate's own file are then mounted
    ``:ro``, and only the scratch (``HOME``/``TMPDIR``, mypy's cache) is writable,
    so a candidate cannot rewrite what a later gate trusts. Callers that pass
    ``HOME == tmpdir`` (the contract author's discrimination check, the sandbox
    worker) keep the single read-write mount.

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
        *_mounts(tmpdir, env.get("HOME")),
        "-w", tmpdir,
    ]
    for key, value in env.items():
        if key != "PATH":  # let the image set its own PATH
            args += ["-e", f"{key}={value}"]
    args.append(image)
    return args


def _mounts(tmpdir: str, scratch: str | None) -> list[str]:
    """``docker run -v`` arguments: exam dir read-only beside a separate scratch, or one rw dir."""
    if scratch and scratch != tmpdir:
        return ["-v", f"{tmpdir}:{tmpdir}:ro", "-v", f"{scratch}:{scratch}:rw"]
    return ["-v", f"{tmpdir}:{tmpdir}:rw"]


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
    # The invariant gate's generation seed, on a violation — and the benchmark
    # gate's input seed on a reject or inconclusive (OMNI-41). Also carried in the
    # reason string so it survives into the episodic log's reject_reason without
    # every caller having to plumb a new field.
    seed: int | None = None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ended_without_verdict(stdout: str, nonce: str) -> bool:
    """True unless the script printed its verdict token (KNOWN_ISSUES M8).

    A zero exit code says the process ended, not that it reached a verdict:
    ``os._exit(0)`` at import ends every gate's script with 0 before a single
    assertion runs. So the harness generates a token per run, the script prints
    it after the last check, and a zero exit is believed only with that line in
    the output. Anywhere in it, not last: a candidate may print at exit, and a
    candidate that ended the process early cannot have printed the token.

    Since OMNI-146 the scripts run in a harness process the candidate never
    runs in, so it cannot find the token or end the process; the check stays as
    a second line behind that, and still catches a harness that died early.
    """
    token = f"OK {nonce}"
    return not any(line.strip() == token for line in stdout.splitlines())


# pytest's own closing line: "10 passed in 0.05s", "3 passed, 1 warning in 0.4s".
_PYTEST_PASSED = re.compile(r"^\d+ passed\b.* in [\d.]+s\b")
_PYTEST_BROKEN = re.compile(r"\b\d+ (failed|error|errors)\b")


def _pytest_passed(stdout: str) -> bool:
    """Whether pytest's own summary says tests ran and none failed (M8).

    Any line, not the last. Since OMNI-146 pytest runs in a harness process the
    candidate never runs in, and this stays as a second line behind that.
    """
    lines = [line.strip() for line in stdout.splitlines()]
    return (any(_PYTEST_PASSED.match(line) for line in lines)
            and not any(_PYTEST_BROKEN.search(line) for line in lines))


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
    # Chosen once per validation and handed to the invariant gate and the
    # benchmark gate's input stream, so either verdict is reproducible from the
    # log alone.
    seed: int = DEFAULT_SEED
    # Filled in by the differential+benchmark gate; reported on success.
    candidate_latency: float | None = None
    # The sandbox's own writable directory (HOME/TMPDIR, tool caches), kept
    # apart from ``tmp`` and off ``sys.path`` (OMNI-45, M9). None for a hand-built
    # context, which keeps the old single directory.
    scratch: pathlib.Path | None = None
    # Every file the host put in ``tmp`` for the sandbox to trust, with its
    # sha256. What is in ``tmp`` after a gate must be exactly this.
    trusted: dict[str, str] = field(default_factory=dict)
    # The caller's measure_baseline() number, when it passed one: what the
    # benchmark reports a candidate's latency against, for display only.
    baseline_latency: float = 0.0

    def put(self, rel: str, text: str) -> pathlib.Path:
        """Write a file the sandbox will trust, and remember what it must still say."""
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.trusted[rel] = _digest(text.encode("utf-8"))
        return path

    def tampering(self) -> str | None:
        """What a gate left different in ``tmp`` from what the host put there, or None.

        Modified, added or removed files, symlinks and special files all count.
        An added file matters as much as a changed one: ``tmp`` is on
        ``sys.path``, so a planted ``json.py`` or ``hypothesis.py`` would be
        imported by the next gate in place of the real module (M9). A context
        that recorded nothing (a hand-built one) checks nothing.
        """
        if not self.trusted:
            return None
        changed: list[str] = []
        added: list[str] = []
        for path in sorted(self.tmp.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                continue
            rel = path.relative_to(self.tmp).as_posix()
            expected = self.trusted.get(rel)
            if expected is None:
                added.append(rel)
                continue
            try:
                intact = (path.is_file() and not path.is_symlink()
                          and _digest(path.read_bytes()) == expected)
            except OSError:
                intact = False
            if not intact:
                changed.append(rel)
        removed = sorted(rel for rel in self.trusted if not (self.tmp / rel).exists())
        parts = [f"{label}: {', '.join(names[:5])}"
                 for label, names in (("modified", changed), ("added", added),
                                      ("removed", removed)) if names]
        return "; ".join(parts) or None


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


def serve_canary_problem(canary_backend: str, proposer_backend: str) -> str | None:
    """Why the Serve canary may not run this proposer's code, or ``None``. Pure.

    OMNI-49 (KNOWN_ISSUES M19), interim until OMNI-48 (H3) isolates the Serve
    canary. A green replica is an ordinary Ray worker, not the gauntlet's
    sandbox: no ``--network none``, the host filesystem readable, the named
    actors (the CEO's ``reset_breaker`` included) callable, and on the AWS box
    the instance role's credentials a request to IMDS away — the hop limit of 1
    only stops *containers* behind a bridge, not host processes. The env scrub
    blanks credentials in the replica's own environment; a same-uid process
    can still read them from ``/proc/<pid>/environ`` of the raylet or driver.

    That is fine for the stub's hand-written candidate and not for code an LLM
    wrote, wherever it runs — so the rule keys on the proposer, not on
    ``SIS_ENV``: an AWS run with the stub is as safe as a local one (M1 makes
    the same call for the gauntlet).

    No override flag, unlike M1. The legacy in-memory canary is the safe
    alternative and costs nothing, and an override here would be a switch for
    running LLM-written code as a control-plane worker — exactly H3.
    """
    if canary_backend != "serve" or proposer_backend == "stub":
        return None
    return (
        f"canary.backend='serve' with proposer.backend={proposer_backend!r} would run "
        "LLM-written code as an ordinary Ray worker in the control-plane cluster — not "
        "the gauntlet's sandbox: it can read host files and other processes' "
        "environments, call the named actors, and on AWS reach the instance's "
        "credentials through IMDS (KNOWN_ISSUES H3/M19). Leave canary.backend unset "
        "(the legacy in-memory canary) until OMNI-48 isolates the Serve canary."
    )


def ensure_canary_allows_proposer(canary_backend: str | None = None) -> None:
    """Raise if the effective canary backend may not run the configured proposer.

    *canary_backend* ``None`` means the configured one — the same resolution
    ``DevOps.canary()`` applies. See :func:`serve_canary_problem`.
    """
    backend = canary_backend or config.get("canary.backend") or "legacy"
    proposer = str(config.get("proposer.backend"))
    if problem := serve_canary_problem(str(backend), proposer):
        raise RuntimeError(problem)


def _install_canonical(ctx: _GateContext) -> pathlib.Path:
    """Copy :mod:`sis.canonical` into the harness directory and return the copy's path.

    What the acceptance, invariant and backtest gates reduce output to before
    ``==`` (OMNI-46, H4). Since OMNI-146 their output has crossed a pipe as JSON,
    so it is plain already; the check stays as the rule's second line. Rewritten
    by each gate that uses it and recorded in the context's registry, so a
    change made while a gate ran is caught as tampering (M9).
    """
    return ctx.put(
        f"{_HARNESS}/{canonical.SANDBOX_MODULE}.py",
        pathlib.Path(canonical.__file__).read_text(encoding="utf-8"),
    )


# --- the harness: a gate's trusted code, outside the candidate's process (OMNI-146) ---
#
# The acceptance, invariant and backtest gates run their trusted code (pytest,
# Hypothesis, the backtest replay) in a process on the host, in this directory
# under the exam, where ``target`` is not the candidate but a stand-in that
# sends each call to the candidate's own worker (sandbox_worker.install_proxy).
# The candidate runs only in that worker, in the configured sandbox; it never
# shares a process with the code that judges it, so it cannot print the gate's
# token, end its process early or patch its checks (KNOWN_ISSUES M8).
_HARNESS = "harness"

_PROXY_MODULE = '''\
"""Written by the gauntlet: the candidate's exports, answered by a worker of its own."""
import sys

sys.path.append({root!r})
from sis import sandbox_worker  # noqa: E402

sandbox_worker.install_proxy(
    globals(), source_path={source!r}, entry={entry!r}, exports={exports!r},
    optional={optional!r}, worker_name={name!r}, call_timeout_s={timeout!r},
)
'''

# The config a harness's worker must see as the gauntlet does. It runs in a
# new process, so a value set by a CLI flag would otherwise not reach it.
_HARNESS_CONFIG = frozenset({
    "sandbox.mode", "sandbox.image", "sandbox.memory", "sandbox.cpus",
    "sandbox.allow_unsandboxed_llm", "proposer.backend",
})


def _install_proxy(ctx: _GateContext, optional: Sequence[str] = ()) -> str:
    """Write the stand-in ``target`` module for one gate; return its worker's name.

    It stands in for the contract's ``public_api`` and for the *optional* names,
    when the candidate has them. A fresh worker name per gate, so a harness that
    timed out can have its worker's container killed by name (a SIGKILL to the
    ``docker run`` client leaves the container running).
    """
    name = f"sis-gate-{uuid.uuid4().hex[:12]}"
    spec = ctx.contract
    ctx.put(f"{_HARNESS}/target.py", _PROXY_MODULE.format(
        root=str(PROJECT_ROOT), source=str(ctx.candidate), entry=spec.entry,
        exports=list(spec.public_api), optional=list(optional), name=name,
        timeout=_timeout_seconds(),
    ))
    return name


def _names_the_tests_use(source: str) -> list[str]:
    """Every ``target.<name>`` and ``from target import <name>`` in a test module.

    The acceptance tests are trusted and may use more of the candidate than
    its ``public_api`` (sum_of_divisors' call ``target.benchmark()``). The
    stand-in serves these too, when the candidate has them.
    """
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "target"):
            names.append(node.attr)
        elif isinstance(node, ast.ImportFrom) and node.module == "target":
            names.extend(alias.name for alias in node.names if alias.name != "*")
    return list(dict.fromkeys(names))


def _harness_env(ctx: _GateContext) -> dict[str, str]:
    """The environment of a gate's harness process.

    The host's own, since the harness is trusted code and its worker's
    ``docker`` client needs the host's docker context. Minus ``PYTHON*`` and
    ``PYTEST_*`` settings meant for the process that launched it, plus what the
    sandboxed gates had: no bytecode written beside the exam, and Hypothesis's
    cache in the scratch.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "PYTEST_"))}
    for key in config.SCHEMA:
        if key.path in _HARNESS_CONFIG:
            value = config.get(key.path)
            env[key.env] = ("true" if value else "false") if isinstance(value, bool) else str(value)
    scratch = ctx.scratch or pathlib.Path(ctx.env.get("HOME", ctx.tmpdir))
    env.update({
        "PYTHONPATH": str(ctx.tmp / _HARNESS),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "HYPOTHESIS_STORAGE_DIRECTORY": str(scratch / ".hypothesis"),
    })
    return env


def _run_harness(
    ctx: _GateContext, argv: list[str], worker: str
) -> subprocess.CompletedProcess[str]:
    """Run a gate's harness on the host, with the gate's timeout.

    A timeout kills the harness's whole process group (its worker too, in the
    subprocess sandbox) and the worker's container by name (in docker).
    """
    cmd = [sys.executable, *argv]
    timeout = _timeout_seconds()
    proc = subprocess.Popen(
        cmd, cwd=ctx.tmp / _HARNESS, env=_harness_env(ctx), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        result = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=10)
        result = _timeout_result(cmd, timeout)
    if result.returncode != 0 and sandbox_mode() == "docker":
        _docker_kill(worker)  # a harness that did not end normally did not close it
    return result


def _worker_failure(
    result: subprocess.CompletedProcess[str], gate: str, spec: Contract
) -> Result | None:
    """The gate's result when the candidate's worker could not serve, else None."""
    from sis.sandbox_worker import (
        PROXY_EXIT_HARNESS,
        PROXY_EXIT_MISSING,
        PROXY_EXIT_NO_START,
        PROXY_NOTE,
    )

    if timed_out := _timed_out(result, gate):
        return timed_out
    notes = [line.removeprefix(PROXY_NOTE).strip()
             for line in result.stderr.splitlines() if line.startswith(PROXY_NOTE)]
    note = notes[-1] if notes else "no detail"
    if result.returncode == PROXY_EXIT_HARNESS:
        return Result(passed=False, reason=note if note.startswith("harness:")
                      else f"harness: the {gate} gate's worker could not start ({note})")
    if result.returncode == PROXY_EXIT_MISSING:
        return Result(passed=False, reason=f"interface: candidate does not export {note!r} "
                                           f"(required by contract {spec.name!r})")
    if result.returncode == PROXY_EXIT_NO_START:
        return Result(passed=False, reason=f"interface: the candidate did not start in its "
                                           f"worker for the {gate} gate ({note})")
    return None


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
    shared_mod = ctx.put("invariants.py", shared_src.read_text(encoding="utf-8"))

    seed = ctx.seed
    nonce = secrets.token_hex(8)
    worker = _install_proxy(ctx)
    script = invariant_script(
        candidate_path=str(ctx.tmp / _HARNESS / "target.py"),
        canonical_path=str(_install_canonical(ctx)),
        exports=list(spec.public_api),
        shared_path=str(shared_mod),
        oracle_path=str(ctx.oracle) if ctx.oracle is not None else None,
        entry=spec.entry,
        plan=[invariant_plan_entry(inv) for inv in spec.invariants],
        examples=spec.invariant_examples,
        seed=seed,
        nonce=nonce,
    )
    result = _run_harness(ctx, ["-c", script], worker)
    if failure := _worker_failure(result, "invariant", spec):
        return failure

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
    if _ended_without_verdict(result.stdout, nonce):
        return Result(
            passed=False,
            reason=f"invariant violated in sandbox (seed={seed}): the candidate ended the "
                   "process before the laws finished, so there is no verdict",
            errors=result.stdout.splitlines(),
            seed=seed,
        )
    return None


def _gate_backtest(ctx: _GateContext) -> Result | None:
    """Run the contract's backtests in the sandbox. Returns None when they pass.

    **Ordered after acceptance and before the differential gate**, which differs
    from the list in docs/CLASS2_CONTRACT.md. Cheapest-first: replaying a handful
    of fixtures costs far less than 300 randomised trials plus ~100 paired
    benchmark samples, and it fails with a far sharper message ("did not reproduce q1,
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
    spec, oracle_mod = ctx.contract, ctx.oracle
    if not spec.backtests:
        return None

    comparators_src = pathlib.Path(COMPARATORS_PATH)
    if not comparators_src.exists():
        return Result(
            passed=False,
            reason=f"harness: shared comparators missing at {COMPARATORS_PATH} "
                   "— the backtest gate cannot run",
        )
    comparators_mod = ctx.put("comparators.py", comparators_src.read_text(encoding="utf-8"))

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
        fixture_dst = ctx.put(
            f"fixtures/{index}_fixture.json", fixture_src.read_text(encoding="utf-8"))
        expect_dst = ctx.put(
            f"fixtures/{index}_expect.json", expect_src.read_text(encoding="utf-8"))
        plan.append(plan_entry(bt, fixture_path=fixture_dst, expect_path=expect_dst))

    nonce = secrets.token_hex(8)
    worker = _install_proxy(ctx)
    script = build_script(
        candidate_path=str(ctx.tmp / _HARNESS / "target.py"),
        canonical_path=str(_install_canonical(ctx)),
        comparators_path=str(comparators_mod),
        # A Class-2 contract need not ship an oracle at all; when it does, its
        # comparators take precedence over the shared library.
        oracle_path=str(oracle_mod) if oracle_mod is not None else None,
        entry=spec.entry,
        plan=plan,
        nonce=nonce,
    )
    result = _run_harness(ctx, ["-c", script], worker)
    if failure := _worker_failure(result, "backtest", spec):
        return failure
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
    if _ended_without_verdict(result.stdout, nonce):
        return Result(
            passed=False,
            reason="backtest failed: candidate did not reproduce recorded history — it ended "
                   "the process before the replay finished, so there is no verdict",
            errors=result.stdout.splitlines(),
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
    # Written where the sandbox can write: the scratch, when it has one, since the
    # exam directory is read-only in docker mode (M9).
    out = pathlib.Path(env.get("HOME", tmpdir)) / "probe_out.txt"
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
    cache = pathlib.Path(ctx.env.get("HOME", ctx.tmpdir)) / ".mypy_cache"
    result = _run(
        [_PY, "-m", "mypy", "--strict", "--cache-dir", str(cache), str(ctx.candidate)],
        ctx.tmpdir, ctx.env,
    )
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

    The candidate is imported in a worker of its own, which reports what it
    exports (OMNI-146); the verdict is decided here. That report is the
    candidate's own account, so one that claims a ``seed`` it lacks gets past
    this gate and fails the first call that passes one.
    """
    from sis.sandbox_worker import SandboxWorker, WorkerStartError

    spec = ctx.contract
    timeout = _timeout_seconds()
    try:
        worker = SandboxWorker(
            ctx.candidate.read_text(encoding="utf-8"), spec.entry, exports=spec.public_api,
            call_timeout_s=timeout, start_timeout_s=timeout,
        ).start()
    except WorkerStartError as exc:
        if exc.harness:
            return Result(passed=False, reason=str(exc))
        if exc.timed_out:
            return Result(passed=False, reason="interface gate timed out",
                          errors=str(exc).splitlines())
        if exc.missing:
            return Result(
                passed=False,
                reason=f"interface: candidate does not export {','.join(exc.missing)!r} "
                       f"(required by contract {spec.name!r})",
            )
        return Result(passed=False, reason="interface: candidate could not be imported",
                      errors=str(exc).splitlines())
    entry = worker.exports[spec.entry]
    worker.close()
    if not entry.callable:
        return Result(
            passed=False,
            reason=f"interface: candidate's {spec.entry!r} is not callable "
                   f"(required by contract {spec.name!r})",
        )
    if spec.determinism is Determinism.STOCHASTIC and "seed" not in (entry.params or ()):
        return Result(
            passed=False,
            reason=f"interface: contract {spec.name!r} is stochastic, so {spec.entry!r} must "
                   "accept a 'seed' parameter — without it a failure cannot be reproduced",
        )
    return None


_ACCEPTANCE_CONFTEST = '''\
"""Written by the gauntlet: the contract's public API returns canonical output."""
import {module}
import target

{module}.wrap_exports(target, {names!r})
'''


def _gate_acceptance(ctx: _GateContext) -> Result | None:
    """The contract's trusted-authored acceptance tests, run against the candidate.

    They ``import target``, which in the harness resolves to the stand-in for
    the candidate: pytest runs on the host, and every call reaches the
    candidate in a worker of its own (OMNI-146). So pytest's exit code and
    summary are written by a process the candidate never ran in.

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
    tests = tests_src.read_text(encoding="utf-8")
    worker = _install_proxy(ctx, optional=_names_the_tests_use(tests))
    ctx.put(f"{_HARNESS}/tests/__init__.py", "")
    ctx.put(f"{_HARNESS}/tests/test_target.py", tests)
    # Loaded by pytest before the test module imports `target`, so every
    # assertion compares plain values rather than whatever `__eq__` the
    # candidate's return type defines (OMNI-46, H4).
    _install_canonical(ctx)
    ctx.put(f"{_HARNESS}/tests/conftest.py", _ACCEPTANCE_CONFTEST.format(
        module=canonical.SANDBOX_MODULE, names=tuple(spec.public_api)))

    result = _run_harness(
        ctx, ["-m", "pytest", "tests", "-q", "--tb=short", "-p", "no:cacheprovider"], worker
    )
    if failure := _worker_failure(result, "acceptance", spec):
        return failure
    if result.returncode != 0:
        return Result(
            passed=False,
            reason="acceptance tests failed",
            errors=result.stdout.splitlines() + result.stderr.splitlines(),
        )
    if not _pytest_passed(result.stdout):
        return Result(
            passed=False,
            reason="acceptance tests failed: pytest ended without reporting that the tests "
                   "passed, so the candidate is not believed",
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
    # Too few usable timings to judge at all. Deliberately NOT neutral, so that
    # "unmeasurable" can never become a place to hide the way INCONCLUSIVE would
    # be. Since OMNI-45 the timings come from the host's clock, which the
    # candidate cannot reach, so in practice this means too few samples.
    UNMEASURABLE = "unmeasurable"


@dataclass(frozen=True)
class BenchmarkDecision:
    """The benchmark verdict plus the evidence it was read from."""

    verdict: BenchmarkVerdict
    ratio: float          # sum(candidate time) / sum(baseline time) over all pairs
    ci_low: float
    ci_high: float
    samples: int
    confidence: float

    def describe(self) -> str:
        """One-line summary of the measurement, for a reject reason."""
        return (
            f"total-time ratio {self.ratio:.4f}, "
            f"{self.confidence:.0%} interval [{self.ci_low:.4f}, {self.ci_high:.4f}] "
            f"over {self.samples} paired samples"
        )


# Fewer usable pairs than this and the gate will not decide either way.
_MIN_DECIDABLE_PAIRS = 10
# Bootstrap resamples. Deterministic (fixed seed) so benchmark_decision stays a
# pure function of its input; 1000 keeps it well under a tenth of a second.
_BOOTSTRAP_RESAMPLES = 1000


def benchmark_decision(
    pairs: Sequence[tuple[float, float]],
    *,
    max_ratio: float,
    confidence: float = DEFAULT_BENCH_CONFIDENCE,
) -> BenchmarkDecision:
    """Decide from paired ``(candidate_seconds, baseline_seconds)`` timings. Pure.

    Each pair times candidate and baseline back-to-back on the same fresh input,
    so load drifting across the run scales both halves and largely cancels.

    **The estimand is total cost** — ``sum(candidate) / sum(baseline)`` over the
    pairs — because that is what "faster" means for a workload. An earlier cut
    of this gate decided on the *median* per-pair ratio, and a pre-merge review
    broke it within the hour: a candidate fast on the typical 70% of inputs and
    4x slower on the large 30% was ~2x slower overall and passed. The interval
    is a paired bootstrap (resampling whole pairs keeps each pair's shared drift
    together), so a few expensive pairs widen it rather than silently decide.

    - ``ACCEPT``       — the interval's upper bound clears the margin.
    - ``REJECT``       — the point estimate itself misses the margin.
    - ``INCONCLUSIVE`` — the candidate *looks* faster by the margin but the
      interval cannot confirm it. Recorded as neutral
      (``episodic.NEUTRAL_OUTCOMES``), which is why it is reachable only by a
      candidate whose best estimate already clears the margin: a slow candidate
      cannot get there by being noisy.
    - ``UNMEASURABLE`` — fewer than ``_MIN_DECIDABLE_PAIRS`` usable pairs. A
      failure, not neutral (see ``BenchmarkVerdict``).

    The timings come from the host's clock, around exchanges with a worker the
    candidate runs in (OMNI-45), so the candidate cannot forge them the way it
    could while it shared the measuring process (KNOWN_ISSUES H2, fixed). A
    percentile bootstrap on raw wall-clock sums
    undercovers when scheduler stalls land in a few pairs — the false-accept
    rate at the margin is a few times the nominal (KNOWN_ISSUES M7).
    """
    usable = [(c, b) for c, b in pairs
              if math.isfinite(c) and math.isfinite(b) and c > 0.0 and b > 0.0]
    n = len(usable)
    if n < _MIN_DECIDABLE_PAIRS:
        return BenchmarkDecision(
            verdict=BenchmarkVerdict.UNMEASURABLE, ratio=float("nan"),
            ci_low=float("nan"), ci_high=float("nan"), samples=n, confidence=confidence,
        )
    cand = [c for c, _ in usable]
    base = [b for _, b in usable]
    ratio = sum(cand) / sum(base)
    rng = random.Random(0)
    resampled = sorted(
        sum(cand[i] for i in idx) / sum(base[i] for i in idx)
        for idx in (rng.choices(range(n), k=n) for _ in range(_BOOTSTRAP_RESAMPLES))
    )
    tail = (1.0 - confidence) / 2.0
    low = resampled[int(tail * (_BOOTSTRAP_RESAMPLES - 1))]
    high = resampled[int((1.0 - tail) * (_BOOTSTRAP_RESAMPLES - 1))]
    if high <= max_ratio:
        verdict = BenchmarkVerdict.ACCEPT
    elif ratio > max_ratio:
        verdict = BenchmarkVerdict.REJECT
    else:
        verdict = BenchmarkVerdict.INCONCLUSIVE
    return BenchmarkDecision(
        verdict=verdict, ratio=ratio, ci_low=low, ci_high=high,
        samples=n, confidence=confidence,
    )


# How long one timed exchange of the *baseline* should take, as a multiple of
# the pipe's own round trip (OMNI-45, H2). The exchange's fixed cost is then at
# most 1/20 of the window, and being the same for both sides it can only pull
# the ratio toward 1: toward rejecting a gain, never toward accepting a loss.
# Kept as small as that allows, because pairing cancels drift only while the two
# halves are adjacent in time (OMNI-41).
_WINDOW_OVER_ROUND_TRIP = 20
# Bounds on calls per timed exchange.
_MAX_BATCH_CALLS = 20_000
# Differential inputs per exchange: keeps replies small without a round trip each.
_DIFF_CHUNK = 50
# Draws in a row that may repeat a used input before the oracle's input range
# counts as used up (OMNI-152): this many, or a few times the number of inputs
# already used when that is more, so the last unused inputs of a small range are
# still found. Bounded, so a range that is used up costs a fixed amount to see.
_REDRAW_LIMIT = 100
_REDRAWS_PER_USED = 8
_REDRAW_CEILING = 200_000


def _hashable(value: Any) -> Any:
    """A JSON value as something a set can hold, equal exactly when the values are."""
    if isinstance(value, list):
        return tuple(_hashable(item) for item in value)
    if isinstance(value, dict):
        return frozenset((key, _hashable(item)) for key, item in value.items())
    return value


class _UnusedInputs:
    """One benchmark's inputs, none of them given out twice (OMNI-152, H7).

    Drawing a fresh input each time is not enough. Once the benchmark asks for
    more inputs than the oracle's range holds, most draws repeat an earlier
    one, and a candidate under ``functools.cache`` answers those from a
    dictionary: it measures faster without being faster. ``sum_of_divisors``
    draws from 19,999 values, and a fast baseline asked for 99 batches of 1024.

    So every input is remembered and a repeat is drawn again. Inputs are
    compared with ``==``, at least as coarsely as any cache could key them: a
    dict keys ``1``, ``1.0`` and ``True`` alike, so here they are one input. A
    range that runs short gives fewer inputs, never a repeated one.
    """

    def __init__(self, draw: Callable[[random.Random], list[Any]]) -> None:
        self._draw = draw
        self._used: set[Any] = set()

    def use(self, args: list[Any]) -> bool:
        """Mark *args* as used. False if they already were."""
        key = _hashable(args)
        if key in self._used:
            return False
        self._used.add(key)
        return True

    def take(self, rng: random.Random, count: int) -> list[list[Any]]:
        """Up to *count* inputs never used before: fewer once the range runs short."""
        taken: list[list[Any]] = []
        repeats = 0
        while len(taken) < count:
            args = self._draw(rng)
            if self.use(args):
                taken.append(args)
                repeats = 0
                continue
            repeats += 1
            if repeats >= min(max(_REDRAW_LIMIT, _REDRAWS_PER_USED * len(self._used)),
                              _REDRAW_CEILING):
                break
        return taken


@dataclass(frozen=True)
class _TimedWork:
    """What the benchmark will time: one batch of unused inputs per sample."""

    batches: list[list[list[Any]]]  # empty when the range cannot give each sample one input
    batch: int                      # inputs per batch
    capped: bool                    # the oracle's range, not the timing, set the batch
    found: int                      # unused inputs found for timing


def _timed_work(
    unused: _UnusedInputs,
    rng: random.Random,
    *,
    samples: int,
    min_batch: int,
    window: float,
    time_baseline: Callable[[list[list[Any]]], float],
    max_batch: int = _MAX_BATCH_CALLS,
) -> _TimedWork:
    """Choose the timed batches: sized by time, filled only with unused inputs.

    The batch doubles until *time_baseline* (one exchange with the baseline's
    worker, in seconds) takes at least *window* for a batch of that size. Every
    input, the sizing ones included, comes from *unused*, so none is used twice
    (OMNI-152, H7).

    The timed inputs are set aside **before** each sizing exchange. Sizing
    first would let a small range be spent on sizing and leave the timing with
    nothing. When the range cannot fill the size the timing asks for, the batch
    is what the inputs found can fill: smaller, never repeated.
    """
    batch = max(1, min_batch)
    pool = unused.take(rng, samples * batch)
    capped = len(pool) < samples * batch
    while not capped and batch < max_batch:
        probe = unused.take(rng, batch)
        if len(probe) < batch:
            pool += probe  # never sent to a worker, so still unused
            capped = True
            break
        if time_baseline(probe) >= window:
            break
        batch = min(batch * 2, max_batch)
        pool += unused.take(rng, samples * batch - len(pool))
        capped = len(pool) < samples * batch
    batch = min(batch, len(pool) // samples)
    return _TimedWork(
        batches=[pool[index * batch:(index + 1) * batch] for index in range(samples)]
        if batch else [],
        batch=batch, capped=capped, found=len(pool),
    )


def _host_oracle(spec: Contract) -> Any:
    """The contract's oracle, loaded on the host from ``specs/`` (trusted, human-written).

    From the repository, never from the sandbox's copy: the harness's reference
    must not be a file the candidate's sandbox could reach.
    """
    import importlib.util

    path = PROJECT_ROOT / str(spec.oracle_path)
    module_spec = importlib.util.spec_from_file_location(f"_sis_host_oracle_{spec.name}", path)
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


class _WorkerFailed(Exception):
    """A worker failed mid-gate; carries the gate's verdict for it."""

    def __init__(self, result: Result) -> None:
        super().__init__(result.reason)
        self.result = result


def _gate_differential_benchmark(ctx: _GateContext) -> Result | None:
    """Differential correctness over random inputs, then a like-for-like benchmark.

    Two anti-gaming defences beyond the static acceptance cases:

    (a) Agreement with an **independent reference** over **randomised** inputs
        the candidate cannot predict — catches a diff that special-cases the
        known test and benchmark inputs but is wrong elsewhere.
    (b) Candidate and baseline are timed back-to-back on the **same fresh**
        inputs, many times, deciding from the paired ratios via
        :func:`benchmark_decision` (OMNI-41). Fresh inputs matter as much as
        pairing: a fixed workload timed repeatedly measures a cache, not an
        algorithm. **No input is timed twice, and none is timed after a worker
        has met it** (OMNI-152, :class:`_UnusedInputs`, :func:`_timed_work`):
        not in another timed batch, not in a sizing exchange, and not in (a).
        When the oracle's input range is too small for the batch the timing
        asks for, the batch shrinks instead, and each exchange's fixed cost
        weighs more. A range that cannot give every sample one unused input is
        the exam's fault, reported as ``harness:``.

    **The candidate never shares a process with the clock, the reference or the
    verdict** (OMNI-45, closes KNOWN_ISSUES H2). Candidate and baseline each run
    in their own :class:`~sis.sandbox_worker.SandboxWorker`, in the configured
    sandbox; the reference runs here, loaded from ``specs/``; every exchange is
    timed on this process's clock, and every answer arrives as JSON this
    process decodes. Before, the candidate ran inside the script that timed it
    and printed the verdict, and could write a forged verdict and exit.

    **Timed in batches, sized by time.** An exchange with a worker costs a round
    trip (about 20 µs in the subprocess sandbox, a few hundred in docker), far
    more than a µs-scale call. So each exchange carries a batch of fresh inputs,
    sized so the baseline's batch takes about ``_WINDOW_OVER_ROUND_TRIP`` round
    trips. What batching cannot amortise is the per-call cost of sending an
    input and its answer as JSON (about 0.2 µs for an int). The worker's loop
    runs no Python per call (OMNI-153, H8), so a candidate cannot make its own
    share of that cheaper. The same for both sides, it dilutes a gain rather
    than invents one, so the gate resolves differences well under a µs poorly
    — a known limit.

    **Every timed answer is checked.** Candidate against baseline, and where
    they differ, against the reference: a candidate that tells a timing batch
    from a correctness batch (by size, say) and answers the first wrongly and
    fast is rejected, not measured.

    What remains outside this gate: the candidate's worker can still contend
    for CPU while the baseline's batch runs (bounded by the sandbox's ``--cpus``
    in docker; the subprocess sandbox can do more and is refused for a real
    proposer).

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
    if ctx.baseline_code is None:
        return Result(
            passed=False,
            reason="harness: the benchmark gate needs a baseline and none was prepared",
        )
    try:
        oracle = _host_oracle(spec)
    except Exception as exc:  # noqa: BLE001 - trusted code that fails is the harness's fault
        return Result(passed=False, reason=f"harness: the contract oracle does not load ({exc})")

    from sis.sandbox_worker import SandboxWorker, WorkerStartError

    deadline = time.monotonic() + _timeout_seconds()
    try:
        cand = SandboxWorker(ctx.code_str, spec.entry).start()
    except WorkerStartError as exc:
        return _candidate_start_failure(exc, spec)
    try:
        try:
            base = SandboxWorker(ctx.baseline_code, spec.entry).start()
        except WorkerStartError as exc:
            return Result(passed=False, reason=f"harness: the baseline did not start in its "
                                               f"worker ({exc}) — the benchmark cannot run")
        try:
            return _judge(ctx, oracle, cand, base, deadline)
        except _WorkerFailed as failed:
            return failed.result
        finally:
            base.close()
    finally:
        cand.close()


def _candidate_start_failure(exc: Exception, spec: Contract) -> Result:
    if getattr(exc, "harness", False):
        return Result(passed=False, reason=str(exc))
    text = str(exc)
    if getattr(exc, "missing", ()):
        return Result(passed=False,
                      reason=f"interface: candidate does not export {spec.entry!r} "
                             f"(required by contract {spec.name!r})")
    return Result(passed=False, reason=f"benchmark: the candidate did not start in its worker "
                                       f"({' '.join(text.split())[:300]})")


def _exchange(worker: Any, calls: list[list[Any]], deadline: float, who: str) -> Any:
    """One timed exchange, or the gate's verdict on why it could not happen."""
    from sis.sandbox_worker import WorkerError, WorkerTimeout

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _WorkerFailed(Result(passed=False, reason="benchmark gate timed out"))
    try:
        return worker.call(calls, timeout_s=remaining)
    except WorkerTimeout as exc:
        raise _WorkerFailed(Result(passed=False, reason="benchmark gate timed out",
                                   errors=[str(exc)])) from exc
    except WorkerError as exc:
        detail = " ".join(str(exc).split())[:300]
        if who == "baseline":
            raise _WorkerFailed(Result(
                passed=False, reason=f"harness: the baseline's worker failed ({detail})")) from exc
        # Not "harness:": the candidate can cause this (exit, or write to the
        # channel). _attribute's probe decides whether the sandbox is at fault.
        raise _WorkerFailed(Result(
            passed=False, reason=f"benchmark: the candidate's worker failed ({detail})")) from exc


def _mismatch(args: Any, why: str) -> _WorkerFailed:
    return _WorkerFailed(Result(
        passed=False,
        reason="correctness mismatch (candidate disagrees with reference — "
               "possible benchmark gaming)",
        errors=[f"MISMATCH {args!r:.300}", why],
    ))


def _judge(ctx: _GateContext, oracle: Any, cand: Any, base: Any,
           deadline: float) -> Result | None:
    import copy as _copy

    from sis.sandbox_worker import as_wire

    spec = ctx.contract
    trials = getattr(spec, "diff_trials", DEFAULT_DIFF_TRIALS)
    max_ratio = getattr(spec, "max_latency_ratio", DEFAULT_MAX_LATENCY_RATIO)
    samples = getattr(spec, "bench_samples", DEFAULT_BENCH_SAMPLES)
    min_batch = getattr(spec, "bench_batch", DEFAULT_BENCH_BATCH)
    confidence = getattr(spec, "bench_confidence", DEFAULT_BENCH_CONFIDENCE)
    # Seeded, and reported on rejection — the same convention the invariant
    # gate uses. The seed fixes the order of the draws. Which of them are timed
    # also depends on (a)'s inputs, which come from system entropy and are
    # skipped here, so a seed alone does not replay a benchmark's inputs.
    bench_seed = ctx.seed

    def reference(args: list[Any]) -> Any:
        return as_wire(oracle.reference(*_copy.deepcopy(args)))

    def fresh(rng: random.Random) -> list[Any]:
        # The wire form for both sides and the reference: a tuple becomes a list
        # for all three, and each worker decodes its own copy (M10 by construction).
        return list(as_wire(list(oracle.random_input(rng))))

    unused = _UnusedInputs(fresh)

    # (a) Differential correctness, inputs from system entropy. The candidate
    # has met these, so none of them is timed later.
    rng = random.Random()
    inputs = [fresh(rng) for _ in range(trials)]
    for args in inputs:
        unused.use(args)
    for start in range(0, len(inputs), _DIFF_CHUNK):
        chunk = inputs[start:start + _DIFF_CHUNK]
        reply = _exchange(cand, chunk, deadline, "candidate")
        for args, got in zip(chunk, reply.results, strict=True):
            if not got.ok:
                raise _mismatch(args, f"the candidate raised: {got.error}")
            if got.value != reference(args):
                raise _mismatch(args, "the candidate's answer differs from the reference")

    # (b) The pipe's own round trip, after a warm-up, then a batch size. The
    # median of ten, not the fastest: on a loaded machine the best case is the
    # rare one, and a window sized to it let one slow exchange end the sizing at
    # a single call, where the pipe's cost hid a 200x gain (OMNI-141).
    for worker, who in ((cand, "candidate"), (base, "baseline")):
        _exchange(worker, [], deadline, who)
    trips = sorted(
        _exchange(worker, [], deadline, who).elapsed_s
        for _ in range(5) for worker, who in ((cand, "candidate"), (base, "baseline"))
    )
    round_trip = trips[len(trips) // 2]
    window = _WINDOW_OVER_ROUND_TRIP * round_trip
    bench_rng = random.Random(bench_seed)
    # The contract's own BENCH_INPUTS are timed too, at most once each, spread
    # among the fresh batches: the oracle chose them for shape coverage
    # random_input lacks (sort: already-sorted, reverse-sorted, heavy-duplicate).
    # Claimed before any fresh draw, so none repeats one. One the candidate met
    # in (a) is left out: its answer was checked there, and it is not unused.
    shapes = [args for args in (list(as_wire(list(given))) for given in oracle.BENCH_INPUTS)
              if unused.use(args)]
    timed = _timed_work(
        unused, bench_rng, samples=samples, min_batch=min_batch, window=window,
        time_baseline=lambda probe: float(
            _exchange(base, probe, deadline, "baseline").elapsed_s),
    )
    if not timed.batches:
        # The exam's fault, not the candidate's: no candidate can cause or cure
        # it. A human has to widen the oracle's random_input.
        return Result(
            passed=False,
            reason=f"harness: the contract oracle's random_input gives too few distinct "
                   f"inputs to benchmark — {timed.found} unused found, and {samples} samples "
                   f"need one each, since no input is timed twice (seed={bench_seed})",
            seed=bench_seed,
        )
    batch = timed.batch
    sized = f"{batch} call(s) each" + (
        ", capped by the oracle's input range" if timed.capped else "")
    work = timed.batches
    for args in shapes:
        work[bench_rng.randrange(len(work))].append(args)

    pairs: list[tuple[float, float]] = []
    base_calls = 0
    base_seconds = 0.0
    for index, calls in enumerate(work):
        # Alternating order, so neither side systematically runs second.
        if index % 2 == 0:
            c_reply = _exchange(cand, calls, deadline, "candidate")
            b_reply = _exchange(base, calls, deadline, "baseline")
        else:
            b_reply = _exchange(base, calls, deadline, "baseline")
            c_reply = _exchange(cand, calls, deadline, "candidate")
        for args, got, theirs in zip(calls, c_reply.results, b_reply.results, strict=True):
            if not got.ok:
                raise _mismatch(args, f"the candidate raised while being timed: {got.error}")
            if theirs.ok and got.value == theirs.value:
                continue
            if got.value != reference(args):
                raise _mismatch(args, "the candidate answered wrongly while being timed")
        pairs.append((c_reply.elapsed_s, b_reply.elapsed_s))
        base_calls += len(calls)
        base_seconds += b_reply.elapsed_s

    decision = benchmark_decision(pairs, max_ratio=max_ratio, confidence=confidence)
    if decision.verdict is BenchmarkVerdict.UNMEASURABLE:
        return Result(
            passed=False,
            reason=f"benchmark unmeasurable: only {decision.samples} usable timing pairs "
                   f"of {len(pairs)} (need {_MIN_DECIDABLE_PAIRS}); seed={bench_seed}",
            seed=bench_seed,
        )
    # Per-call latencies are for display (the PR's evidence, the episodic log);
    # the verdict never uses them. The caller's measure_baseline() number when it
    # passed one, so a step's reported gain is measured on one workload.
    measured_baseline = ctx.baseline_latency if ctx.baseline_latency > 0 else max(
        (base_seconds - len(pairs) * round_trip) / max(base_calls, 1), 1e-9)
    candidate_latency = measured_baseline * decision.ratio
    ctx.candidate_latency = candidate_latency
    if decision.verdict is BenchmarkVerdict.ACCEPT:
        return None
    detail = (
        f"candidate ~{candidate_latency:.6f}s vs baseline {measured_baseline:.6f}s "
        f"per call (need ≤ {max_ratio:.0%}); {decision.describe()} of {sized}; "
        f"seed={bench_seed}"
    )
    if decision.verdict is BenchmarkVerdict.REJECT:
        return Result(passed=False, reason=f"no improvement: {detail}",
                      latency_seconds=candidate_latency, seed=bench_seed)
    # Inconclusive: the best estimate clears the margin, the interval cannot
    # confirm it. Neutral in the org (episodic.NEUTRAL_OUTCOMES); benchmark_decision
    # makes it unreachable for a candidate whose best estimate is slower.
    return Result(passed=False, reason=f"benchmark inconclusive: {detail}",
                  latency_seconds=candidate_latency, seed=bench_seed)


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
    decision times candidate and baseline in-sandbox, back-to-back in pairs on
    the same fresh inputs, which is far less noisy (OMNI-41).
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

    # Two directories, so the sandbox is self-contained (in docker mode only
    # these are mounted — nothing reaches the host): the exam, which holds every
    # file a gate trusts and is on sys.path, and a scratch the sandbox may write
    # to. The exam is read-only in docker, and in both modes it is checked after
    # every gate against what the host put there (OMNI-45, M9).
    with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as scratchdir:
        tmp = pathlib.Path(tmpdir)
        ctx = _GateContext(
            contract=spec, code_str=code_str, tmp=tmp, tmpdir=tmpdir,
            env=_sandbox_env(home=scratchdir, pythonpath=tmpdir), candidate=tmp / "target.py",
            baseline_code=baseline_code, seed=run_seed, scratch=pathlib.Path(scratchdir),
            baseline_latency=baseline_latency,
        )
        ctx.put("target.py", code_str)

        # sitecustomize.py runs at interpreter startup in every gate that has
        # tmp on PYTHONPATH, installing the network guard before any candidate
        # code runs. In docker mode --network none enforces this in the kernel
        # too; the guard stays as defence in depth.
        ctx.put("sitecustomize.py", _NETWORK_GUARD)

        if baseline_code is not None:
            # Copied into the sandbox and loaded from the mount, never from an
            # external host path.
            ctx.baseline = ctx.put("baseline.py", baseline_code)

        # The contract's oracle: reference implementation, benchmark inputs and
        # random-input generator. It is *code* and has to run beside the
        # candidate, so it is copied in as a module rather than interpolated
        # into a script as literals (which is what tied the whole gauntlet to
        # one target — L5). Optional: a Class-2 contract may declare none, and
        # the gate that requires one says so itself.
        oracle_path = spec.oracle_path
        if oracle_path is not None:
            oracle_src = pathlib.Path(PROJECT_ROOT / oracle_path)
            if oracle_src.exists():
                ctx.oracle = ctx.put("oracle.py", oracle_src.read_text(encoding="utf-8"))

        for gate_name in profile:
            try:
                failure = _GATES[gate_name](ctx)
            except Exception as exc:  # noqa: BLE001 - reported, never propagated (L15)
                # The gate's own code raised: a harness fault, not a verdict on
                # the candidate. Returned rather than raised, so the cycle keeps
                # its episodic record, its spend and its breaker count (OMNI-64).
                detail = " ".join(f"{type(exc).__name__}: {exc}".split())[:300]
                return Result(passed=False,
                              reason=f"harness: the {gate_name.value} gate raised ({detail})",
                              errors=traceback.format_exc().splitlines())
            # Before anything is concluded from the gate: a gate that ran beside
            # a candidate which rewrote the exam has not judged it. Checked when
            # the gate passed too, since a pass is exactly what tampering buys.
            if changed := ctx.tampering():
                return Result(
                    passed=False,
                    reason=f"tampered: the candidate changed the exam files while the "
                           f"{gate_name.value} gate ran ({changed})",
                )
            if failure:
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
            import copy, time, importlib.util

            def _load(path, name):
                s = importlib.util.spec_from_file_location(name, path)
                mod = importlib.util.module_from_spec(s)
                s.loader.exec_module(mod)
                return mod

            m = _load({str(module)!r}, "m")
            oracle = _load({str(oracle_mod)!r}, "oracle")

            fn = getattr(m, {spec.entry!r})
            best = float("inf")
            # A fresh copy per repetition (OMNI-47): a function that sorts its
            # input in place would otherwise time repetitions 2-5 on sorted data.
            for inputs in [copy.deepcopy(oracle.BENCH_INPUTS) for _ in range(5)]:
                start = time.perf_counter()
                for args in inputs:
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
