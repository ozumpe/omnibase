"""OMNI-129: a candidate served hot from the sandbox, over a pipe. No Ray here.

The isolation tests run in both sandbox modes. Docker is the one that counts
(kernel-enforced, and what a real proposer requires); it is skipped where the
``sis-gauntlet`` image is not built, as on CI. The subprocess mode is the soft
sandbox, and ``ensure_sandbox_ready`` refuses it for a real proposer (M1).
"""

from __future__ import annotations

import functools
import shutil
import subprocess
from collections.abc import Iterator

import pytest

from sis import config, gauntlet
from sis.policy import ChangeTier, classify
from sis.sandbox_worker import (
    HotSlot,
    SandboxWorker,
    WorkerDied,
    WorkerStartError,
    WorkerTimeout,
    as_wire,
)


@functools.cache
def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    image = str(config.get("sandbox.image"))
    probe = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    return probe.returncode == 0


@pytest.fixture(autouse=True)
def _fresh_config() -> Iterator[None]:
    config.reset_config_cache()
    yield
    config.reset_config_cache()


@pytest.fixture(params=["subprocess", "docker"])
def mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "docker" and not _docker_ready():
        pytest.skip("docker or the sis-gauntlet image is not available")
    monkeypatch.setenv("SIS_SANDBOX", request.param)
    config.reset_config_cache()
    return str(request.param)


def _serve(source: str, entry: str = "f", **options: float) -> SandboxWorker:
    return SandboxWorker(source, entry, **options).start()


def _one(worker: SandboxWorker, *args: object, timeout_s: float | None = None) -> object:
    (result,) = worker.call([list(args)], timeout_s=timeout_s).results
    return result.value if result.ok else f"ERROR {result.error}"


# --- serving ------------------------------------------------------------------------


def test_serves_a_candidate_in_batches(mode: str) -> None:
    with _serve("def f(xs):\n    return sorted(xs)\n") as worker:
        reply = worker.call([[[3, 1, 2]], [[5, 4]], [[]]])
    assert [r.value for r in reply.results] == [[1, 2, 3], [4, 5], []]
    assert reply.elapsed_s > 0


def test_a_chatty_candidate_cannot_corrupt_the_channel(mode: str) -> None:
    source = ("print('at import', flush=True)\n"
              "def f(n):\n    print('{\"id\": 99, \"results\": []}', flush=True)\n"
              "    return n + 1\n")
    with _serve(source) as worker:
        assert _one(worker, 1) == 2 and _one(worker, 2) == 3


def test_a_raising_call_is_an_error_not_a_dead_worker() -> None:
    source = "def f(n):\n    if n < 0:\n        raise ValueError('negative')\n    return n\n"
    with _serve(source) as worker:
        assert _one(worker, -1) == "ERROR ValueError: negative"
        assert _one(worker, 5) == 5 and worker.running


def test_answers_arrive_as_plain_data() -> None:
    # H4 by construction: a value with its own __eq__ cannot cross the pipe.
    source = ("class Liar(int):\n    def __eq__(self, other):\n        return True\n"
              "    __hash__ = int.__hash__\n"
              "def f(kind):\n"
              "    return {'liar': Liar(0), 'tuple': (1, 2), 'object': object()}[kind]\n")
    with _serve(source) as worker:
        liar = worker.call([["liar"]]).results[0].value
        assert type(liar) is int and liar == 0 and liar != 1
        assert _one(worker, "tuple") == [1, 2] == as_wire((1, 2))
        assert str(_one(worker, "object")).startswith("ERROR not JSON-representable")


# --- isolation ------------------------------------------------------------------------


def test_the_host_environment_stays_outside(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIS_WORKER_TEST_SECRET", "s3cret")
    source = "import os\ndef f():\n    return os.environ.get('SIS_WORKER_TEST_SECRET')\n"
    with _serve(source) as worker:
        assert _one(worker) is None


def test_the_network_is_out_of_reach(mode: str) -> None:
    source = ("import socket\n"
              "def f():\n"
              "    socket.create_connection(('1.1.1.1', 53), timeout=2).close()\n"
              "    return 'connected'\n")
    with _serve(source) as worker:
        answer = str(_one(worker))
    assert answer.startswith("ERROR") and "connected" not in answer


def test_ray_is_out_of_reach_in_docker(mode: str) -> None:
    # The H3 hole: a Serve replica is a Ray worker that can reach every
    # detached actor. The container has no Ray at all, and no network to find one.
    if mode != "docker":
        pytest.skip("the soft sandbox shares the host's packages; M1 refuses it for "
                    "a real proposer")
    with _serve("def f():\n    import ray\n    return ray.__version__\n") as worker:
        assert str(_one(worker)).startswith("ERROR ModuleNotFoundError")


def test_a_real_proposer_is_refused_in_the_soft_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    # M1, exactly as for the gates: untrusted code needs the kernel-enforced sandbox.
    monkeypatch.setenv("SIS_PROPOSER", "claude")
    monkeypatch.setenv("SIS_SANDBOX", "subprocess")
    monkeypatch.delenv("SIS_ALLOW_UNSANDBOXED_LLM", raising=False)
    config.reset_config_cache()
    with pytest.raises(RuntimeError, match="docker sandbox"):
        SandboxWorker("def f():\n    return 1\n", "f").start()


def test_the_worker_is_guardrail_code() -> None:
    for path in ("sis/sandbox_worker.py", "sis/sandbox_worker_main.py"):
        assert classify(path) is ChangeTier.FORBIDDEN, path


# --- failures ---------------------------------------------------------------------------


def test_a_hang_is_killed_by_the_timeout(mode: str) -> None:
    # Sleeps rather than spins: a hang needs no CPU, and a spinning one would
    # perturb the benchmark-timed tests running beside it under -n auto.
    worker = _serve("import time\ndef f():\n    while True:\n        time.sleep(0.05)\n")
    container = worker._container
    try:
        with pytest.raises(WorkerTimeout, match="killed"):
            worker.call([[]], timeout_s=1.0)
        assert not worker.running
        if container is not None:  # killing the docker client leaves the container
            listed = subprocess.run(["docker", "ps", "-q", "--filter", f"name={container}"],
                                    capture_output=True, text=True).stdout.strip()
            assert listed == "", "the container kept running after the timeout"
    finally:
        worker.close()


def test_a_worker_that_exits_is_reported() -> None:
    with _serve("import os\ndef f():\n    os._exit(3)\n") as worker:
        with pytest.raises(WorkerDied, match="exit code 3"):
            worker.call([[]])


def test_a_candidate_that_does_not_load_is_its_own_fault() -> None:
    with pytest.raises(WorkerStartError, match="did not load: ZeroDivisionError") as caught:
        _serve("x = 1 / 0\n")
    assert caught.value.harness is False


def test_a_missing_entry_point_does_not_load() -> None:
    with pytest.raises(WorkerStartError, match="AttributeError"):
        _serve("def g():\n    return 1\n")


def test_an_exit_at_import_is_blamed_on_the_candidate() -> None:
    # 125 is docker's own "could not run" code. Before the handshake the
    # candidate controls the exit code, so only the trusted self-check may
    # blame the sandbox (OMNI-37), and here the sandbox works.
    with pytest.raises(WorkerStartError) as caught:
        _serve("import os\nos._exit(125)\n")
    assert caught.value.harness is False


def test_a_broken_sandbox_is_not_blamed_on_the_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gauntlet, "probe_sandbox", lambda tmpdir, env: "self-check exited 1")
    with pytest.raises(WorkerStartError, match="harness") as caught:
        _serve("import os\nos._exit(1)\n")
    assert caught.value.harness is True


def test_latency_comes_from_the_host_clock() -> None:
    # The candidate owns its process, including its time module. It cannot
    # touch the host's clock, which is the one the measurement uses.
    source = ("import time\n_sleep = time.sleep\n"
              "time.perf_counter = time.monotonic = time.time = lambda: 0.0\n"
              "def f():\n    _sleep(0.2)\n    return 'done'\n")
    with _serve(source) as worker:
        reply = worker.call([[]])
    assert reply.results[0].value == "done" and reply.elapsed_s >= 0.2


# --- hot swap ---------------------------------------------------------------------------


def test_a_hot_swap_replaces_the_version_and_stops_the_old_one(mode: str) -> None:
    slot = HotSlot("f")
    try:
        slot.deploy("v1", "def f():\n    return 1\n")
        old = slot._worker
        assert slot.call([[]]).results[0].value == 1
        slot.deploy("v2", "def f():\n    return 2\n")
        assert slot.call([[]]).results[0].value == 2 and slot.version == "v2"
        assert old is not None and not old.running, "the old version kept running"
    finally:
        slot.close()


def test_a_version_that_does_not_load_never_replaces_a_working_one() -> None:
    slot = HotSlot("f")
    try:
        slot.deploy("v1", "def f():\n    return 1\n")
        with pytest.raises(WorkerStartError):
            slot.deploy("v2", "raise ImportError('broken')\n")
        assert slot.version == "v1" and slot.call([[]]).results[0].value == 1
    finally:
        slot.close()


def test_an_empty_slot_answers_nothing() -> None:
    with pytest.raises(WorkerDied, match="nothing is deployed"):
        HotSlot("f").call([[]])
