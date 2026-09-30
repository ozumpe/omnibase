"""OMNI-147 end to end: the loop builds a Class-2 feature from its spec.

Its own module and a fresh cluster. ``roman`` is registered by bootstrap; the
variants below are registered by the tests themselves, each with its own
target path (the registry's key), so their features never meet.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

ray = pytest.importorskip("ray")

from sis import org  # noqa: E402
from sis.contract import ROMAN  # noqa: E402
from sis.paths import PROJECT_ROOT  # noqa: E402
from sis.policy import DEFAULT_TARGET_PATHS  # noqa: E402

_STUB = (PROJECT_ROOT / "runtime/candidates/roman.py").read_text(encoding="utf-8")
_BROKEN = _STUB.replace('return "".join(parts)', 'return "".join(parts).lower()')


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    return tmp_path_factory.mktemp("class2").resolve()


@pytest.fixture(scope="module")
def handles(workdir: pathlib.Path) -> Iterator[dict[str, Any]]:
    # The rebuilt variant's target must be SOFT for its PR to open; read by the
    # actors when they are created.
    before = os.environ.get("SIS_TARGET_PATHS")
    os.environ["SIS_TARGET_PATHS"] = ",".join(
        [*DEFAULT_TARGET_PATHS, str(workdir / "roman_broken.py")])
    ray.shutdown()
    try:
        yield org.bootstrap()
    finally:
        ray.shutdown()
        if before is None:
            os.environ.pop("SIS_TARGET_PATHS", None)
        else:
            os.environ["SIS_TARGET_PATHS"] = before


def _register(handles: dict[str, Any], workdir: pathlib.Path, name: str, *,
              built: str | None = None, answer: str = _STUB) -> str:
    """A copy of ``roman`` under *name*: its target file holds *built* (or does
    not exist), and the stub proposer answers *answer*."""
    target = workdir / f"{name}.py"
    if built is not None:
        target.write_text(built, encoding="utf-8")
    stub = workdir / f"{name}_stub.py"
    stub.write_text(answer, encoding="utf-8")
    ray.get(handles["SelfModel"].register_contract.remote(
        replace(ROMAN, name=name, entry_module=str(target), stub_candidate_path=str(stub))))
    return name


def test_a_feature_is_built_from_its_spec_and_proposed_in_one_pr(
    handles: dict[str, Any],
) -> None:
    sm, ws = handles["SelfModel"], handles["Workspace"]
    result = org.run_cycle(handles, "Roman numerals", "Convert to and from roman numerals.",
                           contract_name="roman")
    assert result["status"] == "verified_awaiting_human_merge", result.get("reason")
    assert result["contract"] == "roman"
    assert result["candidate_latency"] is None, "a built feature has no timings"
    pr = ray.get(ws.get_pr.remote(result["pr_id"], "runtime/roman.py"))
    assert pr.title.startswith("Build roman: 1 step"), pr.title
    assert pr.artifact == _STUB
    assert ray.get(sm.feature.remote("roman")) is None, "the next feature starts fresh"
    assert ray.get(sm.plan.remote("roman")) is None


def test_a_rejected_attempt_is_remembered_for_the_next_one(
    handles: dict[str, Any], workdir: pathlib.Path,
) -> None:
    # The feature has no branch until an attempt passes, and its reasons are
    # what the next prompt needs most.
    name = _register(handles, workdir, "roman_wrong", answer=_BROKEN)
    sm = handles["SelfModel"]

    first = org.run_cycle(handles, "Roman numerals", "As specified.", contract_name=name)
    assert first["status"] == "rolled_back"
    assert first["reason"] == "acceptance tests failed", first["reason"]
    feature = ray.get(sm.feature.remote(name))
    assert feature["steps"] == [] and feature["attempts"] == [
        "rejected: acceptance tests failed"]

    second = org.run_cycle(handles, "Roman numerals", "As specified.", contract_name=name)
    assert second["status"] == "rolled_back"
    assert second["story_id"] == first["story_id"], "one plan for the whole feature"
    assert len(ray.get(sm.feature.remote(name))["attempts"]) == 2


def _filed(ws: Any) -> int:
    return sum(1 for e in ray.get(ws.events.remote())
               if e.get("event") in ("issue.created", "page.created"))


def test_a_feature_already_built_is_the_target_converging(
    handles: dict[str, Any], workdir: pathlib.Path,
) -> None:
    name = _register(handles, workdir, "roman_built", built=_STUB)
    ws = handles["Workspace"]
    before = _filed(ws)
    result = org.run_cycle(handles, "Roman numerals", "As specified.", contract_name=name)
    assert result["status"] == "no_gain", result.get("reason")
    assert result["reason"].startswith("already built:")
    assert result["cost_usd"] == 0.0, "nothing was proposed"
    # OMNI-149 (L52): checked before planning, so nothing is filed for it.
    assert _filed(ws) == before, "a built feature's check filed pages or issues"
    assert ray.get(handles["SelfModel"].plan.remote(name)) is None
    # OMNI-150 (L53): no model was called, so none is recorded.
    assert result["model"] is None


def test_a_plan_made_for_a_built_feature_is_closed_not_left_in_progress(
    handles: dict[str, Any], workdir: pathlib.Path,
) -> None:
    # A plan can exist before the check sees the feature built (one made by
    # an earlier run, say). Its story is closed and the plan cleared (L52).
    from sis.ports import IssueStatus, IssueType

    name = _register(handles, workdir, "roman_planned", built=_STUB)
    ws, sm = handles["Workspace"], handles["SelfModel"]
    story = ray.get(ws.create_issue.remote(IssueType.STORY, "Implement: roman", None))
    ray.get(sm.set_plan.remote(name, {"spec_id": "SPEC-9", "epic_id": "EPIC-9",
                                      "feature_story_id": str(story.id)}))
    result = org.run_cycle(handles, "Roman numerals", "As specified.", contract_name=name)
    assert result["status"] == "no_gain" and result["story_id"] == str(story.id)
    assert ray.get(ws.get_issue.remote(str(story.id))).status == IssueStatus.DONE
    assert ray.get(sm.plan.remote(name)) is None


def test_a_built_feature_that_now_fails_its_spec_is_rebuilt(
    handles: dict[str, Any], workdir: pathlib.Path,
) -> None:
    # Its spec changed, say: the module that was built fails a gate now. The
    # reason is the first note of the rebuild, and the rebuild is proposed.
    name = _register(handles, workdir, "roman_broken", built=_BROKEN)
    result = org.run_cycle(handles, "Roman numerals", "As specified.", contract_name=name)
    assert result["status"] == "verified_awaiting_human_merge", result.get("reason")
    pr = ray.get(handles["Workspace"].get_pr.remote(result["pr_id"],
                                                    str(workdir / f"{name}.py")))
    assert pr.title.startswith(f"Build {name}: 1 step") and pr.artifact == _STUB


def test_the_serve_canary_is_refused_for_a_feature(handles: dict[str, Any]) -> None:
    with pytest.raises(RuntimeError, match="cannot judge contract 'roman'"):
        org.run_cycle(handles, "Roman numerals", "As specified.", contract_name="roman",
                      canary_backend="serve")
