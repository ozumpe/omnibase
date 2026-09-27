"""sis.sandbox_worker — serve a candidate hot, from inside the sandbox, over a pipe.

OMNI-129, phase 0 of staged delivery (epic OMNI-128). A long-lived sandboxed
process holds one candidate and answers calls as JSON lines on stdin/stdout
(:mod:`sis.sandbox_worker_main`). The **host** sends the inputs, times each
exchange on its own clock and decodes the answers. The candidate never sees
the exam, the verdict or the clock that judges it.

**Why a pipe.** The docker sandbox runs with ``--network none``. A worker
reached over stdio needs no network at all, so a hot deployment keeps exactly
the gauntlet's kernel-enforced isolation. A Serve replica does not: it is an
ordinary Ray worker on the control-plane cluster (KNOWN_ISSUES H3). OMNI-48
moves the canary onto this, and OMNI-45 the gates.

**What decoding JSON buys.** Every answer arrives as dict/list/str/int/float/
bool/None, built by the host's own ``json`` module. A value with its own
``__eq__`` cannot cross the pipe, so H4 is closed by construction here. Tuples
arrive as lists, so a trusted value compared with a worker's answer has to take
the same path (:func:`as_wire`).

**The same sandbox as the gauntlet, not a second one.** The container flags,
the credential-free environment, the network guard, docker's kill-on-timeout
and the self-check that tells a broken sandbox from a failing candidate
(OMNI-37) all come from :mod:`sis.gauntlet`. Docker mode is kernel-enforced.
Subprocess mode is the soft sandbox: :func:`gauntlet.ensure_sandbox_ready`
refuses it for a real proposer (M1), exactly as for the gates.
"""

from __future__ import annotations

import json
import os
import pathlib
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import IO, Any, NoReturn

from sis import config, gauntlet

# A served call is a request, not a gate run: seconds, not the gate's minutes.
DEFAULT_CALL_TIMEOUT_S = 10.0
# Container start plus the candidate's import. Docker takes about a second.
START_TIMEOUT_S = 30.0
# A reply is the candidate's output; one this large is an attack or a bug.
MAX_REPLY_BYTES = 16 * 1024 * 1024

_WORKER_FILE = "_sis_worker.py"
_CANDIDATE_FILE = "candidate.py"
_MAIN = pathlib.Path(__file__).with_name("sandbox_worker_main.py")


class WorkerError(RuntimeError):
    """The version being served failed. ``harness`` is True when the sandbox did."""

    def __init__(self, message: str, *, harness: bool = False) -> None:
        super().__init__(message)
        self.harness = harness


class WorkerStartError(WorkerError):
    """The worker never became ready: the candidate did not load, or the sandbox broke."""


class WorkerTimeout(WorkerError):
    """A call outlived its timeout. The worker was killed."""


class WorkerDied(WorkerError):
    """The worker exited while it should have been serving."""


class WorkerProtocolError(WorkerError):
    """The worker answered with something that is not a reply to the request."""


@dataclass(frozen=True)
class CallResult:
    """One call's outcome. ``value`` is plain JSON data; ``error`` says why not."""

    ok: bool
    value: Any = None
    error: str | None = None


@dataclass(frozen=True)
class Reply:
    """A batch's outcomes, and how long the exchange took on the **host's** clock."""

    results: list[CallResult]
    elapsed_s: float


def as_wire(value: Any) -> Any:
    """*value* as it would arrive from a worker: through JSON.

    For a trusted value compared with a worker's answer, so both have taken the
    same path (a tuple becomes a list on both sides).
    """
    return json.loads(json.dumps(value))


class SandboxWorker:
    """One candidate, served from the configured sandbox until closed.

    Calls are serialised (one pipe), and :meth:`close` waits for a call in
    flight, so a hot swap drains the old version instead of cutting it off.
    """

    def __init__(
        self,
        source: str,
        entry: str,
        *,
        call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
        start_timeout_s: float = START_TIMEOUT_S,
    ) -> None:
        self._source = source
        self._entry = entry
        self._call_timeout_s = call_timeout_s
        self._start_timeout_s = start_timeout_s
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._container: str | None = None
        self._dir: str | None = None
        self._stderr: IO[bytes] | None = None
        self._buf = b""
        self._next_id = 0

    # --- lifecycle ---------------------------------------------------------------

    def start(self) -> SandboxWorker:
        """Start the sandboxed process and wait until the candidate has loaded."""
        gauntlet.ensure_sandbox_ready()  # M1: no real proposer in the soft sandbox
        self._dir = tempfile.mkdtemp(prefix="sis-worker-")  # 0700, the host user's
        workdir = pathlib.Path(self._dir)
        (workdir / _CANDIDATE_FILE).write_text(self._source, encoding="utf-8")
        (workdir / _WORKER_FILE).write_text(_MAIN.read_text(encoding="utf-8"),
                                            encoding="utf-8")
        (workdir / "sitecustomize.py").write_text(gauntlet._NETWORK_GUARD, encoding="utf-8")
        env = gauntlet._sandbox_env(home=self._dir, pythonpath=self._dir)
        argv = [str(workdir / _WORKER_FILE), str(workdir / _CANDIDATE_FILE), self._entry]
        # Host-side and outside the mounted directory: the candidate can write
        # into its own directory, not into what the host reads back as diagnostics.
        self._stderr = tempfile.TemporaryFile()
        if gauntlet.sandbox_mode() == "docker":
            self._container = f"sis-worker-{uuid.uuid4().hex[:12]}"
            args = gauntlet._docker_args(self._dir, env, str(config.get("sandbox.image")),
                                         self._container)
            cmd = [*args[:2], "-i", *args[2:], "python", *argv]
            # The docker *client* runs with the host's environment; the container
            # gets only the -e variables _docker_args passed.
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=self._stderr)
        else:
            self._proc = subprocess.Popen([sys.executable, *argv], stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=self._stderr,
                                          cwd=self._dir, env=env)
        try:
            line = self._read_line(self._start_timeout_s)
            ready = json.loads(line)
        except (WorkerError, ValueError) as exc:
            self._start_failed(f"no handshake ({exc})")
        if isinstance(ready, dict) and ready.get("ready") is True:
            return self
        error = ready.get("error") if isinstance(ready, dict) else None
        self.close()
        raise WorkerStartError(f"the candidate did not load: {error or repr(ready)}")

    def _start_failed(self, why: str) -> NoReturn:
        """The worker died or hung before its handshake. Whose fault was it?

        The candidate's import runs before the handshake, so it can exit with
        any code, docker's own 125-127 included. As for the gates (OMNI-37),
        only a trusted self-check in the same sandbox decides: if that fails
        too, the sandbox is broken, and the candidate is not blamed.
        """
        tail = self._stderr_tail()
        self.close()
        probe_dir = tempfile.mkdtemp(prefix="sis-worker-probe-")
        try:
            broken = gauntlet.probe_sandbox(
                probe_dir, gauntlet._sandbox_env(home=probe_dir, pythonpath=probe_dir))
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)
        if broken:
            raise WorkerStartError(f"harness: the sandbox is broken ({broken})", harness=True)
        raise WorkerStartError(f"the worker did not start: {why}; {tail}")

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        """Stop serving: end of input first, then a kill if it does not go."""
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                try:
                    if proc.stdin is not None:
                        proc.stdin.close()
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    self._kill()
            if self._container is not None:
                gauntlet._docker_kill(self._container)  # best-effort; --rm removes it
                self._container = None
            if self._stderr is not None:
                self._stderr.close()
                self._stderr = None
            if self._dir is not None:
                shutil.rmtree(self._dir, ignore_errors=True)
                self._dir = None

    def __enter__(self) -> SandboxWorker:
        return self if self.running else self.start()

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- serving ---------------------------------------------------------------------

    def call(self, calls: Sequence[Sequence[Any]], *, timeout_s: float | None = None) -> Reply:
        """Run *calls* (one argument list each) and return their outcomes.

        One exchange per batch: several calls share one pipe round trip, which
        matters when the candidate is faster than the pipe (sort: 3.6 µs).
        ``elapsed_s`` is measured here, on the host.
        """
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                raise WorkerDied(f"the worker is not running{self._exit_note()}")
            self._next_id += 1
            rid = self._next_id
            payload = json.dumps({"id": rid, "calls": [list(args) for args in calls]})
            started = time.perf_counter()
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write(payload.encode("utf-8") + b"\n")
                self._proc.stdin.flush()
            except OSError as exc:
                raise WorkerDied(f"the worker stopped reading ({exc}){self._exit_note()}") \
                    from exc
            line = self._read_line(self._call_timeout_s if timeout_s is None else timeout_s)
            elapsed = time.perf_counter() - started
            return Reply(self._parse(line, rid, len(calls)), elapsed)

    def _parse(self, line: bytes, rid: int, expected: int) -> list[CallResult]:
        try:
            reply = json.loads(line)
        except ValueError as exc:
            raise WorkerProtocolError(f"unreadable reply ({exc})") from exc
        if not isinstance(reply, dict) or reply.get("id") != rid:
            raise WorkerProtocolError(f"a reply to request {rid} expected, got {line[:200]!r}")
        items = reply.get("results")
        if not isinstance(items, list) or len(items) != expected:
            raise WorkerProtocolError(f"{expected} results expected, got {items!r:.200}")
        results: list[CallResult] = []
        for item in items:
            if isinstance(item, dict) and "ok" in item:
                results.append(CallResult(ok=True, value=item["ok"]))
            elif isinstance(item, dict) and isinstance(item.get("error"), str):
                results.append(CallResult(ok=False, error=item["error"]))
            else:
                raise WorkerProtocolError(f"not a call result: {item!r:.200}")
        return results

    # --- the pipe ----------------------------------------------------------------------

    def _read_line(self, timeout_s: float) -> bytes:
        """One line from the worker, or a timeout (which kills it), or its death."""
        assert self._proc is not None and self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        deadline = time.monotonic() + timeout_s
        while b"\n" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill()
                raise WorkerTimeout(f"no answer within {timeout_s:g}s; the worker was killed")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                self._proc.wait(timeout=5)
                raise WorkerDied(f"the worker exited{self._exit_note()}")
            self._buf += chunk
            if len(self._buf) > MAX_REPLY_BYTES:
                self._kill()
                raise WorkerProtocolError(f"a reply larger than {MAX_REPLY_BYTES} bytes")
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def _kill(self) -> None:
        if self._container is not None:
            gauntlet._docker_kill(self._container)  # killing the client leaves it running
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def _exit_note(self) -> str:
        code = None if self._proc is None else self._proc.poll()
        note = "" if code is None else f" (exit code {code})"
        tail = self._stderr_tail()
        return f"{note}; {tail}" if tail else note

    def _stderr_tail(self) -> str:
        """The end of the worker's stderr: the candidate's text, so data only."""
        if self._stderr is None:
            return ""
        self._stderr.seek(0)
        lines = self._stderr.read().decode("utf-8", "replace").strip().splitlines()
        return f"stderr: {lines[-1][:300]}" if lines else ""


class HotSlot:
    """One served version at a time, replaced without stopping whoever calls it.

    :meth:`deploy` starts the new version *before* taking it into service, so a
    candidate that fails to load never replaces a working one, and callers keep
    being answered by the old version while the new one starts. The old worker
    is closed after the switch, once any call it is serving has finished.
    """

    def __init__(self, entry: str, **worker_options: float) -> None:
        self._entry = entry
        self._options = worker_options
        self._lock = threading.Lock()
        self._worker: SandboxWorker | None = None
        self._version: str | None = None

    @property
    def version(self) -> str | None:
        return self._version

    def deploy(self, version: str, source: str) -> None:
        new = SandboxWorker(source, self._entry, **self._options).start()
        with self._lock:
            old, self._worker, self._version = self._worker, new, version
        if old is not None:
            old.close()

    def call(self, calls: Sequence[Sequence[Any]], *, timeout_s: float | None = None) -> Reply:
        with self._lock:
            worker = self._worker
        if worker is None:
            raise WorkerDied("nothing is deployed in this slot")
        return worker.call(calls, timeout_s=timeout_s)

    def close(self) -> None:
        with self._lock:
            worker, self._worker, self._version = self._worker, None, None
        if worker is not None:
            worker.close()
