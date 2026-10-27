"""The served target (Ray Serve). Own module for its own Serve instance.

Split into two halves on purpose: the pure helpers and source-loading are
tested without Serve at all (fast, always run), and only the handful of tests
that genuinely need a live deployment pay the ~5s startup.
"""

import os
import pathlib

import pytest

ray = pytest.importorskip("ray")
serve = pytest.importorskip("ray.serve")

from sis import config, serving  # noqa: E402
from sis.contract import SORT, SUM_OF_DIVISORS  # noqa: E402
from sis.sandbox_worker import WorkerStartError  # noqa: E402

_BLUE_SRC = pathlib.Path(SORT.target_file).read_text(encoding="utf-8")
_GREEN_SRC = pathlib.Path(str(SORT.stub_candidate_path)).read_text(encoding="utf-8")


# --- pure: no Serve, no Ray -----------------------------------------------


def test_slot_names_match_the_rest_of_the_system() -> None:
    # These strings also key SelfModel._slots and DeployRecord.slot. If they
    # drift, the live topology and the digital twin describe the same canary
    # with different words — unpickable apart during an incident.
    assert serving.BLUE == "blue"
    assert serving.GREEN == "green"


def test_blue_owns_the_plain_route_and_green_a_distinct_one() -> None:
    # Both must be servable simultaneously — that IS the canary. Overlapping
    # route prefixes would make Serve reject the second application.
    assert serving.route_prefix(SORT, serving.BLUE) == "/sort"
    assert serving.route_prefix(SORT, serving.GREEN) == "/sort-green"
    assert serving.app_name(SORT, serving.GREEN) == "sort-green"


def test_routes_are_contract_derived_not_hardcoded() -> None:
    # Same generalisation property as the rest of the engine: nothing here
    # knows "sort" by name.
    assert serving.route_prefix(SUM_OF_DIVISORS, serving.BLUE) == "/sum_of_divisors"


def test_a_version_starts_in_the_sandbox_and_answers() -> None:
    worker = serving._start_worker(_GREEN_SRC, "sort_numbers", None)
    try:
        assert worker.call([[[3, 1, 2]]]).results[0].value == [1, 2, 3]
    finally:
        worker.close()


def test_a_version_without_the_entry_point_fails_to_deploy() -> None:
    # Fail at deploy time with a clear message, rather than at the first
    # request from inside a replica.
    with pytest.raises(WorkerStartError) as caught:
        serving._start_worker("def other() -> None: ...\n", "sort_numbers", None)
    assert caught.value.missing == ("sort_numbers",)


def test_the_drivers_sandbox_settings_are_what_the_replica_uses(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # OMNI-48: green's environment is scrubbed, so a SIS_SANDBOX=docker read in
    # the replica comes back blank and falls through to config.yml's default.
    # The candidate would be served in the soft sandbox while the driver had
    # asked for docker. So the driver's values are bound into the deployment.
    for key in config.SCHEMA:
        monkeypatch.setenv(key.env, os.environ.get(key.env, ""))  # restored after
    monkeypatch.setenv("SIS_SANDBOX", "docker")
    config.reset_config_cache()
    settings = serving.sandbox_settings()
    assert settings["sandbox.mode"] == "docker"
    assert "proposer.backend" in settings, "the worker's M1 check needs the proposer"

    monkeypatch.setenv("SIS_SANDBOX", "")          # what the scrub leaves green
    config.reset_config_cache()
    assert config.get("sandbox.mode") == "subprocess", "the trap this guards against"
    monkeypatch.setattr(serving, "SandboxWorker", _Unstarted)
    try:
        serving._start_worker(_GREEN_SRC, "sort_numbers", settings)
        assert config.get("sandbox.mode") == "docker"
    finally:
        config.clear_cli_overrides()
        config.reset_config_cache()


class _Unstarted:
    def __init__(self, source: str, entry: str) -> None:
        pass

    def start(self) -> "_Unstarted":
        return self


# --- live Serve ------------------------------------------------------------


@pytest.fixture(scope="module")
def slots():  # type: ignore[no-untyped-def]
    """Blue and green up at once, running *different* source."""
    import logging

    ray.init(logging_level=logging.ERROR, ignore_reinit_error=True)
    serve.start(logging_config={"log_level": "ERROR"})
    blue = serve.run(
        serving.build(SORT, source=_BLUE_SRC, version="v1", slot=serving.BLUE),
        name=serving.app_name(SORT, serving.BLUE),
        route_prefix=serving.route_prefix(SORT, serving.BLUE))
    green = serve.run(
        serving.build(SORT, source=_GREEN_SRC, version="v2", slot=serving.GREEN),
        name=serving.app_name(SORT, serving.GREEN),
        route_prefix=serving.route_prefix(SORT, serving.GREEN))
    yield {"blue": blue, "green": green}
    serve.shutdown()
    ray.shutdown()


def test_the_served_target_answers(slots) -> None:  # type: ignore[no-untyped-def]
    assert slots["blue"].invoke.remote([5, 3, 1]).result() == [1, 3, 5]


def test_both_slots_run_simultaneously_with_different_code(slots) -> None:  # type: ignore[no-untyped-def]
    # The core canary precondition: two versions live at the same time. Blue is
    # the naive bubble sort, green the merge sort — genuinely different code.
    assert slots["blue"].info.remote().result()["version"] == "v1"
    assert slots["green"].info.remote().result()["version"] == "v2"


def test_the_two_versions_agree_on_the_same_request(slots) -> None:  # type: ignore[no-untyped-def]
    # What SHADOW mode will assert on live traffic (evaluate_canary's
    # response-agreement gate). Proving it holds here means a disagreement
    # later is a real candidate defect, not a harness artefact.
    values = [9, -2, 4, 4, 0, 7]
    assert (slots["blue"].invoke.remote(values).result()
            == slots["green"].invoke.remote(values).result())


def test_a_replica_reports_its_own_version(slots) -> None:  # type: ignore[no-untyped-def]
    # The canary attributes each sample to a version from the response itself
    # rather than inferring it from which route it used — under a weighted
    # split the caller does not choose.
    assert slots["green"].info.remote().result()["slot"] == "green"


def test_http_round_trip(slots) -> None:  # type: ignore[no-untyped-def]
    requests = pytest.importorskip("requests")
    resp = requests.post("http://127.0.0.1:8000/sort", json={"args": [[3, 1, 2]]})
    assert resp.json() == {"result": [1, 2, 3], "version": "v1", "slot": "blue"}


def test_a_failing_request_returns_an_error_and_keeps_the_replica_alive(slots) -> None:  # type: ignore[no-untyped-def]
    # Load-bearing for the canary's error_rate: a bad request must be reported
    # as an error *response* attributed to a version, not a dead replica. A
    # crashed replica would read as an infrastructure fault instead of the
    # candidate defect it is — and would take the comparison down with it.
    requests = pytest.importorskip("requests")
    bad = requests.post("http://127.0.0.1:8000/sort", json={"args": [[1, "a"]]}).json()
    assert "TypeError" in bad["error"]
    assert bad["version"] == "v1"

    ok = requests.post("http://127.0.0.1:8000/sort", json={"args": [[3, 1, 2]]}).json()
    assert ok["result"] == [1, 2, 3]


def test_malformed_envelope_is_rejected(slots) -> None:  # type: ignore[no-untyped-def]
    requests = pytest.importorskip("requests")
    resp = requests.post("http://127.0.0.1:8000/sort", json={"args": "not-a-list"})
    assert "must be a list" in resp.json()["error"]


def test_the_deployment_is_stateless(slots) -> None:  # type: ignore[no-untyped-def]
    # Same input, same answer, regardless of what ran before it. Stateless is a
    # deliberate constraint (see the module docstring): it lets the canary
    # mechanics be proven without also solving blue/green state hand-off.
    slots["blue"].invoke.remote([9, 9, 9]).result()
    slots["blue"].invoke.remote([1]).result()
    assert slots["blue"].invoke.remote([3, 1, 2]).result() == [1, 2, 3]


# --- the candidate is not a Ray worker (OMNI-48, H3) --------------------------

# Reports whether it is running inside a Ray worker. A served version used to be
# exec'd in the replica, which is one: ray was imported and connected before the
# candidate's first line ran.
_WHERE_SRC = (
    "import os, sys\n"
    "def sort_numbers(values):\n"
    "    return ['ray' in sys.modules, os.getpid()]\n"
)
# Tries what H3 described: find the cluster, then a detached role actor.
_REACH_SRC = (
    "def sort_numbers(values):\n"
    "    import ray\n"
    "    ray.init(address='auto', namespace='sis')\n"
    "    return ray.get(ray.get_actor('Workspace').secret.remote())\n"
)


def _probe(source: str, name: str):  # type: ignore[no-untyped-def]
    return serve.run(serving.build_candidate(SORT, source=source, version=name),
                     name=name, route_prefix=f"/{name}")


def test_a_served_candidate_is_not_a_ray_worker(slots) -> None:  # type: ignore[no-untyped-def]
    handle = _probe(_WHERE_SRC, "sort-where")
    try:
        in_ray, _pid = handle.invoke.remote([1]).result()
    finally:
        serve.delete("sort-where")
    assert in_ray is False, "candidate code ran inside the replica's Ray worker"


def test_a_served_candidate_cannot_reach_a_role_actor_in_docker(slots) -> None:  # type: ignore[no-untyped-def]
    # The acceptance test of OMNI-48. Only docker can pass it: in the soft
    # sandbox Ray's gRPC client bypasses the Python-level egress block and
    # `address="auto"` finds the cluster by its processes, which is why M1
    # refuses that sandbox for a real proposer (the only proposer whose
    # candidate is untrusted). The driver's mode reaches the replica through
    # sandbox_settings(), so setting it here is enough.
    if not _docker_ready():
        pytest.skip("docker or the sis-gauntlet image is not available")

    @ray.remote
    class Workspace:
        def secret(self) -> str:
            return "TOKEN"

    actor = Workspace.options(name="Workspace", namespace="sis").remote()  # type: ignore[attr-defined]
    ray.get(actor.secret.remote())
    previous = os.environ.get("SIS_SANDBOX")
    os.environ["SIS_SANDBOX"] = "docker"
    config.reset_config_cache()
    try:
        handle = _probe(_REACH_SRC, "sort-reach")
        with pytest.raises(Exception) as caught:  # noqa: B017 - any failure to reach is a pass
            handle.invoke.remote([1]).result()
        serve.delete("sort-reach")
    finally:
        if previous is None:
            os.environ.pop("SIS_SANDBOX", None)
        else:
            os.environ["SIS_SANDBOX"] = previous
        config.reset_config_cache()
        ray.kill(actor)
    assert "TOKEN" not in str(caught.value)


def _docker_ready() -> bool:
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        return False
    image = str(config.get("sandbox.image"))
    return subprocess.run(["docker", "image", "inspect", image],
                          capture_output=True).returncode == 0
