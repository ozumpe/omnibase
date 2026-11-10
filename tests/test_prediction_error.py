"""OMNI-36: prediction error against a persistence baseline, computed and reported.

No network. Traces are built through the real capture path, scenarios through
the pinned generator. What these pin: the predictor is never shown a value
before its ``known_at``; the error is signed and a week with nothing to compare
with is skipped, never scored; the predictor runs in the sandbox; the errors
reach telemetry and the SelfModel's provenance; and nothing here starts a cycle.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import pathlib
import subprocess
import sys
from typing import Any

import pytest

from sis import gauntlet, policy, prediction_error, replay, scenarios
from sis.adapters import InMemoryTelemetry
from sis.adapters_real import EIA_RULES, eia_period, eia_published
from sis.clock import ReplayClock
from sis.contract import CA_PRICE_PERSISTENCE, FEATURE_CONTRACTS, REGISTERED_CONTRACTS
from sis.paths import PROJECT_ROOT
from sis.ports import Reading
from sis.sensor_input import Absent
from sis.sensor_trace import Fetched, capture_lines, read_back

CA = prediction_error.CALIFORNIA
US = "PET.EMM_EPMRR_PTE_NUS_DPG.W"
UNIT = "Dollars per Gallon"
FETCHED = datetime.datetime(2026, 10, 15, 2, 0, tzinfo=datetime.UTC)
STUB = PROJECT_ROOT / "runtime" / "candidates" / "ca_price_persistence.py"
SCRIPT = PROJECT_ROOT / "scripts" / "prediction_error.py"


def _mondays(first: str, n: int) -> list[datetime.date]:
    day = datetime.date.fromisoformat(first)
    return [day + datetime.timedelta(weeks=i) for i in range(n)]


def _trace(prices: dict[datetime.date, float]) -> list[dict[str, Any]]:
    def series(values: dict[datetime.date, float]) -> list[tuple[object, object]]:
        return [(eia_period(day.strftime("%Y%m%d")), value) for day, value in values.items()]

    return capture_lines(
        [], [Fetched(CA, UNIT, series(prices)),
             Fetched(US, UNIT, series({day: 3.9 for day in prices}))],
        EIA_RULES, capture="eia-20261015T020000.000000Z-abcdef01", fetched_at=FETCHED,
        published=eia_published,
        provenance={"url": "https://www.eia.gov/opendata/bulk/PET.zip", "sha256": "ab" * 32,
                    "attribution": "Source: U.S. Energy Information Administration (Oct 2026)"})


PRICES = dict(zip(_mondays("2022-08-01", 10),
                  (5.32, 5.21, 5.12, 5.08, 5.06, 5.053, 5.18, 5.39, 5.60, 6.213), strict=True))


def _readings() -> tuple[tuple[Reading, ...], tuple[Absent, ...]]:
    readings, absent = read_back(_trace(PRICES), EIA_RULES)
    return tuple(readings), tuple(absent)


def _persistence(state: dict[str, Any]) -> float:
    ca = [r for r in state["readings"] if r["series"] == CA]
    return float(max(ca, key=lambda r: r["event_time"])["value"])


# -- the questions ------------------------------------------------------------

def test_one_question_per_week_in_the_order_its_value_became_known() -> None:
    asked = prediction_error.questions(*_readings())
    assert [q.week.date() for q in asked] == list(PRICES)
    assert [q.at for q in asked] == sorted(q.at for q in asked)
    assert all(q.at > q.week for q in asked)
    # Each question's next week is the following Monday, at its latest value.
    assert [q.actual for q in asked[:-1]] == list(PRICES.values())[1:]
    assert asked[-1].actual is None and asked[-1].next_week is None


def test_the_predictor_is_never_shown_a_value_before_its_known_at() -> None:
    readings, absent = _readings()
    asked = prediction_error.questions(readings, absent)
    seen: list[tuple[datetime.datetime, list[dict[str, Any]]]] = []

    def recording(state: dict[str, Any]) -> float:
        at = datetime.datetime.fromisoformat(state["as_of"])
        seen.append((at, state["readings"] + state["absent"]))
        return _persistence(state)

    prediction_error.ask(recording, asked)
    assert len(seen) == len(asked)
    for at, records in seen:
        assert records, at
        for record in records:
            assert datetime.datetime.fromisoformat(record["known_at"]) <= at, (at, record)
    # And the week asked about is the newest California week it is shown.
    for question, (_, records) in zip(asked, seen, strict=True):
        newest = max(datetime.datetime.fromisoformat(r["event_time"])
                     for r in records if r["series"] == CA)
        assert newest == question.week


def test_a_window_and_a_clock() -> None:
    readings, absent = _readings()
    every = prediction_error.questions(readings, absent)
    clock = ReplayClock(every[0].at - datetime.timedelta(days=1))
    some = prediction_error.questions(readings, absent, start=every[2].at, end=every[5].at,
                                      clock=clock)
    assert [q.week for q in some] == [q.week for q in every[2:5]]
    assert clock.now() == every[4].at


def test_lookback_limits_the_history_shown() -> None:
    asked = prediction_error.questions(*_readings(), lookback=datetime.timedelta(weeks=2))
    last = asked[-1]
    weeks = {datetime.datetime.fromisoformat(r["event_time"]) for r in last.state["readings"]}
    assert min(weeks) >= last.week - datetime.timedelta(weeks=2)


def test_a_revision_is_the_actual_and_a_withdrawal_is_skipped() -> None:
    # A later capture revises one week and blanks another: the actual is what
    # the trace holds now, and a withdrawn week has nothing to compare with.
    days = list(PRICES)
    first = _trace(PRICES)
    later = dict(PRICES)
    later[days[3]] = 5.50
    later_lines = capture_lines(
        first, [Fetched(CA, UNIT, [(eia_period(d.strftime("%Y%m%d")),
                                    "NA" if d == days[6] else v) for d, v in later.items()]),
                Fetched(US, UNIT, [(eia_period(d.strftime("%Y%m%d")), 3.9) for d in later])],
        EIA_RULES, capture="eia-20261016T020000.000000Z-abcdef02",
        fetched_at=FETCHED + datetime.timedelta(days=1), published=eia_published,
        provenance={"url": "https://www.eia.gov/opendata/bulk/PET.zip", "sha256": "cd" * 32,
                    "attribution": "Source: U.S. Energy Information Administration (Oct 2026)"})
    readings, absent = read_back(first + later_lines, EIA_RULES)
    asked = {q.week.date(): q for q in prediction_error.questions(readings, absent)}
    assert asked[days[2]].actual == 5.50
    assert asked[days[5]].actual is None
    assert asked[days[5]].next_week is not None and asked[days[5]].next_week.date() == days[6]


def test_a_missing_week_is_a_gap_not_the_week_after() -> None:
    prices = {day: value for day, value in PRICES.items() if day != list(PRICES)[4]}
    readings, absent = read_back(_trace(prices), EIA_RULES)
    asked = {q.week.date(): q for q in prediction_error.questions(readings, absent)}
    gap = asked[list(PRICES)[3]]
    assert gap.actual is None and gap.next_week is None


def _skipped_capture() -> tuple[list[Reading], list[Absent]]:
    # The first capture ends two weeks early; the next capture, a week late,
    # brings the last two weeks in at one fetch, so both are known at once.
    days = list(PRICES)
    first = capture_lines(
        [], [Fetched(CA, UNIT, [(eia_period(d.strftime("%Y%m%d")), PRICES[d]) for d in days[:-2]]),
             Fetched(US, UNIT, [(eia_period(d.strftime("%Y%m%d")), 3.9) for d in days[:-2]])],
        EIA_RULES, capture="eia-20221005T020000.000000Z-abcdef01",
        fetched_at=datetime.datetime(2022, 9, 21, 23, tzinfo=datetime.UTC),
        published=eia_published,
        provenance={"url": "https://www.eia.gov/opendata/bulk/PET.zip", "sha256": "ab" * 32,
                    "attribution": "Source: U.S. Energy Information Administration (Sep 2022)"})
    later = capture_lines(
        first, [Fetched(CA, UNIT, [(eia_period(d.strftime("%Y%m%d")), PRICES[d]) for d in days]),
                Fetched(US, UNIT, [(eia_period(d.strftime("%Y%m%d")), 3.9) for d in days])],
        EIA_RULES, capture="eia-20221006T020000.000000Z-abcdef02",
        fetched_at=datetime.datetime(2022, 10, 6, 2, tzinfo=datetime.UTC),
        published=eia_published,
        provenance={"url": "https://www.eia.gov/opendata/bulk/PET.zip", "sha256": "cd" * 32,
                    "attribution": "Source: U.S. Energy Information Administration (Oct 2022)"})
    readings, absent = read_back(first + later, EIA_RULES)
    return list(readings), list(absent)


def test_a_next_week_known_when_asked_is_skipped_never_scored() -> None:
    # M34 (OMNI-170): a skipped capture made the 26 Sep and 3 Oct prices known
    # at the same fetch. Asked about 26 Sep, the view held 3 Oct too, and
    # persistence, returning it, scored an error of 0.
    days = list(PRICES)
    asked = prediction_error.questions(*_skipped_capture())
    by_week = {q.week.date(): q for q in asked}
    leaked = by_week[days[-2]]
    assert leaked.at == by_week[days[-1]].at
    assert any(r["event_time"] == by_week[days[-1]].week.isoformat()
               for r in leaked.state["readings"] if r["series"] == CA)
    assert leaked.actual is None and leaked.skip
    report = prediction_error.score(asked, prediction_error.ask(_persistence, asked))
    step = next(s for s in report.steps if s.week.date() == days[-2])
    assert step.error is None and "already known" in step.note
    assert all(s.error != 0 for s in report.scored)
    assert report.summary()["scored"] == len(days) - 2


# -- the score ------------------------------------------------------------------

def test_the_error_is_signed_and_the_summary_counts_skips() -> None:
    asked = prediction_error.questions(*_readings())
    report = prediction_error.score(asked, prediction_error.ask(_persistence, asked),
                                    unit=UNIT, source="test")
    values = list(PRICES.values())
    expected = [a - b for a, b in zip(values, values[1:], strict=False)]
    assert [s.error for s in report.scored] == pytest.approx(expected)
    s = report.summary()
    assert (s["weeks"], s["scored"], s["skipped"]) == (10, 9, 1)
    assert s["bias"] == pytest.approx(sum(expected) / 9)
    assert s["bias"] < 0          # persistence is late in a rising market
    assert s["mae"] == pytest.approx(sum(abs(e) for e in expected) / 9)
    assert s["rmse"] >= s["mae"]
    assert "9 of 10 weeks scored" in prediction_error.table(report)


def test_a_failed_or_non_numeric_answer_is_a_note_never_a_score() -> None:
    asked = prediction_error.questions(*_readings())[:4]
    answers: list[Any] = [ValueError("boom"), "5.1", True, float("nan")]
    report = prediction_error.score(asked, answers)
    assert report.scored == ()
    assert "failed" in report.steps[0].note
    assert all("not a number" in step.note for step in report.steps[1:])
    with pytest.raises(ValueError, match="answers"):
        prediction_error.score(asked, answers[:2])


@pytest.mark.parametrize("answer", [10**400, -(10**400), 10**5000],
                         ids=["huge", "-huge", "too-long-to-print"])
def test_an_answer_too_large_for_a_float_is_not_a_number(answer: int) -> None:
    # OMNI-201 (L73): JSON carries an int of any size and the sandbox worker
    # decodes it as one; math.isfinite raised OverflowError on it, and the whole
    # report was lost instead of one step. Past 4300 digits str() itself raises,
    # so the note may not print it either.
    asked = prediction_error.questions(*_readings())[:2]
    report = prediction_error.score(asked, [answer, 5.0])
    assert report.steps[0].predicted is None and report.steps[0].error is None
    assert "not a number" in report.steps[0].note
    assert report.steps[1].predicted == 5.0


def test_an_empty_report_has_no_statistics() -> None:
    s = prediction_error.Report(target=CA, unit=UNIT, source="", steps=()).summary()
    assert s["scored"] == 0 and s["bias"] is None and s["mae"] is None and s["rmse"] is None


# -- climatology and skill (OMNI-195) -------------------------------------------

def _monday(year: int, week: int) -> datetime.datetime:
    return datetime.datetime.combine(datetime.date.fromisocalendar(year, week, 1),
                                     datetime.time(), tzinfo=datetime.UTC)


def test_climatology_is_the_latest_value_plus_the_mean_seasonal_change() -> None:
    # Week 40 to 41 rose 0.10 in 2019 and 0.30 in 2020; 2021 sits at 4.00.
    values = {_monday(2019, 40): 3.00, _monday(2019, 41): 3.10,
              _monday(2020, 40): 2.00, _monday(2020, 41): 2.30,
              _monday(2021, 40): 4.00}
    assert prediction_error.climatology(values, _monday(2021, 40)) == pytest.approx(4.20)
    # Only the latest year if asked for one.
    assert prediction_error.climatology(values, _monday(2021, 40), years=1) \
        == pytest.approx(4.30)
    # No earlier year with the pair, or no value for the week: no forecast.
    assert prediction_error.climatology(values, _monday(2019, 40)) is None
    assert prediction_error.climatology(values, _monday(2021, 41)) is None
    gap = {_monday(2020, 40): 2.0, _monday(2020, 43): 2.5, _monday(2021, 40): 4.0}
    assert prediction_error.climatology(gap, _monday(2021, 40)) is None


def test_skill_is_one_minus_the_ratio_of_maes() -> None:
    assert prediction_error.skill(0.05, 0.10) == pytest.approx(0.5)
    assert prediction_error.skill(0.10, 0.10) == pytest.approx(0.0)
    assert prediction_error.skill(0.20, 0.10) == pytest.approx(-1.0)
    assert prediction_error.skill(0.1, 0.0) is None
    assert prediction_error.skill(0.1, 1e-16) is None    # perfect, up to rounding
    assert prediction_error.skill(None, 0.1) is None and prediction_error.skill(0.1, None) is None


def _three_autumns() -> dict[datetime.date, float]:
    # Each autumn rises 0.05 a week from its own level; the model below
    # (persistence) misses each rise by 0.05, climatology learns it.
    prices: dict[datetime.date, float] = {}
    for year, level in ((2020, 3.0), (2021, 4.0), (2022, 5.0)):
        start = datetime.date.fromisocalendar(year, 32, 1)
        for i in range(10):
            prices[start + datetime.timedelta(weeks=i)] = round(level + 0.05 * i, 3)
    return prices


def test_a_climatology_that_learned_the_season_claims_no_skill_to_beat() -> None:
    readings, absent = read_back(_trace(_three_autumns()), EIA_RULES)
    asked = prediction_error.questions(readings, absent)
    report = prediction_error.score(asked, prediction_error.ask(_persistence, asked))
    autumn_2022 = [s for s in report.steps if s.week.year == 2022 and s.error is not None]
    assert len(autumn_2022) == 9
    assert all(s.climatology_error == pytest.approx(0.0, abs=1e-9) for s in autumn_2022)
    assert all(s.error == pytest.approx(-0.05) for s in autumn_2022)
    # 2020 had no earlier year; 2021 learned from 2020; and the jump between
    # autumns has no next week within reach, so it is not scored at all.
    assert all(s.climatology is None for s in report.steps if s.week.year == 2020)
    s = report.summary()
    assert s["skill_weeks"] == 18
    assert s["climatology_mae"] == pytest.approx(0.0, abs=1e-9)
    assert s["skill"] is None     # a perfect climatology leaves nothing to beat


def test_skill_against_a_climatology_that_misses() -> None:
    # Two autumns that disagree: 2021's weekly rise is 0.10, 2022's 0.02, so
    # climatology in 2022 forecasts +0.10 and misses by 0.08; persistence
    # misses by 0.02 and has skill 1 - 0.02 / 0.08 = 0.75.
    prices: dict[datetime.date, float] = {}
    for year, step in ((2021, 0.10), (2022, 0.02)):
        start = datetime.date.fromisocalendar(year, 32, 1)
        for i in range(8):
            prices[start + datetime.timedelta(weeks=i)] = round(4.0 + step * i, 3)
    readings, absent = read_back(_trace(prices), EIA_RULES)
    asked = prediction_error.questions(readings, absent)
    report = prediction_error.score(asked, prediction_error.ask(_persistence, asked))
    s = report.summary()
    assert s["skill_weeks"] == 7
    assert s["climatology_mae"] == pytest.approx(0.08)
    assert s["skill"] == pytest.approx(0.75)
    text = prediction_error.table(report)
    assert "against climatology over 7 weeks" in text and "skill +0.750" in text


def test_climatology_sees_nothing_after_the_question() -> None:
    # Change everything after a question: its climatology does not move.
    prices = _three_autumns()
    later = {day: (value + 1.0 if day.year == 2022 and day.month >= 9 else value)
             for day, value in prices.items()}
    before = {q.week: q.climatology for q in prediction_error.questions(
        *read_back(_trace(prices), EIA_RULES))}
    after = {q.week: q.climatology for q in prediction_error.questions(
        *read_back(_trace(later), EIA_RULES))}
    cut = datetime.datetime(2022, 8, 29, tzinfo=datetime.UTC)
    early = [week for week in before if week < cut]
    assert early and all(before[w] == after[w] for w in early)
    # And it is the same forecast the pure function makes from the as-of view.
    readings, absent = read_back(_trace(prices), EIA_RULES)
    for question in prediction_error.questions(readings, absent):
        view = replay.as_of(readings, absent, question.at)
        seen = {r.event_time: r.value for r in view.readings if r.series == CA}
        assert question.climatology == prediction_error.climatology(seen, question.week)


def test_skipped_weeks_stay_out_of_the_skill() -> None:
    asked = prediction_error.questions(*_skipped_capture())
    report = prediction_error.score(asked, prediction_error.ask(_persistence, asked))
    skipped = [s for s in report.steps if s.error is None]
    assert skipped and all(s.climatology_error is None for s in skipped)
    assert report.summary()["skill_weeks"] <= report.summary()["scored"]


def test_an_empty_report_has_no_skill() -> None:
    s = prediction_error.Report(target=CA, unit=UNIT, source="", steps=()).summary()
    assert s["skill_weeks"] == 0 and s["skill"] is None and s["climatology_mae"] is None


# -- scenarios ----------------------------------------------------------------

@pytest.mark.parametrize("kind", ["supply_shock", "outage", "national_move", "demand_shock"])
def test_a_scenario_replays_and_scores(kind: str) -> None:
    generated = scenarios.generate(kind, seed=3)
    asked = prediction_error.questions(generated.readings, generated.absent)
    assert [q.at for q in asked] == generated.decision_times()
    report = prediction_error.score(asked, prediction_error.ask(_persistence, asked))
    assert report.scored and report.summary()["mae"] is not None


def test_a_scenario_with_data_events_skips_what_it_must() -> None:
    plain = scenarios.generate("outage", seed=3)
    events = scenarios.generate("outage", seed=3, data_events=("missing_week", "withdrawn"))
    s_plain = prediction_error.score(
        q := prediction_error.questions(plain.readings, plain.absent),
        prediction_error.ask(_persistence, q)).summary()
    s_events = prediction_error.score(
        q := prediction_error.questions(events.readings, events.absent),
        prediction_error.ask(_persistence, q)).summary()
    assert s_events["skipped"] > s_plain["skipped"]


# -- the sandbox, telemetry and provenance -----------------------------------

def test_the_stub_predictor_answers_from_the_sandbox() -> None:
    asked = prediction_error.questions(*_readings())
    answers = prediction_error.ask_in_sandbox(STUB.read_text(encoding="utf-8"),
                                              CA_PRICE_PERSISTENCE.entry, asked)
    assert answers == pytest.approx(list(PRICES.values()))
    assert prediction_error.ask_in_sandbox(STUB.read_text(encoding="utf-8"),
                                           CA_PRICE_PERSISTENCE.entry, []) == []


def test_a_predictor_that_raises_in_the_sandbox_is_a_failed_step() -> None:
    source = ("from typing import Any\n\n"
              "def predict_next_week(state: dict[str, Any]) -> float:\n"
              "    raise ValueError('no')\n")
    asked = prediction_error.questions(*_readings())[:2]
    answers = prediction_error.ask_in_sandbox(source, "predict_next_week", asked)
    assert all(isinstance(a, BaseException) for a in answers)
    assert all("failed" in s.note for s in prediction_error.score(asked, answers).steps)


def test_publish_reaches_telemetry_and_the_recorder() -> None:
    asked = prediction_error.questions(*_readings())
    report = prediction_error.score(asked, prediction_error.ask(_persistence, asked), unit=UNIT)
    telemetry = InMemoryTelemetry()
    recorded: list[tuple[str, str, dict[str, Any]]] = []

    def record(kind: str, ref: str, **detail: Any) -> None:
        recorded.append((kind, ref, detail))

    prediction_error.publish(report, telemetry, ref="run-1", record=record)
    events = telemetry.events()
    errors = [e for e in events if e["event"] == "prediction.error"]
    assert len(errors) == 10 and all(e["ref"] == "run-1" for e in errors)
    assert [e for e in events if e["event"] == "prediction.summary"][0]["scored"] == 9
    assert recorded == [("prediction_error", "run-1", report.summary())]
    json.dumps(events)            # plain values only


# -- nothing acts on it -------------------------------------------------------

def test_nothing_here_can_start_a_cycle() -> None:
    probe = ("import sys, runpy\n"
             f"sys.argv = ['x', '--scenario', 'supply_shock', '--seed', '1']\n"
             "import sis.prediction_error\n"
             f"runpy.run_path({str(SCRIPT)!r}, run_name='not_main')\n"
             "bad = [m for m in ('sis.org', 'sis.roles', 'sis.loop', 'sis.proposer', 'ray')\n"
             "       if m in sys.modules]\n"
             "print('LOADED', bad)\n")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         cwd=PROJECT_ROOT, timeout=120, check=True)
    assert "LOADED []" in out.stdout, out.stdout + out.stderr


# -- the CLI -------------------------------------------------------------------

def _script() -> Any:
    spec = importlib.util.spec_from_file_location("prediction_error_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_prints_a_scenario_report(capsys: pytest.CaptureFixture[str]) -> None:
    assert _script().main(["--scenario", "supply_shock", "--seed", "7"]) == 0
    out = capsys.readouterr().out
    assert "scenario supply_shock:7" in out and "weeks scored" in out


def test_the_script_replays_autumn_2022_from_a_trace(
        tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    prices = dict(zip(_mondays("2022-07-04", 26), (4.6 + i / 20 for i in range(26)),
                      strict=True))
    trace = tmp_path / "eia.jsonl"
    trace.write_text("".join(json.dumps(line, sort_keys=True) + "\n" for line in _trace(prices)),
                     encoding="utf-8")
    assert _script().main(["--trace", str(trace)]) == 0
    out = capsys.readouterr().out
    assert "2022-08-01" in out and "2022-11-28" in out and "2022-07-25" not in out
    assert "2022-12-05" not in out


def test_the_script_prints_skill_against_climatology(
        tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Two years of history before autumn 2022, so climatology has something to learn.
    prices = dict(zip(_mondays("2020-07-06", 130), (3.5 + (i % 52) / 50 for i in range(130)),
                      strict=True))
    trace = tmp_path / "eia.jsonl"
    trace.write_text("".join(json.dumps(line, sort_keys=True) + "\n" for line in _trace(prices)),
                     encoding="utf-8")
    assert _script().main(["--trace", str(trace)]) == 0
    out = capsys.readouterr().out
    assert "climatology" in out and "against climatology over" in out and "skill " in out


def test_the_script_refuses_without_a_trace(
        tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _script().main(["--trace", str(tmp_path / "none.jsonl")]) == 1
    assert "capture one first" in capsys.readouterr().err


# -- the contract and its policy ----------------------------------------------

def test_the_contract_is_registered_and_its_files_are_classified() -> None:
    assert CA_PRICE_PERSISTENCE in FEATURE_CONTRACTS
    assert CA_PRICE_PERSISTENCE in REGISTERED_CONTRACTS
    assert CA_PRICE_PERSISTENCE.backtests == ()           # no gated fixture yet (OMNI-163)
    assert policy.classify("runtime/ca_price_persistence.py") is policy.ChangeTier.SOFT
    for path in ("specs/ca_price_persistence/oracle.py", "specs/ca_price_persistence/tests.py",
                 "sis/prediction_error.py"):
        assert policy.classify(path) is policy.ChangeTier.FORBIDDEN, path


def test_the_stub_passes_the_gauntlet() -> None:
    result = gauntlet.validate(STUB.read_text(encoding="utf-8"), contract=CA_PRICE_PERSISTENCE)
    assert result.passed, result.reason


def test_a_wrong_predictor_fails_acceptance() -> None:
    wrong = ("from typing import Any\n\n"
             "def predict_next_week(state: dict[str, Any]) -> float:\n"
             "    return 4.0\n")
    result = gauntlet.validate(wrong, contract=CA_PRICE_PERSISTENCE)
    assert not result.passed
