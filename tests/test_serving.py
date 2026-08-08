"""The served target (Ray Serve). Own module for its own Serve instance.

Split into two halves on purpose: the pure helpers and source-loading are
tested without Serve at all (fast, always run), and only the handful of tests
that genuinely need a live deployment pay the ~5s startup.
"""

import pathlib

import pytest

ray = pytest.importorskip("ray")
serve = pytest.importorskip("ray.serve")

from sis import serving  # noqa: E402
from sis.contract import SORT, SUM_OF_DIVISORS  # noqa: E402

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


def test_load_returns_the_entry_callable() -> None:
    fn = serving._load(_GREEN_SRC, "sort_numbers", "t")
    assert fn([3, 1, 2]) == [1, 2, 3]


def test_load_rejects_source_without_the_entry_point() -> None:
    # Fail at deployment build time with a clear message, rather than at the
    # first request with an AttributeError from inside a replica.
    with pytest.raises(ValueError, match="sort_numbers"):
        serving._load("def other() -> None: ...\n", "sort_numbers", "t")


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
