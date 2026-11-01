"""The ``ca_price_wholesale`` contract (OMNI-163): a forecast that must beat persistence.

Its real fixture is private (the LA spot price may not be redistributed), so
it is not in CI. These tests build small private fixtures of their own and
run the whole gauntlet on them: the stub model passes, persistence passes
every per-week bound and still fails, because it does not beat itself.
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
import json
import math
import pathlib
from typing import Any

import pytest

from sis import gauntlet, policy
from sis.backtest import FIXTURE_SCHEMA, PRIVATE_INDEX
from sis.contract import (
    CA_PRICE_WHOLESALE,
    CA_PRICE_WHOLESALE_INDEX,
    FEATURE_CONTRACTS,
    ca_price_wholesale,
)
from sis.paths import PROJECT_ROOT

CA = "PET.EMM_EPMRR_PTE_SCA_DPG.W"
SPOT = "PET.EER_EPMRR_PF4_Y05LA_DPG.D"
STUB = PROJECT_ROOT / "runtime" / "candidates" / "ca_price_wholesale.py"
PERSISTENCE = PROJECT_ROOT / "runtime" / "candidates" / "ca_price_persistence.py"


def _stub() -> Any:
    spec = importlib.util.spec_from_file_location("ca_price_wholesale_stub", STUB)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reading(series: str, day: datetime.date, value: float) -> dict[str, Any]:
    return {"source": "eia", "series": series, "value": round(value, 3),
            "unit": "Dollars per Gallon", "event_time": f"{day.isoformat()}T00:00:00-04:00",
            "known_at": f"{day.isoformat()}T23:00:00-04:00"}


def _state(monday: datetime.date) -> dict[str, Any]:
    """A year of a synthetic market to *monday*: spot swings, retail follows it late."""
    def spot(day: datetime.date) -> float:
        n = day.toordinal()
        return 2.5 + 0.4 * math.sin(n / 23.0) + 0.1 * math.sin(n / 3.1)

    days = [monday - datetime.timedelta(days=n) for n in range(371)]
    weeks = [monday - datetime.timedelta(weeks=n) for n in range(53)]
    readings = [_reading(CA, week, 1.9 + spot(week - datetime.timedelta(days=10)))
                for week in weeks]
    readings += [_reading(SPOT, day, spot(day)) for day in days if day.weekday() < 5]
    return {"as_of": f"{(monday + datetime.timedelta(days=3)).isoformat()}T17:00:00-04:00",
            "readings": readings, "absent": []}


def _fixture(root: pathlib.Path) -> str:
    """Three private episodes whose answer is the stub's forecast; returns the index."""
    model = _stub()
    episodes = []
    for monday in (datetime.date(2022, 9, 5), datetime.date(2022, 9, 26),
                   datetime.date(2022, 10, 17)):
        state = _state(monday)
        answer = model.predict_next_week(state)
        name = monday.isoformat()
        files = {"fixture": json.dumps({"schema": FIXTURE_SCHEMA, "event_time": state["as_of"],
                                        "args": [state]}),
                 "expect": json.dumps({"schema": FIXTURE_SCHEMA, "value": answer})}
        entry: dict[str, str] = {"name": name}
        for key, text in files.items():
            relative = f"private/backtests/{name}_{key}.json"
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(text, encoding="utf-8")
            entry[key] = relative
            entry[f"{key}_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        episodes.append(entry)
    (root / PRIVATE_INDEX).write_text(json.dumps({"schema": 1, "episodes": episodes}),
                                      encoding="utf-8")
    return str(root / PRIVATE_INDEX)


def test_the_contract_waits_for_its_promoted_fixture() -> None:
    # No private.json in git yet: nothing to judge it on, so not registered.
    assert not (PROJECT_ROOT / CA_PRICE_WHOLESALE_INDEX).exists()
    assert CA_PRICE_WHOLESALE is None
    assert all(c.name != "ca_price_wholesale" for c in FEATURE_CONTRACTS)


def _malformed(entry: dict[str, Any]) -> list[tuple[bytes, str]]:
    """Index bodies a promotion could leave broken, each with what the refusal names."""
    def index(*episodes: object) -> bytes:
        return json.dumps({"schema": 1, "episodes": list(episodes)}).encode()

    def without(key: str) -> dict[str, Any]:
        return {k: v for k, v in entry.items() if k != key}

    return [
        (index(entry)[:40], "cannot be read as JSON"),                 # truncated
        (b"\xff\xfe" + index(entry), "cannot be read as JSON"),        # not UTF-8
        (index(entry, ["private/a.json"]), "episode 1 is not an object"),
        (index(without("expect")), "episode 0 has no string expect"),
        (index(without("name"), entry), "episode 0 has no string name"),
        (index({**entry, "fixture_sha256": 7}), "episode 0 has no string fixture_sha256"),
        (index({**entry, "fixture": None}), "episode 0 has no string fixture"),
    ]


def test_a_malformed_index_is_refused_naming_the_file_and_the_episode(
        tmp_path: pathlib.Path) -> None:
    # OMNI-176 (L61): these raised KeyError, TypeError or JSONDecodeError
    # rather than a ValueError that says which file and which episode.
    from sis.backtest import private_backtests

    good = json.loads((pathlib.Path(_fixture(tmp_path))).read_text(encoding="utf-8"))
    index = tmp_path / PRIVATE_INDEX
    for body, says in _malformed(good["episodes"][0]):
        index.write_bytes(body)
        with pytest.raises(ValueError) as refused:
            private_backtests(str(index))
        assert str(index) in str(refused.value) and says in str(refused.value), body


def test_a_malformed_index_makes_only_its_own_contract_unavailable(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # sis.contract builds the wholesale contract at import, and the loop and
    # every role actor import it: a bad index stopped roman and sort as well.
    from sis import backtest, contract

    root = tmp_path / "repo"
    (root / CA_PRICE_WHOLESALE_INDEX).parent.mkdir(parents=True)
    monkeypatch.setattr(contract, "PROJECT_ROOT", root)
    monkeypatch.setattr(backtest, "PROJECT_ROOT", root)
    good = json.loads(pathlib.Path(_fixture(tmp_path)).read_text(encoding="utf-8"))
    for body, says in _malformed(good["episodes"][0]):
        (root / CA_PRICE_WHOLESALE_INDEX).write_bytes(body)
        wholesale, problem = contract._promoted_wholesale()
        assert wholesale is None and problem is not None and says in problem, body
    # A sound index still registers it, and no index means simply absent.
    (root / CA_PRICE_WHOLESALE_INDEX).write_text(json.dumps(good), encoding="utf-8")
    wholesale, problem = contract._promoted_wholesale()
    assert wholesale is not None and wholesale.name == "ca_price_wholesale" and problem is None
    (root / CA_PRICE_WHOLESALE_INDEX).unlink()
    assert contract._promoted_wholesale() == (None, None)
    # Choosing it then says why, rather than "not a registered contract".
    from sis.roles import unknown_contract

    monkeypatch.setitem(contract.UNAVAILABLE, "ca_price_wholesale", problem_text := "bad index")
    assert unknown_contract("contract_name", "ca_price_wholesale", ["roman"]) == \
        f"contract_name='ca_price_wholesale' is unavailable: {problem_text}"
    assert "not a registered contract" in unknown_contract("contract_name", "romna", ["roman"])
    monkeypatch.delitem(contract.UNAVAILABLE, "ca_price_wholesale")
    # Today's registry: nothing is unavailable, and the others are all there.
    assert contract.UNAVAILABLE == {}
    assert {"roman", "sort", "sum_of_divisors"} <= {
        c.name for c in contract.REGISTERED_CONTRACTS}


def test_its_files_are_classified() -> None:
    assert policy.classify("runtime/ca_price_wholesale.py") is policy.ChangeTier.SOFT
    for path in ("specs/ca_price_wholesale/oracle.py", "specs/ca_price_wholesale/tests.py",
                 CA_PRICE_WHOLESALE_INDEX,
                 "specs/ca_price_wholesale/private/backtests/2020-01-06_fixture.json"):
        assert policy.classify(path) is policy.ChangeTier.FORBIDDEN, path


def test_it_is_judged_on_held_out_weeks_against_persistence(tmp_path: pathlib.Path) -> None:
    contract = ca_price_wholesale(_fixture(tmp_path))
    assert contract.skill is not None and contract.skill.baseline == "persistence"
    assert contract.skill.margin == pytest.approx(0.10)
    assert len(contract.backtests) == 3
    assert all(b.tolerance == pytest.approx(0.15) and b.fixture_sha256
               for b in contract.backtests)


def test_the_stub_passes_the_gauntlet(tmp_path: pathlib.Path) -> None:
    contract = ca_price_wholesale(_fixture(tmp_path))
    result = gauntlet.validate(STUB.read_text(encoding="utf-8"), contract=contract)
    assert result.passed, result.reason


def test_persistence_is_within_every_bound_and_still_fails(tmp_path: pathlib.Path) -> None:
    contract = ca_price_wholesale(_fixture(tmp_path))
    source = PERSISTENCE.read_text(encoding="utf-8")
    result = gauntlet.validate(source, contract=contract)
    assert not result.passed
    assert "did not beat the baseline" in result.reason


def test_a_constant_forecast_fails_acceptance(
        tmp_path: pathlib.Path) -> None:
    contract = ca_price_wholesale(_fixture(tmp_path))
    flat = ("from typing import Any\n\n\n"
            "def predict_next_week(state: dict[str, Any]) -> float:\n"
            "    return 4.0\n")
    result = gauntlet.validate(flat, contract=contract)
    assert not result.passed and "acceptance" in result.reason
