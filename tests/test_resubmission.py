"""OMNI-127 (L46): a rejected candidate is remembered, and never judged twice.

Found in the second AWS run: the sort target had no headroom left, and two
cycles 44 s apart submitted byte-identical code. The second was paid for,
sandboxed, benchmarked and rejected for the same reason as the first, because
a Class-1 rejection with no feature in progress left nothing for the next
prompt to see.

Its own module and a fresh cluster. The contract is a copy of
``sum_of_divisors`` under its own name and target path (the registry's key),
whose stub proposer answers the same wrong module every time.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

ray = pytest.importorskip("ray")

from sis import org  # noqa: E402
from sis.contract import SUM_OF_DIVISORS  # noqa: E402
from sis.paths import PROJECT_ROOT  # noqa: E402

_STUB = (PROJECT_ROOT / SUM_OF_DIVISORS.stub_candidate_path).read_text(encoding="utf-8")
_WRONG = _STUB.replace("    return total\n", "    return total + 1\n", 1)


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    return tmp_path_factory.mktemp("resubmission").resolve()


@pytest.fixture(scope="module")
def handles() -> Iterator[dict[str, Any]]:
    ray.shutdown()
    try:
        yield org.bootstrap()
    finally:
        ray.shutdown()


def _register(handles: dict[str, Any], workdir: pathlib.Path, name: str) -> str:
    target = workdir / f"{name}.py"
    target.write_text((PROJECT_ROOT / SUM_OF_DIVISORS.target_path).read_text(encoding="utf-8"),
                      encoding="utf-8")
    stub = workdir / f"{name}_stub.py"
    stub.write_text(_WRONG, encoding="utf-8")
    ray.get(handles["SelfModel"].register_contract.remote(replace(
        SUM_OF_DIVISORS, name=name, target_path=str(target), stub_candidate_path=str(stub))))
    return name


def test_the_stub_answers_a_wrong_module() -> None:
    assert _WRONG != _STUB


def test_a_rejection_at_the_base_is_remembered_and_its_resubmission_not_rerun(
    handles: dict[str, Any], workdir: pathlib.Path,
) -> None:
    name = _register(handles, workdir, "sod_wrong")
    sm, ws = handles["SelfModel"], handles["Workspace"]

    first = org.run_cycle(handles, "Speed up divisor-sum", "Too slow; same results.",
                          contract_name=name)
    assert first["status"] == "rolled_back", first.get("reason")
    # No feature was in progress, and still the next prompt has the reason.
    feature = ray.get(sm.feature.remote(name))
    assert feature is not None and feature["steps"] == []
    assert feature["attempts"] == [f"rejected: {first['reason']}"]

    second = org.run_cycle(handles, "Speed up divisor-sum", "Too slow; same results.",
                           contract_name=name)
    assert second["status"] == "rolled_back"
    assert second["story_id"] == first["story_id"], "one plan for the whole feature"
    # The same code against the same baseline: the same verdict, not a second run.
    assert second["reason"].startswith(first["reason"])
    assert "resubmitted" in second["reason"]
    rerun = [e for e in ray.get(ws.events.remote()) if e.get("event") == "candidate.resubmitted"]
    assert len(rerun) == 1, rerun
    assert rerun[0]["candidate_sha"] == first["candidate_sha"] == second["candidate_sha"]
    attempts = ray.get(sm.feature.remote(name))["attempts"]
    assert len(attempts) == 2 and attempts[1].startswith("resubmitted")
