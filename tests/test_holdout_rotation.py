"""OMNI-225: D5's rotation. Fresh weeks become the held-out set, the old held-out
weeks join the training split, and nothing already judged is judged again.

The script half runs on the synthetic trace of ``tests/test_fixture_builder.py``
(wholesale-shaped, with the licensed spot price). No network.
"""

from __future__ import annotations

import datetime
import json
import pathlib
from dataclasses import replace
from typing import Any

import pytest

from sis import holdout
from sis.backtest import TRAINING_FILE, Backtest, Split, check_training, load_training
from sis.contract_author import load_staged
from tests.test_fixture_builder import (
    SPOT,
    WHOLESALE,
    _script,
    _spot_calendar,
    _spot_trace,
    _tree,
    _wholesale,
)

D = datetime.date.fromisoformat
WEEK = datetime.timedelta(weeks=1)


def _weeks(first: str, count: int) -> list[datetime.date]:
    return [D(first) + n * WEEK for n in range(count)]


# --- the plan (pure) ----------------------------------------------------------------


def test_only_the_unbroken_weeks_after_the_held_out_set_are_fresh() -> None:
    current = _weeks("2026-01-05", 10)
    available = [*_weeks("2025-01-06", 30), *_weeks("2026-03-16", 5), D("2026-05-04")]
    plan = holdout.plan_rotation(current, available, min_weeks=3)
    # Weeks at or before the set's last were judged or trained on: never fresh.
    # A gap (2026-04-20 missing) ends the run: the build would refuse it.
    assert plan.fresh == tuple(_weeks("2026-03-16", 5))
    assert plan.ready and plan.first == D("2026-03-16") and plan.last == D("2026-04-13")
    text = plan.describe()
    assert "5 fresh week(s) 2026-03-16 to 2026-04-13" in text
    assert "10 held-out week(s) 2026-01-05 to 2026-03-09 join the training split" in text
    assert "runs to 2026-03-09" in text


def test_too_few_fresh_weeks_refuse_and_say_when_there_will_be_enough() -> None:
    plan = holdout.plan_rotation(_weeks("2020-01-06", 356), _weeks("2026-11-02", 3), 13)
    assert not plan.ready
    text = plan.describe()
    assert "3 of 13" in text and "after 2026-10-26" in text
    assert "week of 2027-01-25" in text and "release calendar" in text


def test_no_held_out_set_or_a_silly_minimum_is_refused() -> None:
    with pytest.raises(ValueError, match="no held-out set"):
        holdout.plan_rotation([], _weeks("2026-01-05", 3), 1)
    with pytest.raises(ValueError, match="at least 1"):
        holdout.plan_rotation(_weeks("2026-01-05", 3), [], 0)


def test_held_out_weeks_come_from_episode_names_and_a_dateless_one_refuses() -> None:
    backtests = (Backtest(name="2020-01-13", fixture="b", expect="b"),
                 Backtest(name="2020-01-06", fixture="a", expect="a"),
                 Backtest(name="2019-06-03", fixture="t", expect="t", split=Split.TRAIN))
    assert holdout.held_out_weeks(backtests) == (D("2020-01-06"), D("2020-01-13"))
    with pytest.raises(ValueError, match="not named for its week"):
        holdout.held_out_weeks((Backtest(name="spike", fixture="s", expect="s"),))


def test_the_spent_budget_page_says_how_to_rotate() -> None:
    page = holdout.refusal(holdout.Budget("ca_price_wholesale", "f" * 64, 50, 50))
    assert "--fixture ca_price_wholesale --rotate" in page
    assert "training split" in page and "starts at zero" in page


# --- the script -----------------------------------------------------------------------


def _setup(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
           committed: bool = True) -> tuple[Any, list[str]]:
    from sis import contract_author

    monkeypatch.setattr(contract_author, "CONTRACT_STAGING_DIR", tmp_path / "staging")
    script = _script()
    specs = tmp_path / "specs"
    monkeypatch.setattr(script, "SPECS", specs)
    monkeypatch.setitem(script.FIXTURES, "ca_price_wholesale",
                        replace(script.FIXTURES["ca_price_wholesale"], question=WHOLESALE))
    if committed:   # the promoted held-out set: 2022-09-05 to 2022-10-10
        index = specs / "ca_price_wholesale" / "private.json"
        index.parent.mkdir(parents=True)
        index.write_text(_wholesale()["private.json"], encoding="utf-8")
    trace = tmp_path / "eia.jsonl"
    trace.write_text("".join(json.dumps(line, sort_keys=True) + "\n" for line in _spot_trace()),
                     encoding="utf-8")
    calendar = tmp_path / "calendar.json"
    calendar.write_text(_spot_calendar().raw, encoding="utf-8")
    return script, ["--fixture", "ca_price_wholesale", "--trace", str(trace),
                    "--calendar", str(calendar), "--first", "2022-01-03"]


def test_rotation_stages_fresh_held_out_weeks_and_the_old_ones_as_training(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    script, args = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("SIS_HOLDOUT_ROTATION_MIN_WEEKS", "4")
    assert script.main([*args, "--rotate", "--last", "2022-11-14"]) == 0, \
        capsys.readouterr().err
    out = capsys.readouterr().out
    assert "5 fresh week(s) 2022-10-17 to 2022-11-14 become the held-out set" in out
    assert "6 held-out week(s) 2022-09-05 to 2022-10-10 join the training split" in out
    staged = load_staged("ca_price_wholesale")
    assert f"digest: {staged.digest}" in out
    files = {name: (staged.directory / name).read_text(encoding="utf-8")
             for name in staged.files}
    held = sorted(name.rsplit("/", 1)[1][:10] for name in files
                  if name.endswith("_fixture.json"))
    assert held == [w.isoformat() for w in _weeks("2022-10-17", 5)]
    assert all(name.startswith("private/") for name in files if "backtests/" in name)

    # The training split now runs up to the new held-out set, over the old one,
    # and the contract layer accepts the pair.
    (tmp_path / TRAINING_FILE).write_text(files[TRAINING_FILE], encoding="utf-8")
    split = load_training(str(tmp_path / TRAINING_FILE), root=pathlib.Path("/"))
    body = json.loads(files[TRAINING_FILE])
    assert body["held_out_from"] == "2022-10-17"
    shown = {r["event_time"][:10] for r in split.state["readings"]}
    assert {"2022-09-05", "2022-10-03", "2022-10-10"} <= shown
    assert not any(day >= "2022-10-17" for day in shown)
    check_training(split, tuple(Backtest(name=w, fixture="f", expect="e") for w in held),
                   contract="ca_price_wholesale")

    # Licensed values stay private: the training split never shows the series,
    # and the committed index and README hold no recorded value at all.
    assert SPOT not in files[TRAINING_FILE]
    assert any(SPOT in text for name, text in files.items() if name.endswith("_fixture.json"))
    assert '"value"' not in files["private.json"] and '"value"' not in files["README.md"]


def test_rotation_waits_for_enough_fresh_weeks_and_leaves_specs_alone(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    script, args = _setup(tmp_path, monkeypatch)
    before = _tree(tmp_path / "specs")
    monkeypatch.setenv("SIS_HOLDOUT_ROTATION_MIN_WEEKS", "13")
    assert script.main([*args, "--rotate", "--last", "2022-11-14"]) == 1
    err = capsys.readouterr().err
    assert "not enough fresh weeks to rotate: 5 of 13" in err
    assert "week of 2023-01-09" in err
    assert _tree(tmp_path / "specs") == before


def test_rotation_needs_a_promoted_held_out_set(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    script, args = _setup(tmp_path, monkeypatch, committed=False)
    assert script.main([*args, "--rotate"]) == 1
    assert "build and promote one first" in capsys.readouterr().err


def test_a_plain_rebuild_keeps_the_promoted_weeks_and_takes_no_fresh_ones(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    # Adding fresh weeks to a judged set would give it a new digest, and so a
    # new budget, on weeks the loop has seen judged. Only --rotate takes them.
    script, args = _setup(tmp_path, monkeypatch)
    index = tmp_path / "specs" / "ca_price_wholesale" / "private.json"
    body = json.loads(index.read_text(encoding="utf-8"))
    body["episodes"] = body["episodes"][1:4]          # 2022-09-12 to 2022-09-26
    index.write_text(json.dumps(body), encoding="utf-8")
    plain = [a for a in args if a not in ("--first", "2022-01-03")]
    assert script.main(plain) == 0, capsys.readouterr().err
    staged = load_staged("ca_price_wholesale")
    held = sorted(name.rsplit("/", 1)[1][:10] for name in staged.files
                  if name.endswith("_fixture.json"))
    assert held == ["2022-09-12", "2022-09-19", "2022-09-26"]
    # The training split, too, stops where the promoted set begins.
    assert script.main([*plain, "--training", "--first", "2022-01-03"]) == 0
    training = json.loads((load_staged("ca_price_wholesale").directory / TRAINING_FILE)
                          .read_text(encoding="utf-8"))
    assert training["held_out_from"] == "2022-09-12"
