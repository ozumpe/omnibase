"""The live ("serve") canary backend (OMNI-14).

Own module for its own Ray cluster + Serve instance, like the other Serve
test modules. Exercises the real path: DevOps.canary(canary_backend="serve")
deploys behind a real Ray Serve router, fills the window with synthetic
loadgen traffic (nothing external calls the target yet), and evaluate_canary
decides — including the failure path, which the legacy in-memory backend
never had a way to exercise at all.
"""

import pathlib

import pytest

ray = pytest.importorskip("ray")

from sis import org  # noqa: E402
from sis.roles import LIVE_CANARY_REQUESTS  # noqa: E402


@pytest.fixture(scope="module")
def handles():  # type: ignore[no-untyped-def]
    h = org.bootstrap()
    yield h
    ray.shutdown()


def _post(args, url="http://127.0.0.1:8000/sort"):  # type: ignore[no-untyped-def]
    requests = pytest.importorskip("requests")
    return requests.post(url, json={"args": args}, timeout=10).json()


def _open_pr(handles, branch: str, artifact: str, contract_name: str = "sort"):  # type: ignore[no-untyped-def]
    """A PR whose contract is recorded, without running a whole cycle to get
    one -- lets the rejection/routing tests deploy a source of their choosing."""
    ws, sm = handles["Workspace"], handles["SelfModel"]
    name = ray.get(ws.create_branch.remote(branch, "develop")).name
    pr = ray.get(ws.open_pr.remote(name, "test PR", artifact))
    ray.get(sm.set_pr_contract.remote(pr.id, contract_name))
    return pr


# --- legacy backend: unaffected by default ---------------------------------


def test_the_legacy_backend_stays_the_default(handles) -> None:  # type: ignore[no-untyped-def]
    # No canary_backend argument, no SIS_CANARY -- the behaviour this whole
    # engine had before OMNI-14 must be bit-for-bit what a bare call gets.
    result = org.run_cycle(handles, "Legacy default check", "same results, faster.")
    assert result["status"] == "verified_awaiting_human_merge"
    assert result["canary"]["canary_passed"] is True
    assert "verdict" not in result["canary"]


def test_an_unrecognised_backend_value_is_treated_as_legacy(handles) -> None:  # type: ignore[no-untyped-def]
    # canary() only special-cases the literal "serve" -- anything else (a
    # typo, a future backend name not yet built) must fail toward the known-
    # safe path rather than toward undefined behaviour.
    pr = _open_pr(handles, "feature/typo-backend", "def sort_numbers(v):\n    return sorted(v)\n")
    result = ray.get(handles["DevOps"].canary.remote(pr.id, 0.001, "not-a-real-backend"))
    assert result["canary_passed"] is True
    assert "verdict" not in result


# --- live backend: the real flow --------------------------------------------


def test_a_passing_live_canary_reaches_the_human_merge_gate(handles) -> None:  # type: ignore[no-untyped-def]
    result = org.run_cycle(
        handles, "Speed up the sort (live)", "same results, faster please.",
        contract_name="sort", canary_backend="serve")

    assert result["status"] == "verified_awaiting_human_merge", result.get("reason")
    canary = result["canary"]
    assert canary["canary_passed"] is True
    assert canary["live"] is False and canary["slot"] == "green"

    verdict = canary["verdict"]
    assert verdict["passed"] is True
    assert verdict["samples"] == LIVE_CANARY_REQUESTS
    assert verdict["response_disagreements"] == 0
    # The whole point: a real percentile comparison from real dispatched
    # traffic, not a number copied out of the offline benchmark.
    assert verdict["candidate_p95"] > 0 and verdict["baseline_p95"] > 0


def test_a_disagreeing_candidate_is_rejected_rolled_back_and_reported(handles) -> None:  # type: ignore[no-untyped-def]
    # A hand-crafted bad PR rather than the stub proposer, which always writes
    # the known-good variant -- the only way to exercise a live REJECTION.
    pr = _open_pr(handles, "feature/bad-sort",
                  "def sort_numbers(values):\n    return list(reversed(sorted(values)))\n")

    result = ray.get(handles["DevOps"].canary.remote(pr.id, 0.0001, "serve"))

    assert result["canary_passed"] is False
    assert "disagreement" in result["reason"]
    assert result["bug_id"] is not None

    # The release half actually ran: green is free, not left dangling with a
    # rejected candidate still occupying it.
    deployment = ray.get(handles["SelfModel"].deployment.remote())
    assert deployment["slots"]["green"] is None
    assert deployment["pending_pr"] is None

    # A durable artifact, not just a return value nobody reads.
    issue = ray.get(handles["Workspace"].get_issue.remote(result["bug_id"]))
    assert "Live canary rejected" in issue.summary
    assert pr.id in issue.summary


def test_cycle_outcome_turns_a_live_rejection_into_canary_rejected() -> None:
    # org.cycle_outcome is the pure fold DevOps.canary()'s result goes
    # through in run_cycle -- the actual result shape DevOps.canary() returns
    # on a live rejection (see the test above), fed through the real function
    # rather than logic re-derived by hand in the test.
    from sis.org import cycle_outcome

    status, success, reason = cycle_outcome(
        approved=True,
        canary={"canary_passed": False, "reason": "response disagreement: 143 of 150 ..."})
    assert status == "canary_rejected"
    assert success is False
    assert reason == "response disagreement: 143 of 150 ..."


def test_cycle_outcome_matches_pre_omni14_behaviour_without_a_live_verdict() -> None:
    # canary=None (QA rejected, no canary ran) and the legacy backend's dict
    # (no "canary_passed" key at all) must both reduce to "QA alone decides" —
    # the exact behaviour every cycle had before this story.
    from sis.org import cycle_outcome

    assert cycle_outcome(approved=False, canary=None) == ("qa_rejected", False, None)
    assert cycle_outcome(approved=True, canary=None) == (
        "verified_awaiting_human_merge", True, None)
    assert cycle_outcome(approved=True, canary={"version": "v1", "slot": "green"}) == (
        "verified_awaiting_human_merge", True, None)


def test_observe_merge_promotes_the_real_serve_deployment(handles) -> None:  # type: ignore[no-untyped-def]
    # The end-to-end close: promotion through the live backend must actually
    # redeploy blue, not just flip a bookkeeping flag nobody is looking at.
    result = org.run_cycle(
        handles, "Speed up the sort (promote)", "same results, faster please.",
        contract_name="sort", canary_backend="serve")
    assert result["status"] == "verified_awaiting_human_merge", result.get("reason")
    pr_id = result["pr_id"]

    before = _post([[3, 1, 2]])
    assert before["result"] == [1, 2, 3]

    ray.get(handles["Workspace"].__ray_call__.remote(
        lambda self, pid: self.vcs.simulate_human_merge(pid), pr_id))
    outcome = ray.get(handles["DevOps"].observe_merge.remote(pr_id))

    assert outcome["merged"] is True and outcome["promoted"] is True
    after = _post([[3, 1, 2]])
    assert after["result"] == [1, 2, 3]  # still correct...
    assert after["version"] == outcome["version"]  # ...and now serving the candidate

    deployment = ray.get(handles["SelfModel"].deployment.remote())
    assert deployment["slots"]["green"] is None
    assert deployment["live_version"] == outcome["version"]


def test_retire_canary_routes_a_live_pr_through_its_own_backend(handles) -> None:  # type: ignore[no-untyped-def]
    # The manual-release path must also know to route through ServeCloud for a
    # live-backed PR, not just the automatic rejection path inside
    # _canary_live -- regardless of whether the deploy it's releasing passed.
    #
    # Deliberately does NOT assert canary_passed. Earlier versions tried to
    # engineer a guaranteed pass by picking a candidate with "real" offline
    # margin (sort, then sum_of_divisors' O(sqrt n) vs O(n)) -- both still hit
    # this project's own known noise floor (L5, docs/KNOWN_ISSUES.md), just
    # from a different direction: sum_of_divisors' actual compute time
    # (microseconds) is dwarfed by real Ray Serve dispatch overhead (tens of
    # milliseconds per request), so even a genuine 100x algorithmic advantage
    # doesn't reliably clear evaluate_canary's live p95 gate. There is no
    # candidate that reliably passes here, so this test doesn't require one --
    # it asserts what it actually cares about (retire_canary's routing), which
    # holds identically whether _canary_live already auto-retired on a failed
    # verdict or this call is the first release.
    from sis.contract import SUM_OF_DIVISORS

    candidate = pathlib.Path(
        str(SUM_OF_DIVISORS.stub_candidate_path)).read_text(encoding="utf-8")
    pr = _open_pr(handles, "feature/manual-release", candidate,
                  contract_name="sum_of_divisors")
    deployed = ray.get(handles["DevOps"].canary.remote(pr.id, 0.001, "serve"))

    ray.get(handles["DevOps"].retire_canary.remote(deployed["version"], pr.id))

    deployment = ray.get(handles["SelfModel"].deployment.remote())
    assert deployment["slots"]["green"] is None


def test_the_servecloud_is_reused_across_cycles_for_one_contract(handles) -> None:  # type: ignore[no-untyped-def]
    # _cloud_for() caches per contract name -- a second construction would
    # call serve_blue() again and, per OMNI-13's finding, needlessly cycle a
    # replica the first call already stood up correctly. Checked via the
    # audit trail rather than a construction-identity trick (blue's source is
    # always the contract's committed file on first build, not something a
    # caller can parametrise): a rebuild shows up as a second
    # "serve.blue_deployed" telemetry event for the same app. This also
    # exercises _RemoteTelemetry -- if it silently dropped events instead of
    # forwarding them, this test would pass for the wrong reason, so the
    # "before" count matters as much as the "after" one.
    from sis.contract import SUM_OF_DIVISORS
    from sis.serving import app_name

    app = app_name(SUM_OF_DIVISORS, "canary")

    def blue_deploys() -> int:
        events = ray.get(handles["Workspace"].events.remote())
        return sum(1 for e in events
                   if e["event"] == "serve.blue_deployed" and e.get("app") == app)

    before = blue_deploys()
    assert before > 0, "sum_of_divisors' ServeCloud should already exist by this point"

    # canary_passed is NOT asserted here -- see the previous test for why a
    # live sum_of_divisors comparison isn't a reliable pass/fail signal
    # (dispatch overhead dwarfs the function's own compute time). Caching
    # happens in _cloud_for(), before evaluate_canary ever runs, so the
    # property under test holds regardless of the verdict.
    candidate = pathlib.Path(
        str(SUM_OF_DIVISORS.stub_candidate_path)).read_text(encoding="utf-8")
    pr = _open_pr(handles, "feature/cache-check", candidate, contract_name="sum_of_divisors")
    ray.get(handles["DevOps"].canary.remote(pr.id, 0.001, "serve"))

    assert blue_deploys() == before, "canary() rebuilt an already-cached ServeCloud"
