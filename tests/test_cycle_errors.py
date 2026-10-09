"""OMNI-55 (KNOWN_ISSUES M16, L30): an exception inside a cycle is an outcome.

Before, anything that raised after the proposal was paid for lost that cost:
it reached the driver only in ``SWE.implement``'s return value. The CEO never
saw it, the episodic log had no row, no bug was filed, the breaker did not
count, and ``--loop`` died. The documented case was an under-scoped token's
403 at ``open_pr``.

Its own module, so it gets its own Ray cluster and its own CEO (needs Ray).
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ray = pytest.importorskip("ray")

from sis import episodic, loop, org  # noqa: E402
from sis.episodic import gate_from_reason, neutral_status  # noqa: E402

# Small, so the module's total stays under brakes.slo_min_spend_usd: above it
# the cost-per-accepted brake would open the breaker whatever a test does.
PROPOSAL_COST = 0.05


# --- pure ---------------------------------------------------------------------


def test_an_error_is_its_own_gate_whatever_its_message_says() -> None:
    # The message is the exception's, so it can hold any word a gate's reason
    # is recognised by. This one is not a gate that timed out.
    reason = "error: ReadTimeout: the read timed out (in the SWE's step, after the proposal)"
    assert gate_from_reason(reason) == "error"
    assert gate_from_reason("error: RuntimeError: no improvement possible, policy says") == "error"
    # Counted in full: never neutral.
    assert neutral_status(reason) is None


def test_the_console_line_says_what_the_error_was_once() -> None:
    line = org.cycle_summary({
        "status": "error", "reason": "error: PermissionError: 403 Forbidden",
        "cost_usd": 0.25, "economics": {"spent_usd": 0.25, "budget_usd": 1.0}})
    assert line == ("[cycle] error: PermissionError: 403 Forbidden "
                    "(cost $0.2500; spent $0.2500 of $1.00)")


# --- with the real roles ----------------------------------------------------------


@pytest.fixture(scope="module")
def handles():  # type: ignore[no-untyped-def]
    h = org.bootstrap()
    yield h
    ray.shutdown()


@pytest.fixture(autouse=True)
def _a_clean_streak(request: pytest.FixtureRequest) -> None:
    # Each test counts its own failures; three in a row would open the breaker.
    if "handles" in request.fixturenames:
        ray.get(request.getfixturevalue("handles")["CEO"].reset_breaker.remote())


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> episodic.JsonlEpisodicStore:
    # The suite runs on the no-op store; these tests are about what is written.
    real = episodic.JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    monkeypatch.setattr(episodic, "get_episodic_store", lambda: real)
    return real


def _ceo(handles: dict[str, Any]) -> tuple[float, float]:
    state = ray.get(handles["CEO"].state_snapshot.remote())
    return float(state["spent_usd"]), float(state["consecutive_failures"])


def _proposals_cost_money(_swe: Any) -> None:
    """Inside the SWE's process: the stub's proposal now costs PROPOSAL_COST."""
    from sis import proposer

    real, calls = proposer.propose, []

    def paid(*args: Any, **kwargs: Any) -> str:
        candidate = real(*args, **kwargs)
        proposer._last_cost_usd = 0.05  # PROPOSAL_COST
        proposer._last_model = "paid-test-model"
        calls.append(candidate)
        return candidate

    proposer.propose = paid  # type: ignore[assignment]
    proposer._calls = calls  # type: ignore[attr-defined]


def _proposals_made(_swe: Any) -> int:
    from sis import proposer

    return len(proposer._calls)  # type: ignore[attr-defined]


def _open_pr_is_forbidden(workspace: Any) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("403 Forbidden: Resource not accessible by personal access token")

    workspace.vcs._open_pr, workspace.vcs.open_pr = workspace.vcs.open_pr, forbidden


def _open_pr_works_again(workspace: Any) -> None:
    workspace.vcs.open_pr = workspace.vcs._open_pr


def test_a_403_at_open_pr_still_charges_the_proposal_and_counts_the_failure(
    handles: dict[str, Any], store: episodic.JsonlEpisodicStore,
) -> None:
    ray.get(handles["SWE"].__ray_call__.remote(_proposals_cost_money))
    ray.get(handles["Workspace"].__ray_call__.remote(_open_pr_is_forbidden))
    spent, streak = _ceo(handles)
    try:
        result = org.run_cycle(handles, "speed it up", "make it fast")
    finally:
        ray.get(handles["Workspace"].__ray_call__.remote(_open_pr_works_again))

    # An outcome, not a crash, and it says what happened.
    assert result["status"] == "error"
    # The exception that was raised, not Ray's wrapper around it.
    assert result["reason"].startswith("error: PermissionError: 403 Forbidden"), result["reason"]
    # The proposal's cost reached the CEO, and the failure counts.
    assert result["cost_usd"] == PROPOSAL_COST
    assert _ceo(handles) == (pytest.approx(spent + PROPOSAL_COST), streak + 1)
    assert result["economics"]["spent_usd"] == pytest.approx(spent + PROPOSAL_COST)
    # In the episodic log, with its cost, its gate and the model that was paid.
    (event,) = store.events()
    assert (event.outcome, event.reject_gate, event.cost_usd) == ("error", "error", PROPOSAL_COST)
    assert event.model == "paid-test-model"
    assert store.load_state("ceo")["spent_usd"] == pytest.approx(spent + PROPOSAL_COST)
    # A bug, and a page: it is not the candidate's doing and will likely repeat.
    bug = ray.get(handles["Workspace"].get_issue.remote(result["bug_id"]))
    assert "403 Forbidden" in bug.summary
    pages = [e for e in ray.get(handles["Workspace"].events.remote())
             if e["event"] == "notify.sent" and str(e["title"]).startswith("cycle error")]
    assert len(pages) == 1 and pages[0]["severity"] == "warning"

    # The step it had committed is not stranded: the next cycle opens its PR,
    # without paying for another proposal.
    follow_up = org.run_cycle(handles, "speed it up", "make it fast")
    assert follow_up["status"] == "verified_awaiting_human_merge", follow_up.get("reason")
    assert follow_up["pr_id"] and follow_up["cost_usd"] == 0.0
    assert ray.get(handles["SWE"].__ray_call__.remote(_proposals_made)) == 1
    assert _ceo(handles) == (pytest.approx(spent + PROPOSAL_COST), 0.0)


# --- the driver, with roles that fail on demand --------------------------------------


def _raises(message: str) -> SimpleNamespace:
    def remote(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(message)

    return SimpleNamespace(remote=remote)


def _returns(value: Any) -> SimpleNamespace:
    return SimpleNamespace(remote=lambda *args, **kwargs: ray.put(value))


def _swe(implement: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(already_built=_returns(None), implement=implement)


_PASSED: dict[str, Any] = {
    "passed": True, "pr_id": "PR-77", "cost_usd": 0.03, "candidate_sha": "feedface0123",
    "baseline": 0.002, "candidate_latency": 0.001, "contract": "sum_of_divisors",
    "model": "paid-test-model",
}


def test_an_error_before_any_spend_is_recorded_and_counted_too(handles: dict[str, Any]) -> None:
    spent, streak = _ceo(handles)
    faked = {**handles, "SWE": _swe(_raises("the SWE actor died"))}
    result = org.run_cycle(faked, "t", "b")
    assert result["status"] == "error" and result["cost_usd"] == 0.0
    assert result["reason"] == "error: RuntimeError: the SWE actor died"
    assert result["bug_id"] is not None and result["story_id"]
    assert _ceo(handles) == (pytest.approx(spent), streak + 1)


def test_an_error_after_the_step_returned_still_charges_its_cost(
    handles: dict[str, Any], store: episodic.JsonlEpisodicStore,
) -> None:
    spent, streak = _ceo(handles)
    faked = {**handles, "SWE": _swe(_returns(_PASSED)),
             "QA": SimpleNamespace(review=_raises("500 from the work tracker"))}
    result = org.run_cycle(faked, "t", "b")
    assert (result["status"], result["cost_usd"], result["pr_id"]) == ("error", 0.03, "PR-77")
    assert _ceo(handles) == (pytest.approx(spent + 0.03), streak + 1)
    (event,) = store.events()
    assert (event.outcome, event.cost_usd, event.pr_id) == ("error", 0.03, "PR-77")


def test_a_canary_set_by_a_cycle_that_then_fails_is_retired(handles: dict[str, Any]) -> None:
    retired: list[tuple[Any, ...]] = []

    def retire(*args: Any) -> Any:
        retired.append(args)
        return ray.put({"retired": True})

    devops = SimpleNamespace(
        canary=_returns({"version": "v-PR-77", "slot": "green", "canary_passed": True}),
        retire_canary=SimpleNamespace(remote=retire),
        file_bug=handles["DevOps"].file_bug)
    faked = {**handles, "SWE": _swe(_returns(_PASSED)),
             "QA": SimpleNamespace(review=_returns((True, None))),
             "DevOps": devops, "PM": SimpleNamespace(
                 refine_proposal=handles["PM"].refine_proposal,
                 accept=_raises("the document store is down"))}
    spent, streak = _ceo(handles)
    result = org.run_cycle(faked, "t", "b")
    assert result["status"] == "error"
    # Green is not left attached to a cycle that was counted as failed (L30).
    assert retired == [("v-PR-77", "PR-77")] and result["canary_retired"] == "v-PR-77"
    assert _ceo(handles) == (pytest.approx(spent + 0.03), streak + 1)


def test_a_bug_that_cannot_be_filed_does_not_cost_the_cycle_its_verdict(
    handles: dict[str, Any],
) -> None:
    rejected = {"passed": False, "pr_id": None, "cost_usd": 0.01, "candidate_sha": "deadbeef0123",
                "reason": "no improvement: candidate 0.000200s vs baseline 0.000100s"}
    faked = {**handles, "SWE": _swe(_returns(rejected)),
             "DevOps": SimpleNamespace(file_bug=_raises("500 from the work tracker"))}
    spent, streak = _ceo(handles)
    result = org.run_cycle(faked, "t", "b")
    # The gate's verdict stands, charged once; only the bug is missing.
    assert (result["status"], result["bug_id"]) == ("rolled_back", None)
    assert result["reason"].startswith("no improvement")
    assert _ceo(handles) == (pytest.approx(spent + 0.01), streak + 1)


def test_a_ceo_that_cannot_be_told_stops_the_loop(handles: dict[str, Any]) -> None:
    # The one error that is not turned into an outcome: without its brakes the
    # loop must not go on.
    real = handles["CEO"]
    deaf = SimpleNamespace(
        pause_reason=real.pause_reason, breaker_open=real.breaker_open,
        approve_budget=real.approve_budget, economics=real.economics,
        state_snapshot=real.state_snapshot, record_neutral=real.record_neutral,
        report_outcome=_raises("the CEO actor died"))
    faked = {**handles, "CEO": deaf, "SWE": _swe(_raises("a 500"))}
    with pytest.raises(RuntimeError, match="the CEO actor died"):
        org.run_cycle(faked, "t", "b")


def test_the_loop_goes_on_after_a_cycle_that_raised(handles: dict[str, Any]) -> None:
    outcomes = iter([_raises("a transient 500"), _returns(_PASSED)])

    def implement(*args: Any, **kwargs: Any) -> Any:
        return next(outcomes).remote(*args, **kwargs)

    faked = {**handles, "SWE": _swe(SimpleNamespace(remote=implement)),
             "QA": SimpleNamespace(review=_returns((False, "benchmark inconclusive: noise")))}
    results = loop.run_loop(
        lambda: loop.Tick(breaker_open=False, budget_ok=True, work=loop.Work("t", "b")),
        lambda work: org.run_cycle(faked, work.title, work.body),
        interval_s=0.0, max_cycles=2, sleep=lambda _seconds: None)
    # The second cycle ran, and to its own end: QA's neutral verdict.
    assert [r["status"] for r in results] == ["error", "inconclusive"]
