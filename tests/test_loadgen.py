"""The load generator. Pure tests first; the live-Serve ones are marked at the
bottom and pay the Serve startup once."""

import time

import pytest

from sis import loadgen
from sis.adapters import InMemoryCloud, InMemoryTelemetry
from sis.contract import SORT, SUM_OF_DIVISORS

# --- input generation ------------------------------------------------------


def test_inputs_come_from_the_contracts_oracle() -> None:
    # One definition of "a valid input for this target", shared with the
    # offline differential gate. A second generator would be free to drift, and
    # a canary is only as good as its input distribution.
    payloads = loadgen.inputs_for(SORT, 20, seed=1)
    assert len(payloads) == 20
    assert all(isinstance(args, tuple) and isinstance(args[0], list) for args in payloads)


def test_inputs_are_varied_not_repeated() -> None:
    payloads = loadgen.inputs_for(SORT, 40, seed=2)
    sizes = {len(args[0]) for args in payloads}
    assert len(sizes) > 5, "load should exercise a spread of input sizes"


def test_inputs_follow_the_contract() -> None:
    # Contract-derived, like everything else: a different target yields its own
    # input shape with no change here.
    args = loadgen.inputs_for(SUM_OF_DIVISORS, 5, seed=3)
    assert all(isinstance(a[0], int) for a in args)


def test_seed_makes_a_run_reproducible() -> None:
    assert loadgen.inputs_for(SORT, 10, seed=42) == loadgen.inputs_for(SORT, 10, seed=42)


def test_unseeded_runs_differ() -> None:
    # The default is unpredictable traffic, which is the point.
    assert loadgen.inputs_for(SORT, 20) != loadgen.inputs_for(SORT, 20)


def test_zero_requests_is_allowed_and_negative_is_not() -> None:
    assert loadgen.inputs_for(SORT, 0) == []
    with pytest.raises(ValueError, match="count must be >= 0"):
        loadgen.inputs_for(SORT, -1)


# --- driving ---------------------------------------------------------------


def _report(call, n: int = 6, concurrency: int = 2) -> loadgen.LoadReport:  # type: ignore[no-untyped-def]
    return loadgen._drive(call, [(i,) for i in range(n)],
                          version="v", concurrency=concurrency)


def test_a_clean_run_records_every_request() -> None:
    report = _report(lambda args: args[0] * 2)
    assert report.requests == 6
    assert report.error_count == 0
    assert report.responses == [0, 2, 4, 6, 8, 10]
    assert all(latency >= 0 for latency in report.latencies)


def test_a_raised_exception_is_data_not_a_crash() -> None:
    # A failing request is a measurement (it moves error_rate), not something
    # that should abort the run and lose the other observations.
    def call(args: tuple[int]) -> int:
        if args[0] == 2:
            raise ValueError("boom")
        return args[0]

    report = _report(call)
    assert report.requests == 6          # nothing lost
    assert report.error_count == 1
    assert "ValueError: boom" in report.errors[0]


def test_an_in_band_http_error_counts_as_an_error() -> None:
    # sis.serving returns {"error": ...} with HTTP 200 on purpose, so a bad
    # request doesn't kill the replica. The generator must still count it,
    # otherwise error_rate reads 0% while every request is failing.
    report = _report(lambda args: {"error": "TypeError: nope"})
    assert report.error_count == 6
    assert report.window().error_rate == 1.0


def test_concurrency_below_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="concurrency must be >= 1"):
        _report(lambda args: args[0], concurrency=0)


def test_requests_actually_run_concurrently() -> None:
    # Concurrency is the point of this module, not a detail: the offline
    # benchmark times one call at a time in a quiet sandbox and structurally
    # cannot see contention. If this silently ran serially, the canary would be
    # measuring the same thing the gauntlet already does.
    def slow(args: tuple[int]) -> int:
        time.sleep(0.05)
        return args[0]

    serial = loadgen._drive(slow, [(i,) for i in range(8)], version="v", concurrency=1)
    parallel = loadgen._drive(slow, [(i,) for i in range(8)], version="v", concurrency=8)
    assert serial.wall_seconds > 0.3          # 8 x 50ms, one at a time
    assert parallel.wall_seconds < serial.wall_seconds / 2


def test_latency_includes_queueing() -> None:
    # Measured client-side, end to end, because that is what a caller
    # experiences and what a p99 SLO is written against. With more work than
    # workers, per-request latency must reflect the wait.
    def slow(args: tuple[int]) -> int:
        time.sleep(0.05)
        return args[0]

    report = loadgen._drive(slow, [(i,) for i in range(6)], version="v", concurrency=2)
    assert max(report.latencies) >= 0.05


# --- the report ------------------------------------------------------------


def test_window_matches_the_live_metrics_shape() -> None:
    # A load run and a Cloud.live_metrics window must be directly comparable,
    # so evaluate_canary can consume either.
    from sis.metrics import METRIC_KEYS

    report = _report(lambda args: args[0])
    assert set(report.window().as_metrics()) == set(METRIC_KEYS)


def test_throughput_is_reported() -> None:
    report = _report(lambda args: args[0])
    assert report.throughput > 0


def test_record_into_feeds_observations_to_the_cloud() -> None:
    cloud = InMemoryCloud(InMemoryTelemetry())
    _report(lambda args: args[0]).record_into(cloud)
    metrics = cloud.live_metrics("v", 60.0)
    assert metrics["samples"] == 6.0
    assert metrics["error_rate"] == 0.0


def test_record_into_attributes_errors_to_the_right_requests() -> None:
    # Regression: the first version derived which requests failed from a count,
    # assuming errors were the LAST n. They land wherever they land. The count
    # happened to come out right, so this only bites when something pairs a
    # latency with its success/failure -- e.g. excluding failed calls from a
    # percentile. Observations now keep the two bound together.
    def call(args: tuple[int]) -> int:
        if args[0] in (0, 3):          # first and middle, never last
            raise ValueError("boom")
        return args[0]

    report = _report(call)
    assert [o.failed for o in report.observations] == [True, False, False, True, False, False]

    cloud = InMemoryCloud(InMemoryTelemetry())
    report.record_into(cloud)
    assert cloud.live_metrics("v", 60.0)["error_rate"] == pytest.approx(2 / 6)


# --- against a live Serve deployment --------------------------------------


@pytest.fixture(scope="module")
def served():  # type: ignore[no-untyped-def]
    import logging
    import pathlib

    ray = pytest.importorskip("ray")
    serve = pytest.importorskip("ray.serve")
    from sis import serving

    ray.init(logging_level=logging.ERROR, ignore_reinit_error=True)
    serve.start(logging_config={"log_level": "ERROR"})
    blue = serving.serve_slot(SORT, version="v1", slot=serving.BLUE)
    green = serving.serve_slot(
        SORT, source=pathlib.Path(str(SORT.stub_candidate_path)).read_text(encoding="utf-8"),
        version="v2", slot=serving.GREEN)
    yield {"blue": blue, "green": green}
    serve.shutdown()
    ray.shutdown()


def test_drives_a_real_deployment(served) -> None:  # type: ignore[no-untyped-def]
    payloads = loadgen.inputs_for(SORT, 12, seed=5)
    report = loadgen.drive_handle(served["blue"], payloads, version="v1", concurrency=4)
    assert report.requests == 12
    assert report.error_count == 0
    assert report.window().p95 > 0


def test_both_versions_agree_under_load(served) -> None:  # type: ignore[no-untyped-def]
    # The same paired comparison SHADOW mode will make on live traffic, now
    # over concurrent generated load rather than one call at a time.
    payloads = loadgen.inputs_for(SORT, 12, seed=6)
    blue = loadgen.drive_handle(served["blue"], payloads, version="v1", concurrency=4)
    green = loadgen.drive_handle(served["green"], payloads, version="v2", concurrency=4)
    assert blue.responses == green.responses


def test_http_path_works(served) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("requests")
    payloads = loadgen.inputs_for(SORT, 6, seed=7)
    report = loadgen.drive_http(
        "http://127.0.0.1:8000/sort", payloads, version="v1", concurrency=3)
    assert report.requests == 6
    assert report.error_count == 0
    assert all(r["slot"] == "blue" for r in report.responses)
