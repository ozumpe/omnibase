"""OMNI-205: a training split the proposer may see, for contracts judged on held-out history.

The split is built from the same synthetic trace as the fixture tests
(``tests/test_fixture_builder.py``), beside the wholesale-shaped question
there, which shows the licensed spot price. No network.
"""

from __future__ import annotations

import datetime
import json
import pathlib
from dataclasses import replace
from typing import Any

import pytest

from sis import proposer
from sis.adapters_real import EIA_RULES
from sis.backtest import (
    TRAINING_FILE,
    Backtest,
    TrainingSplit,
    check_training,
    load_training,
)
from sis.contract import FeatureContract
from sis.fixture_builder import FixtureRefused, build_training
from sis.sensor_input import SIMULATED_SOURCE
from tests.test_fixture_builder import (
    CA,
    SPOT,
    WHOLESALE,
    _private_gate,
    _spot_calendar,
    _spot_trace,
    _wholesale,
)

FIRST = datetime.date(2022, 1, 3)


def _training_text() -> str:
    return build_training(_spot_trace(), EIA_RULES, _spot_calendar(), WHOLESALE, first=FIRST,
                          name="ca_price_wholesale", spec_ref="OMNI-205").files[TRAINING_FILE]


def _training() -> dict[str, Any]:
    body: dict[str, Any] = json.loads(_training_text())
    return body


def _held_out_questions() -> dict[str, datetime.datetime]:
    """Each held-out episode's name and the moment its question is asked."""
    return {name.split("/")[-1].removesuffix("_fixture.json"):
            datetime.datetime.fromisoformat(json.loads(body)["event_time"])
            for name, body in _wholesale().items() if name.endswith("_fixture.json")}


def _write(tmp_path: pathlib.Path, body: dict[str, Any] | str) -> str:
    path = tmp_path / TRAINING_FILE
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return str(path)


def _contract(training: TrainingSplit | None, names: tuple[str, ...] = ("2022-09-05",)
              ) -> FeatureContract:
    return FeatureContract(
        name="ca_price_wholesale", spec_ref="OMNI-205", entry="predict",
        entry_module="runtime/ca_price_wholesale.py",
        acceptance_tests="specs/ca_price_wholesale/tests.py", public_api=("predict",),
        backtests=tuple(Backtest(name=n, fixture=f"{n}_f.json", expect=f"{n}_e.json")
                        for n in names),
        training=training)


# --- the build --------------------------------------------------------------------


def test_no_training_record_overlaps_or_post_dates_a_held_out_episode() -> None:
    state = _training()["state"]
    asked = _held_out_questions()
    first_question = min(asked.values())
    first_week = datetime.date.fromisoformat(min(asked))
    assert state["readings"]
    for record in [*state["readings"], *state["absent"]]:
        # Known strictly before the first held-out question was asked, so
        # before every one of them, and about a period before the first week.
        assert datetime.datetime.fromisoformat(record["known_at"]) < first_question
        assert datetime.date.fromisoformat(record["event_time"][:10]) < first_week
    assert datetime.datetime.fromisoformat(state["as_of"]) < first_question
    # No held-out answer is in it: every week it holds is before the first held out.
    weeks = {r["event_time"][:10] for r in state["readings"] if r["series"] == CA}
    assert not weeks & set(asked)


def test_the_split_is_what_was_known_by_the_replay_rule() -> None:
    state = _training()["state"]
    assert all(r["known_at"] <= state["as_of"] for r in state["readings"])
    assert min(r["event_time"][:10] for r in state["readings"]) >= FIRST.isoformat()
    # One value per period: the latest vintage, as an episode shows.
    periods = [(r["series"], r["event_time"]) for r in state["readings"]]
    assert len(periods) == len(set(periods))


def test_the_split_never_shows_a_licensed_series() -> None:
    # WHOLESALE shows the LA spot price; the training split leaves it out
    # whatever the held-out question shows (D0 is undecided, OMNI-205).
    text = _training_text()
    assert SPOT not in text
    assert {r["series"] for r in _training()["state"]["readings"]} <= \
        EIA_RULES.series.keys() - EIA_RULES.licensed
    with pytest.raises(FixtureRefused, match="licensed"):
        build_training(_spot_trace(), EIA_RULES, _spot_calendar(),
                       replace(WHOLESALE, target=SPOT), first=FIRST, name="n", spec_ref="x")


def test_a_reading_the_calendar_does_not_cover_refuses_the_split() -> None:
    # The synthetic calendar starts in June 2021: earlier history has no
    # release evidence, so it cannot be shown.
    trace = _spot_trace()
    with pytest.raises(FixtureRefused, match="does not cover"):
        build_training(_with_earlier_history(trace), EIA_RULES, _spot_calendar(), WHOLESALE,
                       first=datetime.date(2021, 1, 4), name="n", spec_ref="x")


def _with_earlier_history(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from sis.adapters_real import eia_period, eia_published
    from sis.sensor_trace import Fetched, capture_lines
    days = [datetime.date(2021, 1, 4) + datetime.timedelta(weeks=n) for n in range(80)]
    return capture_lines(
        [], [Fetched(CA, "Dollars per Gallon",
                     [(eia_period(d.strftime("%Y%m%d")), 4.0) for d in days])],
        EIA_RULES, capture="eia-20261015T020000.000000Z-abcdef02",
        fetched_at=datetime.datetime(2026, 10, 15, 2, 0, tzinfo=datetime.UTC),
        published=eia_published,
        provenance={"url": "https://www.eia.gov/opendata/bulk/PET.zip", "sha256": "cd" * 32})


def test_the_same_trace_builds_a_byte_identical_split() -> None:
    assert _training_text() == _training_text()
    json.loads(_training_text())


# --- loading and declaring ------------------------------------------------------------


def test_a_built_split_loads_and_a_contract_declares_it(tmp_path: pathlib.Path) -> None:
    split = load_training(_write(tmp_path, _training_text()), root=pathlib.Path("/"))
    assert split.target == CA and SPOT not in split.series()
    assert _contract(split).training is split


def test_a_split_that_shows_a_value_before_it_was_known_is_refused(
        tmp_path: pathlib.Path) -> None:
    body = _training()
    body["state"]["readings"][0]["known_at"] = "2099-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="after the split's as_of"):
        load_training(_write(tmp_path, body), root=pathlib.Path("/"))


@pytest.mark.parametrize("change", [
    lambda b: b.update(schema=2),
    lambda b: b["state"].update(readings=[]),
    lambda b: b.update(target="PET.NOT_SHOWN.W"),
    lambda b: b["state"]["readings"][0].update(source=SIMULATED_SOURCE),
])
def test_a_malformed_split_is_refused(tmp_path: pathlib.Path, change: Any) -> None:
    body = _training()
    change(body)
    with pytest.raises(ValueError):
        load_training(_write(tmp_path, body), root=pathlib.Path("/"))


def test_a_split_that_reaches_into_the_exam_is_refused_at_declaration(
        tmp_path: pathlib.Path) -> None:
    split = load_training(_write(tmp_path, _training_text()), root=pathlib.Path("/"))
    check_training(split, _contract(None).backtests, contract="c")
    # Held out from a week the split already covers: the contract is refused.
    with pytest.raises(ValueError, match="before the exam begins"):
        _contract(split, names=("2022-06-06", "2022-09-05"))
    # A held-out episode that is not named for its week cannot be placed in time.
    with pytest.raises(ValueError, match="not named for its week"):
        _contract(split, names=("autumn",))


# --- the gate and the prompt -----------------------------------------------------------


def test_the_backtest_gate_ignores_the_training_split(tmp_path: pathlib.Path) -> None:
    # The gate judges on the held-out episodes only: a model that just looks
    # up training answers is still rejected, as persistence is.
    persistence = (f"def predict(state):\n    prices = [r for r in state['readings'] "
                   f"if r['series'] == {CA!r}]\n"
                   "    return max(prices, key=lambda r: r['event_time'])['value']\n")
    files = {**_wholesale(), TRAINING_FILE: _training_text()}
    result = _private_gate(tmp_path, files, persistence)
    assert result is not None and not result.passed
    # Nothing of the split reached the sandbox.
    sandbox = tmp_path / "sandbox"
    assert not any(TRAINING_FILE in p.name for p in sandbox.rglob("*"))
    copied = sorted(p.name for p in (sandbox / "fixtures").glob("*"))
    assert len(copied) == 2 * len(_held_out_questions())
    assert all(name.endswith(("_fixture.json", "_expect.json")) for name in copied)


def test_the_build_prompt_shows_the_split_and_nothing_held_out(
        tmp_path: pathlib.Path) -> None:
    split = load_training(_write(tmp_path, _training_text()), root=pathlib.Path("/"))
    prompt = proposer._build_prompt("", _contract_with_tests(split))
    assert "Training data you may fit on" in prompt
    some = split.state["readings"][-1]
    assert f"{some['event_time'][:10]} {some['value']}" in prompt
    assert SPOT not in prompt
    for week in _held_out_questions():
        assert f"\n{week} " not in prompt     # no held-out week's value


def test_without_a_split_the_prompt_says_nothing_of_training() -> None:
    prompt = proposer._build_prompt("", _contract_with_tests(None))
    assert "Training data" not in prompt


def _contract_with_tests(split: TrainingSplit | None) -> FeatureContract:
    from sis.contract import ROMAN
    return replace(ROMAN, training=split, backtests=(
        Backtest(name="2022-09-05", fixture="f.json", expect="e.json"),))


def test_the_script_stages_the_split_for_a_human_and_leaves_specs_alone(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    from sis import contract_author
    from tests.test_fixture_builder import _script, _tree

    monkeypatch.setattr(contract_author, "CONTRACT_STAGING_DIR", tmp_path / "staging")
    trace = tmp_path / "eia.jsonl"
    trace.write_text("".join(json.dumps(line, sort_keys=True) + "\n" for line in _spot_trace()),
                     encoding="utf-8")
    calendar = tmp_path / "calendar.json"
    calendar.write_text(_spot_calendar().raw, encoding="utf-8")
    script = _script()
    monkeypatch.setitem(script.FIXTURES, "ca_price_wholesale",
                        replace(script.FIXTURES["ca_price_wholesale"], question=WHOLESALE))
    promoted = pathlib.Path(contract_author.SPECS_DIR) / "ca_price_wholesale"
    before = _tree(promoted)
    args = ["--fixture", "ca_price_wholesale", "--training", "--trace", str(trace),
            "--calendar", str(calendar)]
    assert script.main([*args, "--first", FIRST.isoformat()]) == 0
    out = capsys.readouterr().out
    assert f"training split from {FIRST}" in out
    staged = contract_author.load_staged("ca_price_wholesale")
    assert staged.files == (TRAINING_FILE,)
    assert f"digest: {staged.digest}" in out
    assert _tree(promoted) == before
