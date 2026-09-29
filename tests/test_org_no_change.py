"""A neutral cycle is benign — no bug, no breaker (needs Ray).

Parametrised over every neutral status (sis.episodic.NEUTRAL_STATUSES): the
no-op (KNOWN_ISSUES M3), the inconclusive benchmark (OMNI-41), and a new feature
that finds no further gain (OMNI-138, M27). Each param gets its own
module-scoped Ray cluster, so CEO state never leaks between them.

Its own module so it gets a fresh Ray cluster with clean CEO state: named
detached actors are singletons within a cluster, so reusing the breaker-tripped
CEO from test_org_failures.py would mask the point of this test.
"""

from types import SimpleNamespace
from typing import Any

import pytest

ray = pytest.importorskip("ray")

from sis import org  # noqa: E402

# The SWE finds nothing to improve: the candidate is identical to the baseline,
# so the gauntlet returns "no change" (reject_gate="noop") — see KNOWN_ISSUES M3.
NO_CHANGE_IMPL: dict[str, Any] = {
    "passed": False,
    "reason": "no change: candidate is identical to the baseline",
    "pr_id": None,
    "cost_usd": 0.0,
    "candidate_sha": "cafe12345678",
}

# OMNI-41: the benchmark could not separate the candidate from the margin. Not
# evidence against the candidate, so it must be exactly as benign as a no-op.
INCONCLUSIVE_IMPL: dict[str, Any] = {
    **NO_CHANGE_IMPL,
    "reason": "benchmark inconclusive: candidate 0.000251s vs baseline 0.000282s per call "
              "(need ≤ 90%); median ratio 0.9100, 96% interval [0.8600, 0.9400] "
              "over 99 paired rounds; seed=1",
}


# OMNI-138: the fourth AWS run's TES-101. sum_of_divisors had converged, the
# first step of a new feature measured 7% faster against a 10% margin, and it
# filed a bug and counted toward the breaker. The SWE now flags it as no gain.
NO_GAIN_IMPL: dict[str, Any] = {
    **NO_CHANGE_IMPL,
    "reason": "no improvement: candidate ~0.000001s vs baseline 0.000001s per call "
              "(need ≤ 90%); total-time ratio 0.9330, 95% interval [0.8947, 0.9637] "
              "over 109 paired samples; seed=105245668",
    "no_gain": True,
}


@pytest.fixture(scope="module", params=[
    pytest.param((NO_CHANGE_IMPL, "no_change"), id="no_change"),
    pytest.param((INCONCLUSIVE_IMPL, "inconclusive"), id="inconclusive"),
    pytest.param((NO_GAIN_IMPL, "no_gain"), id="no_gain"),
])
def handles(request):  # type: ignore[no-untyped-def]
    impl, status = request.param
    h = org.bootstrap()
    h["SWE"] = SimpleNamespace(
        implement=SimpleNamespace(
            remote=lambda story_id, contract_name=None: ray.put(impl)))
    h["_expected_status"] = status
    yield h
    ray.shutdown()


def test_no_change_files_no_bug(handles) -> None:  # type: ignore[no-untyped-def]
    result = org.run_cycle(handles, "already optimal", "nothing to do")
    assert result["status"] == handles["_expected_status"]
    assert "bug_id" not in result          # not a defect — no bug filed
    assert result.get("breaker_bug_id") is None


def test_no_change_never_trips_the_breaker(handles) -> None:  # type: ignore[no-untyped-def]
    # Many "nothing to do" cycles, well past the 3-failure threshold, must not
    # page a human: a no-op is not a failure.
    stories = set()
    for _ in range(6):
        r = org.run_cycle(handles, "again", "still nothing")
        assert r["status"] == handles["_expected_status"]
        assert r.get("breaker_bug_id") is None
        stories.add(r["story_id"])
    assert not ray.get(handles["CEO"].breaker_open.remote())
    # No feature started, so no PR opened: every attempt works under one plan
    # and one story (OMNI-135), where the fourth AWS run filed three of each.
    assert len(stories) == 1, stories
    # A further cycle still runs (not refused with circuit_breaker_open).
    assert org.run_cycle(handles, "x", "y")["status"] == handles["_expected_status"]


def test_the_loop_stops_when_the_target_has_converged(handles, capsys) -> None:  # type: ignore[no-untyped-def]
    # Neutral cycles cost no breaker count, so this stop is what keeps a
    # finished target from spending until the budget runs out (OMNI-138).
    from sis import loop

    results = loop.serve(handles, loop.repeat("again", "nothing left"),
                         interval_s=0.01, max_cycles=6, converged_after=2)

    assert [r["status"] for r in results] == [handles["_expected_status"]] * 2
    out = capsys.readouterr().out
    assert "has converged" in out and "reached loop.max_cycles" not in out
    assert not ray.get(handles["CEO"].breaker_open.remote()), "a converged target tripped it"


# Builds its own CEO and never reads the neutral outcome, so run it once rather
# than per param — each extra param is another Ray cluster bootstrap.
@pytest.mark.parametrize(
    "handles", [(NO_CHANGE_IMPL, "no_change")], indirect=True, ids=["no_change"])
def test_record_neutral_records_spend_but_not_a_failure(handles) -> None:  # type: ignore[no-untyped-def]
    from sis.roles import CEO

    # Own CEO instance (tiny budget) so the shared one isn't perturbed; SLO floor
    # raised out of the way to isolate the hard spend cap.
    ceo = CEO.remote(budget_usd=0.5, slo_min_spend_usd=10.0)  # type: ignore[attr-defined]
    # Neutral spend is recorded, but it's neither a failure nor an acceptance.
    assert ray.get(ceo.record_neutral.remote(cost_usd=0.3)) is None
    econ = ray.get(ceo.economics.remote())
    assert econ["spent_usd"] == 0.3
    assert econ["accepted"] == 0.0
    assert not ray.get(ceo.breaker_open.remote())
    # The hard spend cap still applies to neutral spend (0.3 + 0.3 > 0.5).
    assert ray.get(ceo.record_neutral.remote(cost_usd=0.3)) == "hard spend cap exceeded"
    assert ray.get(ceo.breaker_open.remote())


# --- OMNI-140: the run's dataset is synced while the loop runs and when it stops ---
#
# Same cluster as the neutral-cycle tests (the no_change param), and neutral
# cycles because they cost nothing and need no proposer.


class _Uploads:
    """An Uploader that counts, and can be told to fail."""

    def __init__(self, fail: bool = False) -> None:
        self.keys: list[str] = []
        self._fail = fail

    def upload(self, path: Any, key: str) -> None:
        if self._fail:
            raise ConnectionError("Read timeout on endpoint URL")
        self.keys.append(key)


def _sync_of(tmp_path: Any, uploads: _Uploads) -> Any:
    from sis.artifact_sync import ArtifactSync

    (tmp_path / "episodic.jsonl").write_text('{"cycle": 1}\n')
    return ArtifactSync(uploads, bucket="b", prefix="runs/20260929-2028/", runtime_dir=tmp_path)


@pytest.mark.parametrize(
    "handles", [(NO_CHANGE_IMPL, "no_change")], indirect=True, ids=["no_change"])
def test_the_loop_syncs_every_n_cycles_and_once_more_when_it_stops(
        handles, tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    from sis import loop

    uploads = _Uploads()
    sync = _sync_of(tmp_path, uploads)

    results = loop.serve(handles, loop.repeat("again", "nothing left"), interval_s=0.01,
                         max_cycles=5, converged_after=99,
                         artifacts=lambda: sync, artifact_sync_every=2)

    assert len(results) == 5
    # After cycles 2 and 4, then the stop.
    assert uploads.keys == ["runs/20260929-2028/episodic.jsonl"] * 3
    out = capsys.readouterr().out
    assert out.count("artifacts synced to s3://b/runs/20260929-2028/") == 1, \
        "only the last sync of a run says so; the console is not a line longer per cycle"


@pytest.mark.parametrize(
    "handles", [(NO_CHANGE_IMPL, "no_change")], indirect=True, ids=["no_change"])
def test_a_convergence_stop_syncs_too(handles, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sis import loop

    uploads = _Uploads()
    results = loop.serve(handles, loop.repeat("again", "nothing left"), interval_s=0.01,
                         max_cycles=9, converged_after=2,
                         artifacts=lambda: _sync_of(tmp_path, uploads),
                         artifact_sync_every=0)   # only when the loop stops

    assert len(results) == 2
    assert uploads.keys == ["runs/20260929-2028/episodic.jsonl"]


@pytest.mark.parametrize(
    "handles", [(NO_CHANGE_IMPL, "no_change")], indirect=True, ids=["no_change"])
def test_a_failing_sync_warns_and_the_loop_runs_on(
        handles, tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    from sis import loop

    results = loop.serve(handles, loop.repeat("again", "nothing left"), interval_s=0.01,
                         max_cycles=3, converged_after=99,
                         artifacts=lambda: _sync_of(tmp_path, _Uploads(fail=True)),
                         artifact_sync_every=1)

    assert len(results) == 3, "a bucket that times out must not stop the loop"
    err = capsys.readouterr().err
    assert err.count("WARNING: artifacts not synced to s3://b/runs/20260929-2028/") == 4
    assert "ConnectionError: Read timeout" in err
    events = [e["event"] for e in ray.get(handles["Workspace"].events.remote())]
    assert "artifacts.sync_failed" in events


@pytest.mark.parametrize(
    "handles", [(NO_CHANGE_IMPL, "no_change")], indirect=True, ids=["no_change"])
def test_a_crash_still_syncs(handles, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sis import loop
    from sis.loop import Work

    uploads = _Uploads()
    calls = 0

    def trigger() -> Work | None:
        nonlocal calls
        calls += 1
        if calls > 2:
            raise RuntimeError("the trigger blew up")
        return Work("again", "nothing left")

    with pytest.raises(RuntimeError, match="the trigger blew up"):
        loop.serve(handles, trigger, interval_s=0.01, max_cycles=9, converged_after=99,
                   artifacts=lambda: _sync_of(tmp_path, uploads), artifact_sync_every=0)

    assert uploads.keys == ["runs/20260929-2028/episodic.jsonl"], \
        "the log is the point of the run: it goes to the bucket even when the loop dies"


@pytest.mark.parametrize(
    "handles", [(NO_CHANGE_IMPL, "no_change")], indirect=True, ids=["no_change"])
def test_no_configured_sync_changes_nothing(handles) -> None:  # type: ignore[no-untyped-def]
    from sis import loop

    results = loop.serve(handles, loop.repeat("again", "nothing left"), interval_s=0.01,
                         max_cycles=2, converged_after=99, artifacts=lambda: None)
    assert len(results) == 2
