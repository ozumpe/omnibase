"""End-to-end test of the actor org cycle (requires Ray, runs locally)."""

import pytest

ray = pytest.importorskip("ray")

from sis import org  # noqa: E402


@pytest.fixture(scope="module")
def handles():  # type: ignore[no-untyped-def]
    h = org.bootstrap()
    yield h
    ray.shutdown()


def test_full_cycle_reaches_human_merge_gate(handles) -> None:  # type: ignore[no-untyped-def]
    result = org.run_cycle(
        handles,
        "Speed up divisor-sum",
        "It is too slow; make it faster without changing results.",
    )
    # The stub proposer returns the known-good O(sqrt n) variant, so the
    # cycle should pass the gauntlet, QA, and stop at the human merge gate.
    assert result["status"] == "verified_awaiting_human_merge"
    assert result["candidate_latency"] < result["baseline_latency"]
    assert result["pr_id"] is not None

    # Canary deployed to the green slot, never promoted to live.
    assert result["canary"]["slot"] == "green"
    assert result["canary"]["live"] is False

    # Provenance graph captured the full chain.
    kinds = {e["kind"] for e in result["provenance"]}
    assert {"spec", "epic", "story", "pr", "canary", "outcome"} <= kinds

    # The candidate diff is fingerprinted (12-hex sha) so the episodic log can
    # dedupe/correlate proposals — the join key of the learning dataset.
    assert len(result["candidate_sha"]) == 12
    assert all(c in "0123456789abcdef" for c in result["candidate_sha"])


def test_self_model_registry_has_full_org(handles) -> None:  # type: ignore[no-untyped-def]
    roles = {info["role"] for info in ray.get(handles["SelfModel"].registry.remote())}
    assert {"CEO", "PM", "CTO", "Designer", "SWE", "QA", "DevOps"} <= roles


def test_story_done_after_qa(handles) -> None:  # type: ignore[no-untyped-def]
    result = org.run_cycle(handles, "Another speedup", "make it fast")
    from sis.ports import IssueStatus
    issue = ray.get(handles["Workspace"].get_issue.remote(result["story_id"]))
    assert issue.status == IssueStatus.DONE


def test_bootstrap_registers_the_target_contract(handles) -> None:  # type: ignore[no-untyped-def]
    # The SelfModel is the contract registry: it already knows what is deployed
    # where, so what each target is *judged by* belongs with it rather than in
    # an env var — and it is the same lookup the canary needs later to fetch a
    # PR's contract (docs/SERVE_CANARY.md step 10).
    from sis.contract import SUM_OF_DIVISORS

    spec = ray.get(handles["SelfModel"].contract_for.remote("runtime/target.py"))
    assert spec == SUM_OF_DIVISORS
    assert spec.entry == "sum_of_divisors"


def test_contract_registration_is_idempotent(handles) -> None:  # type: ignore[no-untyped-def]
    # bootstrap() is called repeatedly against a detached SelfModel that
    # survives restarts; re-registering must not accumulate duplicates.
    from sis.contract import SUM_OF_DIVISORS

    before = len(ray.get(handles["SelfModel"].contracts.remote()))
    ray.get(handles["SelfModel"].register_contract.remote(SUM_OF_DIVISORS))
    assert len(ray.get(handles["SelfModel"].contracts.remote())) == before


def test_an_unregistered_target_has_no_contract(handles) -> None:  # type: ignore[no-untyped-def]
    assert ray.get(handles["SelfModel"].contract_for.remote("runtime/nope.py")) is None


def test_full_cycle_against_the_second_contract(handles) -> None:  # type: ignore[no-untyped-def]
    # OMNI-7 acceptance: the whole actor cycle -- propose, gauntlet, QA re-run,
    # canary -- runs against a target the engine knows nothing about by name.
    # contract_name selects it; everything downstream is contract-driven.
    #
    # Passed as an ARGUMENT, not monkeypatch.setenv("SIS_CONTRACT"). The first
    # version of this test did the latter and passed while silently running
    # sum_of_divisors: the role actors are separate processes that inherit the
    # driver's env when created, so a var set afterwards never reaches them.
    result = org.run_cycle(
        handles, "Speed up the sort", "Bubble sort is too slow; same results, faster.",
        contract_name="sort")
    assert result["status"] == "verified_awaiting_human_merge", result.get("reason")
    assert result["candidate_latency"] < result["baseline_latency"]
    # Prove it really was the sort, not the default target passing by luck.
    pr = ray.get(handles["Workspace"].get_pr.remote(result["pr_id"]))
    assert "sort_numbers" in pr.artifact
    assert "sum_of_divisors" not in pr.artifact


def test_unknown_contract_name_fails_loudly(handles) -> None:  # type: ignore[no-untyped-def]
    # A typo'd contract name must not silently optimise a different target --
    # that would burn a cycle's spend and produce a baffling PR.
    with pytest.raises(Exception, match="not a registered contract"):
        ray.get(handles["SWE"].implement.remote("STORY-1", "not-a-real-contract"))
