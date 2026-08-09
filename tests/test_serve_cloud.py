"""ServeCloud — the Cloud port over a real Ray Serve deployment.

Same split as the other Serve modules: the pure checks (env scrub, protocol
conformance, the guard rails that fire before anything is deployed) run without
Serve at all; the live half pays one Serve startup for the module.
"""

import os

import pytest

from sis.adapters import InMemoryTelemetry
from sis.canary import CanaryMode, LiveSample, evaluate_canary
from sis.contract import SORT
from sis.ports import Cloud, RequiresHumanApproval
from sis.serve_cloud import ServeCloud

# Set at *import* time, which pytest runs during collection — i.e. before any
# test calls ray.init(). That ordering is the whole point: a Ray worker
# inherits the driver's environment when the worker is created, so a variable
# exported afterwards is invisible to it and a scrub test using one would pass
# no matter what the scrub did. (Same trap as "env vars don't reach the role
# actors", docs/KNOWN_ISSUES.md — different surface, identical cause.)
#
# A deliberately fake name rather than ANTHROPIC_API_KEY: the scrub is
# name-agnostic, so this exercises the identical code path without planting
# something that sis.settings or another test might read. The real credential
# names are covered by the pure scrubbed_env_vars() tests below.
FAKE_CREDENTIAL = "SIS_TEST_FAKE_CREDENTIAL"
FAKE_VALUE = "sk-must-not-reach-a-candidate"
os.environ[FAKE_CREDENTIAL] = FAKE_VALUE

# Source that reports an id fixed at *construction* time, so a changing answer
# means the replica was restarted.
_IDENTITY_SRC = (
    "import uuid\n_ID = uuid.uuid4().hex[:8]\n"
    "def sort_numbers(values):\n    return _ID\n"
)
# A candidate that answers differently from blue — used to prove SHADOW never
# leaks the candidate's answer to the caller.
_DIFFERENT_SRC = 'def sort_numbers(values):\n    return "green-answer"\n'
# A candidate that snitches on its own environment.
_SNITCH_SRC = (
    "import os\n"
    "def sort_numbers(values):\n"
    f"    return [os.environ.get({FAKE_CREDENTIAL!r}, '<absent>'),\n"
    "            bool(os.environ.get('PATH'))]\n"
)


# --- pure: no Serve, no Ray -----------------------------------------------


def test_serve_cloud_satisfies_the_cloud_port() -> None:
    # The port is @runtime_checkable and has three implementations now; a
    # conformance test per adapter is what stops the next port change from
    # silently dropping one (same reason InMemoryCloud/RealCloud have theirs).
    assert isinstance(ServeCloud(InMemoryTelemetry(), SORT), Cloud)


def test_env_scrub_blanks_credentials_but_keeps_operational_vars() -> None:
    from sis.serving import scrubbed_env_vars

    os.environ["ANTHROPIC_API_KEY"] = "sk-should-not-survive"
    try:
        scrub = scrubbed_env_vars()
    finally:
        del os.environ["ANTHROPIC_API_KEY"]

    assert scrub["ANTHROPIC_API_KEY"] == ""
    # PATH and the RAY_/PYTHON operational vars must survive or the replica
    # cannot boot at all.
    assert "PATH" not in scrub
    assert not [k for k in scrub if k.startswith(("RAY_", "PYTHON"))]


def test_env_scrub_blanks_rather_than_omits() -> None:
    # The whole trick: Ray's runtime_env env_vars MERGE with the inherited
    # environment, so listing only the safe vars would leave every secret
    # intact. Each unsafe var must be present with an empty value.
    from sis.serving import scrubbed_env_vars

    os.environ["SIS_FAKE_SECRET"] = "hunter2"
    try:
        scrub = scrubbed_env_vars()
    finally:
        del os.environ["SIS_FAKE_SECRET"]

    assert "SIS_FAKE_SECRET" in scrub, "omitting it would leave the real value visible"
    assert scrub["SIS_FAKE_SECRET"] == ""


def test_a_canary_without_source_is_rejected() -> None:
    # A green slot running blue's code is a canary that can only ever report
    # "no difference" — worse than no canary, because it reports success.
    cloud = ServeCloud(InMemoryTelemetry(), SORT)
    with pytest.raises(ValueError, match="needs the candidate's source"):
        cloud.deploy_canary("v2")


def test_operations_before_a_baseline_fail_loudly() -> None:
    # Fails closed: there is nothing to compare a candidate against, and a
    # canary that cannot compare must not report a verdict.
    cloud = ServeCloud(InMemoryTelemetry(), SORT)
    for call in (lambda: cloud.deploy_canary("v2", source="x"),
                 lambda: cloud.shift_traffic("v2", 1.0),
                 lambda: cloud.live_metrics("v1", 60.0)):
        with pytest.raises(RuntimeError, match="serve_blue"):
            call()


def test_promotion_still_requires_a_human() -> None:
    # CLAUDE.md hard rule. A passing canary makes a candidate *eligible*; the
    # human PR merge is what promotes.
    with pytest.raises(RequiresHumanApproval):
        ServeCloud(InMemoryTelemetry(), SORT).promote("v2")


# --- live Serve ------------------------------------------------------------


@pytest.fixture(scope="module")
def ray_serve():  # type: ignore[no-untyped-def]
    import logging

    ray = pytest.importorskip("ray")
    serve = pytest.importorskip("ray.serve")
    ray.init(logging_level=logging.ERROR, ignore_reinit_error=True)
    serve.start(logging_config={"log_level": "ERROR"})
    yield
    serve.shutdown()
    ray.shutdown()


@pytest.fixture
def cloud(ray_serve):  # type: ignore[no-untyped-def]
    """A ServeCloud with blue up, torn down after each test."""
    c = ServeCloud(InMemoryTelemetry(), SORT, mode=CanaryMode.SPLIT)
    c.serve_blue(version="v1")
    yield c
    c.shutdown()


def _post(args, url="http://127.0.0.1:8000/sort"):  # type: ignore[no-untyped-def]
    requests = pytest.importorskip("requests")
    return requests.post(url, json={"args": args}, timeout=10).json()


def test_blue_serves_the_committed_target(cloud) -> None:  # type: ignore[no-untyped-def]
    assert _post([[3, 1, 2]])["result"] == [1, 2, 3]
    assert cloud.live_version() == "v1"


def test_deploying_a_canary_does_not_restart_blue(ray_serve) -> None:  # type: ignore[no-untyped-def]
    # Regression, and the reason green is a separate application rather than a
    # third deployment in the router's graph. Re-running one combined graph to
    # add a canary cycles the blue replica — measured, not theorised — which
    # discards blue's warm state at the moment it becomes the baseline under
    # comparison. Deploy and rollback must both leave blue untouched.
    c = ServeCloud(InMemoryTelemetry(), SORT, mode=CanaryMode.SPLIT)
    c.serve_blue(source=_IDENTITY_SRC, version="v1")
    try:
        before = _post([[1]])["result"]
        c.deploy_canary("v2", source=_DIFFERENT_SRC)
        assert _post([[1]])["result"] in (before, "green-answer")
        c.shift_traffic("v2", 0.0)
        assert _post([[1]])["result"] == before, "blue restarted on canary deploy"
        c.rollback("v2")
        assert _post([[1]])["result"] == before, "blue restarted on rollback"
    finally:
        c.shutdown()


def test_traffic_shifts_to_the_candidate(cloud) -> None:  # type: ignore[no-untyped-def]
    cloud.deploy_canary("v2", source=_DIFFERENT_SRC)
    cloud.shift_traffic("v2", 1.0)
    answers = {_post([[2, 1]])["slot"] for _ in range(10)}
    assert answers == {"green"}

    cloud.shift_traffic("v2", 0.0)
    assert {_post([[2, 1]])["slot"] for _ in range(10)} == {"blue"}


def test_traffic_can_be_expressed_from_either_side(cloud) -> None:  # type: ignore[no-untyped-def]
    # Naming blue is mirrored onto green, so a caller can ramp from whichever
    # slot it happens to be holding.
    cloud.deploy_canary("v2", source=_DIFFERENT_SRC)
    cloud.shift_traffic("v1", 0.0)          # blue 0% => green 100%
    assert cloud.status()["weight"] == 1.0
    assert _post([[2, 1]])["slot"] == "green"


def test_an_unknown_version_is_rejected(cloud) -> None:  # type: ignore[no-untyped-def]
    cloud.deploy_canary("v2", source=_DIFFERENT_SRC)
    with pytest.raises(ValueError, match="unknown version"):
        cloud.shift_traffic("v9", 1.0)


def test_live_metrics_are_per_version(cloud) -> None:  # type: ignore[no-untyped-def]
    cloud.deploy_canary("v2", source=_DIFFERENT_SRC)
    cloud.shift_traffic("v2", 0.0)
    for _ in range(12):
        _post([[3, 2, 1]])

    blue = cloud.live_metrics("v1", 60.0)
    green = cloud.live_metrics("v2", 60.0)
    assert blue["samples"] == 12.0
    assert green["samples"] == 0.0, "a dark canary must not accumulate samples"
    assert blue["p95"] > 0 and blue["error_rate"] == 0.0


def test_rollback_removes_the_candidate(cloud) -> None:  # type: ignore[no-untyped-def]
    cloud.deploy_canary("v2", source=_DIFFERENT_SRC)
    cloud.shift_traffic("v2", 1.0)
    assert _post([[1]])["slot"] == "green"

    cloud.rollback("v2")
    assert cloud.status()["green_version"] is None
    assert {_post([[2, 1]])["slot"] for _ in range(8)} == {"blue"}


def _snitch_on(ray_serve, *, scrub: bool):  # type: ignore[no-untyped-def]
    """Deploy a candidate that reports its own environment, and ask it."""
    c = ServeCloud(InMemoryTelemetry(), SORT, mode=CanaryMode.SPLIT,
                   scrub_green_env=scrub)
    c.serve_blue(version="v1")
    try:
        c.deploy_canary("v2", source=_SNITCH_SRC)
        c.shift_traffic("v2", 1.0)
        return _post([[1]])["result"]
    finally:
        c.shutdown()


def test_the_green_replica_cannot_read_credentials(ray_serve) -> None:  # type: ignore[no-untyped-def]
    # The OMNI-13 decision, enforced rather than documented. A Serve replica is
    # NOT the gauntlet sandbox — egress stays open by construction, since a
    # replica exists to answer HTTP — but candidate code must not be able to
    # read a credential out of the environment it inherited from the driver.
    #
    # Paired with its negative control below, and the pair matters more than
    # either half: on its own this assertion also passes if the variable never
    # reached the replica in the first place, which is exactly how the first
    # version of this test passed while proving nothing.
    leaked, has_path = _snitch_on(ray_serve, scrub=True)
    assert leaked == "", "candidate code read a credential"
    assert has_path, "scrubbing must not blind the replica to PATH, or it cannot boot"


def test_without_the_scrub_the_credential_does_leak(ray_serve) -> None:  # type: ignore[no-untyped-def]
    # The negative control: proves the threat is real and that the scrub is
    # what closes it. Without this, the test above is indistinguishable from a
    # harness that simply never set the variable.
    leaked, _ = _snitch_on(ray_serve, scrub=False)
    assert leaked == FAKE_VALUE


# --- shadow mode -----------------------------------------------------------


@pytest.fixture
def shadow(ray_serve):  # type: ignore[no-untyped-def]
    c = ServeCloud(InMemoryTelemetry(), SORT, mode=CanaryMode.SHADOW)
    c.serve_blue(version="v1")
    yield c
    c.shutdown()


def test_shadow_never_returns_the_candidates_answer(shadow) -> None:  # type: ignore[no-untyped-def]
    # The containment property that makes shadow safe: the candidate can be
    # completely wrong and no client ever sees it. If this breaks, shadow mode
    # is silently serving unreviewed answers to real callers.
    shadow.deploy_canary("v2", source=_DIFFERENT_SRC)
    shadow.shift_traffic("v2", 1.0)
    for _ in range(8):
        answer = _post([[3, 1, 2]])
        assert answer["result"] == [1, 2, 3]
        assert answer["slot"] == "blue"


def test_shadow_records_paired_samples_and_both_latencies(shadow) -> None:  # type: ignore[no-untyped-def]
    shadow.deploy_canary("v2", source=_DIFFERENT_SRC)
    shadow.shift_traffic("v2", 1.0)
    for _ in range(10):
        _post([[5, 4, 3]])

    samples = shadow.live_samples()
    assert len(samples) == 10
    assert all(isinstance(s, LiveSample) for s in samples)
    # Both sides recorded, which is what makes the comparison paired.
    assert samples[0].baseline_response == [3, 4, 5]
    assert samples[0].candidate_response == "green-answer"
    assert shadow.live_metrics("v1", 60.0)["samples"] == 10.0
    assert shadow.live_metrics("v2", 60.0)["samples"] == 10.0


def test_a_disagreeing_candidate_fails_the_canary_verdict(shadow) -> None:  # type: ignore[no-untyped-def]
    # End to end: real traffic through a real router, into the pure verdict
    # function. This is the join that OMNI-13 exists to make possible.
    shadow.deploy_canary("v2", source=_DIFFERENT_SRC)
    shadow.shift_traffic("v2", 1.0)
    for _ in range(12):
        _post([[9, 2, 7]])

    verdict = evaluate_canary(
        [], shadow.live_samples(),
        [1.0] * 12, [1.0] * 12,
        version="v2", mode=CanaryMode.SHADOW, min_samples=10)
    assert not verdict.passed
    assert verdict.response_disagreements == 12
    assert "disagreement" in verdict.reason


def test_an_agreeing_candidate_passes(shadow) -> None:  # type: ignore[no-untyped-def]
    import pathlib

    candidate = pathlib.Path(str(SORT.stub_candidate_path)).read_text(encoding="utf-8")
    shadow.deploy_canary("v2", source=candidate)
    shadow.shift_traffic("v2", 1.0)
    for _ in range(12):
        _post([[4, 1, 3, 2]])

    verdict = evaluate_canary(
        [], shadow.live_samples(),
        [1.0] * 12, [1.0] * 12,
        version="v2", mode=CanaryMode.SHADOW, min_samples=10)
    assert verdict.passed, verdict.reason
    assert verdict.response_disagreements == 0


def test_a_failing_candidate_does_not_poison_the_sample_set(shadow) -> None:  # type: ignore[no-untyped-def]
    # A green call that raises is an error_rate movement, not a "disagreement".
    # Recording it as a sample would blame the candidate twice and, worse, make
    # a crash look like a wrong answer — different defects, different fixes.
    shadow.deploy_canary("v2", source='def sort_numbers(v):\n    raise ValueError("boom")\n')
    shadow.shift_traffic("v2", 1.0)
    for _ in range(6):
        assert _post([[2, 1]])["result"] == [1, 2]      # caller still served by blue

    assert shadow.live_samples() == []
    assert shadow.live_metrics("v2", 60.0)["error_rate"] == 1.0
    assert shadow.live_metrics("v1", 60.0)["error_rate"] == 0.0
