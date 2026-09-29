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
    pr = ray.get(ws.open_pr.remote(branch, "OMNI-15 test", "code", "runtime/target.py"))
    ray.get(handles["DevOps"].canary.remote(pr.id, 0.001))
    yield pr
    ray.get(sm.set_slot.remote("green", None))
    ray.get(sm.set_pending_pr.remote(None))
    # Decided, as a human would: an agent PR left open now holds every later
    # test's loop, which is exactly the point of OMNI-136.
    _mark_closed(handles, pr.id)


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
    pr = vcs.open_pr(vcs.create_branch("feature/y").name, "t", path="runtime/target.py")
    with pytest.raises(RequiresHumanApproval):
        vcs.merge_pr(pr.id)
    assert vcs.get_pr(pr.id, path=None).merged is False


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


# --- a PR a human declines (OMNI-57) ------------------------------------------


def test_a_human_closing_the_pr_releases_green(handles, canary) -> None:  # type: ignore[no-untyped-def]
    # Before OMNI-57 only a merge ended a hold, so a declined PR held the
    # canary, and under loop.serve the whole loop, forever.
    _mark_closed(handles, canary.id)

    result = ray.get(handles["DevOps"].observe_merge.remote(canary.id))

    assert result["released"] is True and result["promoted"] is False
    assert result["reason"] == "closed without merging"
    deployment = _deployment(handles)
    assert loop.canary_in_flight(deployment) is None and loop.pending_merge(deployment) is None
    assert ray.get(handles["Workspace"].live_version.remote()) != result["version"]


def test_the_loop_resumes_when_a_human_closes_the_pr(handles, canary) -> None:  # type: ignore[no-untyped-def]
    _mark_closed(handles, canary.id)
    assert _consulted(handles), "the loop stayed held after the PR was closed"


def test_a_decline_seen_by_the_loop_is_logged(handles, canary, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # OMNI-57's second criterion: the episodic log records the human rejection.
    store = _durable(monkeypatch, tmp_path)
    _mark_closed(handles, canary.id)
    assert _consulted(handles)
    outcomes = [(e.outcome, e.pr_id) for e in store.events()]
    assert outcomes == [("human_declined", canary.id)]


def test_a_pr_that_no_longer_exists_releases_its_hold(handles) -> None:  # type: ignore[no-untyped-def]
    # A remembered PR can outlive the PR (OMNI-126). PullRequestNotFound must
    # survive the trip through Ray as itself, or this would read as an outage.
    sm = handles["SelfModel"]
    ray.get(sm.set_slot.remote("green", "feature/gone@PR-999"))
    ray.get(sm.set_pending_pr.remote("PR-999"))

    result = ray.get(handles["DevOps"].observe_merge.remote("PR-999"))

    assert result["released"] is True and result["reason"] == "no longer exists"
    assert loop.canary_in_flight(_deployment(handles)) is None


# --- a restart still waits for the human (OMNI-126) -----------------------------


def test_a_restart_waits_for_the_last_runs_pr(handles, tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    # The second AWS run, replayed: a verified cycle leaves a PR, the process
    # ends, the next one starts. Before OMNI-126 it proposed the same change again.
    store = _durable(monkeypatch, tmp_path)
    result = org.run_cycle(handles, "Speed up divisor-sum", "Too slow; same results.")
    assert result["status"] == "verified_awaiting_human_merge"
    pr_id = str(result["pr_id"])
    assert store.load_state(org.PENDING_PR_KEY)["pr_id"] == pr_id

    _forget_in_memory(handles)                 # a new process knows nothing...
    capsys.readouterr()
    org.bootstrap()                            # ...the restart path itself...
    assert f"[sis] HOLDING: PR {pr_id}" in capsys.readouterr().err
    assert loop.pending_merge(_deployment(handles)) == pr_id   # ...restores it

    assert not _consulted(handles), "a restarted loop proposed while the PR was open"

    _mark_closed(handles, pr_id)               # the human declines it
    assert _consulted(handles), "the loop stayed held after the PR was closed"
    assert not store.load_state(org.PENDING_PR_KEY), "a decided PR is still remembered"


def test_a_pr_merged_offline_is_promoted_at_startup(handles, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _durable(monkeypatch, tmp_path)
    result = org.run_cycle(handles, "Speed up divisor-sum", "Too slow; same results.")
    pr_id = str(result["pr_id"])
    _forget_in_memory(handles)
    _mark_merged(handles, pr_id)               # merged while no process was running

    line = org.restore_pending_pr(handles, store, repo="o/r")

    assert line is not None and "was merged since the last run" in line
    deployment = _deployment(handles)
    assert deployment["live_version"] == result["canary"]["version"]
    assert loop.canary_in_flight(deployment) is None
    assert not store.load_state(org.PENDING_PR_KEY)


def test_a_single_run_starts_no_cycle_while_a_pr_awaits_a_human(handles, canary, capsys) -> None:  # type: ignore[no-untyped-def]
    # main.py without --loop: the other way the runbook's order produced #12.
    import main

    before = len(ray.get(handles["SelfModel"].provenance.remote()))
    main.run_org_cycle()

    assert "no cycle started" in capsys.readouterr().out
    kinds = [e["kind"] for e in ray.get(handles["SelfModel"].provenance.remote())[before:]]
    assert "pr" not in kinds and "story" not in kinds, "a cycle ran anyway"
    assert loop.pending_merge(_deployment(handles)) == canary.id


def test_restoring_never_takes_green_from_another_pr(handles, canary) -> None:  # type: ignore[no-untyped-def]
    # A surviving detached SelfModel knows better than a file does.
    outcome = ray.get(handles["DevOps"].adopt_pending.remote("PR-other", "x@PR-other"))
    assert outcome["adopted"] is False
    assert loop.pending_merge(_deployment(handles)) == canary.id


def test_a_pr_declined_offline_is_released_and_logged(handles, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _durable(monkeypatch, tmp_path)
    result = org.run_cycle(handles, "Speed up divisor-sum", "Too slow; same results.")
    pr_id = str(result["pr_id"])
    _forget_in_memory(handles)
    _mark_closed(handles, pr_id)               # declined while no process was running

    line = org.restore_pending_pr(handles, store, repo="o/r")

    assert line is not None and "closed without merging: hold released" in line
    assert loop.canary_in_flight(_deployment(handles)) is None
    declined = [e for e in store.events() if e.outcome == "human_declined"]
    assert [(e.pr_id, e.contract) for e in declined] == [(pr_id, result["contract"])]
    assert not store.load_state(org.PENDING_PR_KEY)


# --- the VCS knows what is open, whatever this process remembers (OMNI-136) ----


def _open_agent_pr(handles, story: str) -> str:  # type: ignore[no-untyped-def]
    """A loop PR open on the VCS that this process holds nothing for.

    How a rebuilt box sees the last box's PR, and how a feature PR that QA did
    not approve is left: open, with no hold anywhere.
    """
    ws = handles["Workspace"]
    branch = ray.get(ws.create_branch.remote(f"feature/{story}", "develop")).name
    title = f"Optimise sum_of_divisors: 1 step ({story.upper()})"
    return str(ray.get(ws.open_pr.remote(branch, title, "code", "runtime/target.py")).id)


def test_a_rebuilt_box_waits_for_a_pr_it_never_saw(handles, capsys) -> None:  # type: ignore[no-untyped-def]
    # AWS run #3, replayed: #13 open on testrun, nothing remembered, nothing
    # held. Before OMNI-136 the loop started a fresh feature and opened #14.
    pr_id = _open_agent_pr(handles, "tes-77")
    assert loop.pending_merge(_deployment(handles)) is None

    assert not _consulted(handles), "the loop proposed beside an open PR"
    assert f"[sis] HOLDING: PR {pr_id} is open" in capsys.readouterr().out
    assert loop.pending_merge(_deployment(handles)) == pr_id

    _mark_closed(handles, pr_id)
    assert _consulted(handles), "the loop stayed held after the PR was closed"


def test_the_loop_waits_until_every_open_pr_is_decided(handles) -> None:  # type: ignore[no-untyped-def]
    first, second = _open_agent_pr(handles, "tes-80"), _open_agent_pr(handles, "tes-83")

    assert not _consulted(handles)
    assert loop.pending_merge(_deployment(handles)) == first      # the oldest first

    _mark_merged(handles, first)
    assert not _consulted(handles), "the loop proposed while another PR was still open"
    assert loop.pending_merge(_deployment(handles)) == second

    _mark_closed(handles, second)
    assert _consulted(handles)


def test_a_pr_outside_the_loops_namespace_does_not_hold(handles) -> None:  # type: ignore[no-untyped-def]
    ws = handles["Workspace"]
    branch = ray.get(ws.create_branch.remote("docs/readme", "develop")).name
    pr = ray.get(ws.open_pr.remote(branch, "Fix README", "text", "README.md"))
    try:
        assert _consulted(handles), "a human's non-feature PR held the loop"
    finally:
        _mark_closed(handles, pr.id)


def test_a_single_run_starts_no_cycle_beside_an_open_pr(handles, capsys) -> None:  # type: ignore[no-untyped-def]
    import main

    pr_id = _open_agent_pr(handles, "tes-90")
    before = len(ray.get(handles["SelfModel"].provenance.remote()))
    try:
        main.run_org_cycle()
        out = capsys.readouterr().out
        assert "no cycle started" in out and f"PR {pr_id}" in out
        kinds = [e["kind"] for e in ray.get(handles["SelfModel"].provenance.remote())[before:]]
        assert "story" not in kinds, "a cycle ran beside the open PR"
    finally:
        _mark_closed(handles, pr_id)
        _forget_in_memory(handles)


def _durable(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    """Behave as the real adapters do (a PR outlives the process), in memory."""
    from sis.episodic import JsonlEpisodicStore

    store = JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    monkeypatch.setattr(org, "durable_vcs", lambda: "o/r")
    monkeypatch.setattr(org.episodic, "get_episodic_store", lambda *a, **k: store)
    return store


def _forget_in_memory(handles) -> None:  # type: ignore[no-untyped-def]
    """What a fresh process's SelfModel holds about the last run's PR: nothing."""
    ray.get(handles["SelfModel"].set_slot.remote("green", None))
    ray.get(handles["SelfModel"].set_pending_pr.remote(None))


def _consulted(handles) -> bool:  # type: ignore[no-untyped-def]
    """Whether a briefly-run loop.serve got as far as asking for new work."""
    import threading

    consulted: list[int] = []

    def _trigger():  # type: ignore[no-untyped-def]
        consulted.append(1)
        return None            # no work: keeps the check fast

    stop = threading.Event()
    threading.Timer(0.4, stop.set).start()
    loop.serve(handles, _trigger, interval_s=0.01, stop_event=stop)
    return bool(consulted)


def _mark_closed(handles, pr_id: str) -> None:  # type: ignore[no-untyped-def]
    """Simulate a human closing the PR without merging, as ``_mark_merged`` does."""
    ray.get(handles["Workspace"].__ray_call__.remote(
        lambda self, pid: self.vcs.simulate_human_close(pid), pr_id))


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
