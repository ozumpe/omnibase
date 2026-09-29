"""OMNI-130 end to end: steps build on a feature branch; one PR when it is finished.

Its own module and a fresh cluster, with three steps per feature set before
the actors start (the rest of the suite runs one-step features).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

ray = pytest.importorskip("ray")

from sis import org  # noqa: E402


@pytest.fixture(scope="module")
def handles() -> Iterator[dict[str, Any]]:
    before = os.environ.get("SIS_FEATURE_MAX_STEPS")
    os.environ["SIS_FEATURE_MAX_STEPS"] = "3"   # read by the actors when created
    ray.shutdown()
    try:
        yield org.bootstrap()
    finally:
        ray.shutdown()
        if before is None:
            os.environ.pop("SIS_FEATURE_MAX_STEPS", None)
        else:
            os.environ["SIS_FEATURE_MAX_STEPS"] = before


def test_steps_build_on_the_feature_and_one_pr_follows(handles: dict[str, Any]) -> None:
    sm, ws = handles["SelfModel"], handles["Workspace"]

    first = org.run_cycle(handles, "Speed up divisor-sum", "Too slow; same results.")
    assert first["status"] == "feature_step" and first["step"] == 1
    assert first.get("pr_id") is None, "a step opens no PR"
    branch = first["branch"]
    feature = ray.get(sm.feature.remote(first["contract"]))
    assert feature["branch"] == branch and len(feature["steps"]) == 1
    committed = ray.get(ws.read_file.remote(branch, "runtime/target.py"))
    assert committed, "the step was not committed to its branch"

    # The stub proposes the same module again: no further gain, so the
    # feature is finished and its PR carries the head, not the rejected step.
    second = org.run_cycle(handles, "Speed up divisor-sum", "Too slow; same results.")
    assert second["status"] == "verified_awaiting_human_merge", second.get("reason")
    pr = ray.get(ws.get_pr.remote(second["pr_id"], "runtime/target.py"))
    assert pr.branch == branch and pr.artifact == committed
    assert pr.title.startswith("Optimise sum_of_divisors: 1 step")
    assert ray.get(sm.feature.remote(first["contract"])) is None, "the next feature starts fresh"


def _epics(ws: Any) -> int:
    return sum(1 for e in ray.get(ws.events.remote())
               if e.get("event") == "issue.created" and e.get("type") == "Epic")


def test_a_feature_is_planned_once_and_carried_on_after_a_restart(
    handles: dict[str, Any],
) -> None:
    # OMNI-135. One plan per feature: its steps share one spec, epic and story.
    # And a restarted process finds the half-built feature on its branch.
    sm, ws = handles["SelfModel"], handles["Workspace"]
    epics_before = _epics(ws)

    first = org.run_cycle(handles, "Speed up divisor-sum", "Too slow; same results.")
    assert first["status"] == "feature_step", first.get("reason")
    contract_name, branch = first["contract"], first["branch"]

    # What a new process on a new box knows: nothing in memory. The branch
    # and its commits are all that is left.
    ray.get(sm.set_feature.remote(contract_name, None))
    ray.get(sm.set_plan.remote(contract_name, None))

    second = org.run_cycle(handles, "Speed up divisor-sum", "Too slow; same results.")

    assert second["status"] == "verified_awaiting_human_merge", second.get("reason")
    assert (second["story_id"], second["spec_id"]) == (first["story_id"], first["spec_id"])
    assert _epics(ws) == epics_before + 1, "a feature files one epic, not one per step"
    pr = ray.get(ws.get_pr.remote(second["pr_id"], None))
    assert pr.branch == branch, "the PR is for the resumed branch, not a new one"
    assert pr.title == f"Optimise {contract_name}: 1 step ({first['story_id']})"
    assert any(e["kind"] == "feature_resumed" and e["ref"] == branch
               for e in second["provenance"])

    # The PR opened, so the next feature is planned afresh.
    third = org.run_cycle(handles, "Speed up divisor-sum", "Too slow; same results.")
    assert third["story_id"] != first["story_id"]
    assert _epics(ws) == epics_before + 2
