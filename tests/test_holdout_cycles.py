"""OMNI-207 end to end: a cycle counts against the holdout budget, and a spent
budget refuses the next cycle before any spend.

Its own module and a fresh cluster. A copy of ``roman`` with one public
held-out episode and a budget of one: the first cycle is judged on it and
uses the budget; the second is refused.
"""

from __future__ import annotations

import json
import os
import pathlib
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

ray = pytest.importorskip("ray")

from sis import episodic, holdout, org  # noqa: E402
from sis.backtest import FIXTURE_SCHEMA, Backtest  # noqa: E402
from sis.contract import ROMAN  # noqa: E402
from sis.paths import PROJECT_ROOT  # noqa: E402
from sis.policy import DEFAULT_TARGET_PATHS  # noqa: E402

_NAME = "roman_holdout"


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    return tmp_path_factory.mktemp("holdout").resolve()


@pytest.fixture(scope="module")
def handles(workdir: pathlib.Path) -> Iterator[dict[str, Any]]:
    # The variant's target must be SOFT for its PR to open; read by the actors
    # when they are created.
    before = os.environ.get("SIS_TARGET_PATHS")
    os.environ["SIS_TARGET_PATHS"] = ",".join(
        [*DEFAULT_TARGET_PATHS, str(workdir / "roman_holdout.py")])
    ray.shutdown()
    try:
        yield org.bootstrap()
    finally:
        ray.shutdown()
        if before is None:
            os.environ.pop("SIS_TARGET_PATHS", None)
        else:
            os.environ["SIS_TARGET_PATHS"] = before


@pytest.fixture
def store(workdir: pathlib.Path,
          monkeypatch: pytest.MonkeyPatch) -> episodic.JsonlEpisodicStore:
    """The driver's episodic log, on a tmp path: the count reads it."""
    log = episodic.JsonlEpisodicStore(workdir / "episodic.jsonl")
    monkeypatch.setattr(episodic, "get_episodic_store", lambda kind=None: log)
    monkeypatch.setattr(holdout, "HISTORY_DIR", workdir / "history")
    return log


@pytest.fixture(scope="module")
def contract(handles: dict[str, Any], workdir: pathlib.Path) -> Any:
    fixture, expect = workdir / "w1.json", workdir / "w1.expect.json"
    fixture.write_text(json.dumps({"schema": FIXTURE_SCHEMA, "args": [4]}), encoding="utf-8")
    expect.write_text(json.dumps({"schema": FIXTURE_SCHEMA, "value": "IV"}), encoding="utf-8")
    stub = workdir / "stub.py"
    stub.write_text((PROJECT_ROOT / "runtime/candidates/roman.py").read_text(encoding="utf-8"),
                    encoding="utf-8")
    spec = replace(ROMAN, name=_NAME, entry_module=str(workdir / "roman_holdout.py"),
                   stub_candidate_path=str(stub), holdout_budget=1,
                   backtests=(Backtest(name="w1", fixture=str(fixture), expect=str(expect)),))
    ray.get(handles["SelfModel"].register_contract.remote(spec))
    return spec


def test_a_judged_cycle_uses_the_budget_and_the_next_is_refused_before_spend(
    handles: dict[str, Any], contract: Any, store: episodic.JsonlEpisodicStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = holdout.fixture_digest(contract.backtests)
    first = org.run_cycle(handles, "Roman numerals", "Build it.", contract_name=_NAME)
    assert first["status"] == "verified_awaiting_human_merge", first.get("reason")
    assert first["holdout"] == {"contract": _NAME, "digest": digest, "limit": 1,
                                "used": 1, "remaining": 0}
    assert [e.holdout_digest for e in store.events()] == [digest]

    pages: list[tuple[str, str]] = []
    real_page = org.page

    def _page(ws: Any, st: Any, severity: Any, title: str, body: str) -> dict[str, str]:
        pages.append((title, body))
        return real_page(ws, st, severity, title, body)

    monkeypatch.setattr(org, "page", _page)
    ceo = handles["CEO"]
    before = ray.get(ceo.economics.remote())
    second = org.run_cycle(handles, "Roman numerals", "Build it.", contract_name=_NAME)
    assert second["status"] == holdout.HOLDOUT_SPENT
    assert second["contract"] == _NAME
    after = ray.get(ceo.economics.remote())
    assert after["spent_usd"] == before["spent_usd"], "nothing reserved or spent"
    assert not ray.get(ceo.breaker_open.remote()), "neutral: no breaker count"
    assert [t for t, _ in pages] == [f"holdout budget spent: {_NAME}"]
    assert "1 of 1" in pages[0][1]
    # The refusal is on the record, and is not itself a judgement.
    assert [e.outcome for e in store.events()][-1] == holdout.HOLDOUT_SPENT
    assert holdout.spent(store.events(), _NAME, str(digest)) == 1
