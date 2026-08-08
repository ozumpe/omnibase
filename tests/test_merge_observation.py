"""Observing the human PR merge (OMNI-15).

Closes the loop's last open end: `Cloud.promote()` had no caller at all, so a
successful cycle left green occupied forever and `main.py --loop` idled after
cycle one. The system now *notices* that a human merged — it still never merges
and never decides to promote.

Needs Ray, and its own module for a fresh cluster: the named detached actors are
cluster singletons.
"""

import pytest

ray = pytest.importorskip("ray")

from sis import loop, org  # noqa: E402
from sis.ports import RequiresHumanApproval  # noqa: E402


@pytest.fixture(scope="module")
def handles():  # type: ignore[no-untyped-def]
    h = org.bootstrap()
    yield h
    ray.shutdown()


@pytest.fixture
def canary(handles):  # type: ignore[no-untyped-def]
    """A PR with a canary deployed against it, green held."""
    ws, sm = handles["Workspace"], handles["SelfModel"]
    branch = ray.get(ws.create_branch.remote("feature/merge-obs", "develop")).name
    pr = ray.get(ws.open_pr.remote(branch, "OMNI-15 test", "code"))
    ray.get(handles["DevOps"].canary.remote(pr.id, 0.001))
    yield pr
    ray.get(sm.set_slot.remote("green", None))
    ray.get(sm.set_pending_pr.remote(None))


def _deployment(handles):  # type: ignore[no-untyped-def]
    return ray.get(handles["SelfModel"].deployment.remote())


# --- pure ------------------------------------------------------------------


def test_pending_merge_reads_the_recorded_pr() -> None:
    assert loop.pending_merge({"pending_pr": "PR-7"}) == "PR-7"
    assert loop.pending_merge({"pending_pr": None}) is None
    assert loop.pending_merge({}) is None


def test_version_string_is_one_definition() -> None:
    # canary() writes the version and observe_merge() looks it up by the same
    # string; a mismatch would promote nothing while reporting success.
    from sis.ports import PullRequest
    from sis.roles import _version_for

    pr = PullRequest(id="PR-3", branch="feature/x", title="t")
    assert _version_for(pr) == "feature/x@PR-3"


# --- the observation -------------------------------------------------------


def test_canary_records_which_pr_would_release_it(handles, canary) -> None:  # type: ignore[no-untyped-def]
    # The watcher needs an exact PR id. Recovering it by splitting the version
    # string is guesswork — a branch name may itself contain "@".
    assert _deployment(handles)["pending_pr"] == canary.id


def test_an_unmerged_pr_promotes_nothing(handles, canary) -> None:  # type: ignore[no-untyped-def]
    # The common case on any given tick, and the one that must be inert: a
    # human takes hours, and every poll until then must change nothing.
    result = ray.get(handles["DevOps"].observe_merge.remote(canary.id))

    assert result["merged"] is False and result["promoted"] is False
    assert loop.canary_in_flight(_deployment(handles)) is not None
    assert ray.get(handles["Workspace"].live_version.remote()) is None


def test_no_role_can_reach_a_merge_at_all() -> None:  # type: ignore[no-untyped-def]
    # The load-bearing half of the design: promotion is gated on `pr.merged`,
    # so the guarantee is only worth as much as the agent's inability to set it.
    # Two independent locks, and this asserts both:
    #   1. Workspace — the ONLY surface a role has — exposes no merge at all.
    #   2. The adapter underneath raises even if something reached it.
    from sis.adapters import InMemoryTelemetry, InMemoryVersionControl
    from sis.workspace import Workspace

    assert not [m for m in dir(Workspace) if "merge" in m.lower()], (
        "Workspace grew a merge-shaped method; a role could then authorise its "
        "own promotion"
    )

    vcs = InMemoryVersionControl(InMemoryTelemetry())
    pr = vcs.open_pr(vcs.create_branch("feature/y").name, "t")
    with pytest.raises(RequiresHumanApproval):
        vcs.merge_pr(pr.id)
    assert vcs.get_pr(pr.id).merged is False


def test_observing_a_human_merge_promotes_and_releases_green(handles, canary) -> None:  # type: ignore[no-untyped-def]
    # The end-to-end close: a human merges out of band, the loop notices, the
    # candidate becomes live and green frees up so the next cycle may start.
    _mark_merged(handles, canary.id)

    result = ray.get(handles["DevOps"].observe_merge.remote(canary.id))

    assert result["merged"] is True and result["promoted"] is True
    assert result["slot"] == "blue" and result["live"] is True

    deployment = _deployment(handles)
    assert deployment["live_version"] == f"{canary.branch}@{canary.id}"
    assert loop.canary_in_flight(deployment) is None, "green must be released"
    assert loop.pending_merge(deployment) is None


def test_observing_twice_does_not_double_promote(handles, canary) -> None:  # type: ignore[no-untyped-def]
    # A poll fires every tick; the merge stays merged forever after. The second
    # observation must be a no-op rather than a second promotion record.
    _mark_merged(handles, canary.id)
    ray.get(handles["DevOps"].observe_merge.remote(canary.id))

    again = ray.get(handles["DevOps"].observe_merge.remote(canary.id))
    assert again["promoted"] is False and again["reason"] == "already live"


def test_the_merge_is_recorded_in_provenance(handles, canary) -> None:  # type: ignore[no-untyped-def]
    # spec → story → branch/PR → deploy → outcome must include the promotion,
    # or the graph stops at the canary and never says what became live.
    _mark_merged(handles, canary.id)
    ray.get(handles["DevOps"].observe_merge.remote(canary.id))

    kinds = [e["kind"] for e in ray.get(handles["SelfModel"].provenance.remote())]
    assert "promote" in kinds


def test_a_real_cycle_reaches_promotion(handles) -> None:  # type: ignore[no-untyped-def]
    # The genuine end-to-end, on a real cycle rather than a hand-built fixture:
    # intake → ... → canary → (human merges) → promoted. Before OMNI-15 the
    # provenance graph simply stopped at "canary" and never recorded what
    # became live, because nothing could ever call promote().
    from sis import org

    result = org.run_cycle(handles, "Speed up divisor-sum", "Too slow; same results.")
    assert result["status"] == "verified_awaiting_human_merge"

    pr_id = loop.pending_merge(_deployment(handles))
    assert pr_id, "a finished cycle must record the PR its canary waits on"

    _mark_merged(handles, pr_id)
    assert ray.get(handles["DevOps"].observe_merge.remote(pr_id))["promoted"] is True

    deployment = _deployment(handles)
    assert loop.canary_in_flight(deployment) is None
    assert deployment["live_version"]
    kinds = [e["kind"] for e in ray.get(handles["SelfModel"].provenance.remote())]
    assert kinds[-1] == "promote", "provenance must terminate in the promotion"


# --- loop integration ------------------------------------------------------


def test_the_loop_releases_itself_when_a_human_merges(handles, canary) -> None:  # type: ignore[no-untyped-def]
    # The whole point of OMNI-15. Before it, serve() with the one-canary gate on
    # ran a single cycle and then idled forever, because nothing ever cleared
    # green. Now the tick that sees the merge is also the tick that may proceed.
    import threading

    _mark_merged(handles, canary.id)
    consulted: list[int] = []

    def _trigger():  # type: ignore[no-untyped-def]
        consulted.append(1)
        return None            # released, but no work to do — keeps this fast

    stop = threading.Event()
    threading.Timer(0.4, stop.set).start()
    loop.serve(handles, _trigger, interval_s=0.01, stop_event=stop)

    assert consulted, "the loop stayed held even though the PR was merged"
    assert loop.canary_in_flight(_deployment(handles)) is None


def test_watch_merges_can_be_turned_off(handles, canary) -> None:  # type: ignore[no-untyped-def]
    # Escape hatch: hold until an operator releases it by hand.
    import threading

    _mark_merged(handles, canary.id)
    consulted: list[int] = []

    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    loop.serve(handles, lambda: consulted.append(1), interval_s=0.01,
               stop_event=stop, watch_merges=False)

    assert consulted == []
    assert loop.canary_in_flight(_deployment(handles)) is not None


def _mark_merged(handles, pr_id: str) -> None:  # type: ignore[no-untyped-def]
    """Simulate a human merging on GitHub.

    Goes in through Ray's ``__ray_call__`` rather than a Workspace method on
    purpose: adding a production passthrough for this would punch a hole in the
    very guarantee under test (see
    ``test_no_role_can_reach_a_merge_at_all``). The test may reach into the
    actor; the agent may not. Against the real adapter there is no seam at all
    — ``merged`` comes from GitHub's API.
    """
    ray.get(handles["Workspace"].__ray_call__.remote(
        lambda self, pid: self.vcs.simulate_human_merge(pid), pr_id))
