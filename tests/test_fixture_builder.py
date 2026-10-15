"""OMNI-35: backtest fixtures built from a sensor trace, checked against release evidence.

No network. The trace is built through the real capture path
(``sensor_trace.capture_lines``) from synthetic weekly series that carry the
autumn-2022 values the ticket verified against EIA's data (California price
$5.053 on 5 Sep and $6.213 on 3 Oct, US $4.026 and $4.188, PADD 5 stocks 29,192
on 5 Aug, the 24,675 low on 30 Sep and 29,943 on 25 Nov); the weeks between are
made up for the test. The real fixture is built from the box's capture.
"""

from __future__ import annotations

import datetime
import json
import pathlib
from typing import Any

import pytest

from sis import gauntlet
from sis.adapters_real import EASTERN, EIA_RULES, eia_period, eia_published
from sis.backtest import Backtest, parse_expectation, parse_fixture
from sis.contract import FeatureContract
from sis.fixture_builder import CALENDAR_FILE, FixtureRefused, Question, ReleaseCalendar, build
from sis.sensor_trace import Fetched, capture_lines

STOCKS = "PET.WGTSTP51.W"
CA = "PET.EMM_EPMRR_PTE_SCA_DPG.W"
US = "PET.EMM_EPMRR_PTE_NUS_DPG.W"
FETCHED = datetime.datetime(2026, 10, 15, 2, 0, tzinfo=datetime.UTC)
QUESTION = Question(target=CA, first=datetime.date(2022, 8, 8), last=datetime.date(2022, 11, 28))

CA_PRICES = {  # Mondays
    "2022-08-01": 5.32, "2022-08-08": 5.21, "2022-08-15": 5.12, "2022-08-22": 5.08,
    "2022-08-29": 5.06, "2022-09-05": 5.053, "2022-09-12": 5.18, "2022-09-19": 5.39,
    "2022-09-26": 5.60, "2022-10-03": 6.213, "2022-10-10": 6.30, "2022-10-17": 6.05,
    "2022-10-24": 5.72, "2022-10-31": 5.45, "2022-11-07": 5.30, "2022-11-14": 5.18,
    "2022-11-21": 5.02, "2022-11-28": 4.813,
}
US_PRICES = {"2022-09-05": 4.026, "2022-10-03": 4.188, "2022-11-28": 3.848}
STOCK_LEVELS = {"2022-08-05": 29192, "2022-09-30": 24675, "2022-11-25": 29943}


def _days(first: str, last: str, step: int = 7) -> list[datetime.date]:
    day, end = datetime.date.fromisoformat(first), datetime.date.fromisoformat(last)
    out = []
    while day <= end:
        out.append(day)
        day += datetime.timedelta(days=step)
    return out


MONDAYS = _days("2021-06-07", "2022-12-26")
FRIDAYS = _days("2021-06-04", "2022-12-30")


def _series(days: list[datetime.date], known: dict[str, float],
            default: float) -> list[tuple[object, object]]:
    return [(eia_period(day.strftime("%Y%m%d")), known.get(day.isoformat(), default))
            for day in days]


def _trace() -> list[dict[str, Any]]:
    return capture_lines(
        [], [Fetched(STOCKS, "Thousand Barrels", _series(FRIDAYS, STOCK_LEVELS, 27000.0)),
             Fetched(CA, "Dollars per Gallon", _series(MONDAYS, CA_PRICES, 4.60)),
             Fetched(US, "Dollars per Gallon", _series(MONDAYS, US_PRICES, 3.90))],
        EIA_RULES, capture="eia-20261015T020000.000000Z-abcdef01", fetched_at=FETCHED,
        published=eia_published,
        provenance={"url": "https://www.eia.gov/opendata/bulk/PET.zip", "sha256": "ab" * 32,
                    "attribution": "Source: U.S. Energy Information Administration (Oct 2026)"})


def _calendar(*, checked_by: str = "test", wpsr: dict[str, str] | None = None,
              gdu: dict[str, str] | None = None, drop: str | None = None) -> ReleaseCalendar:
    stocks = {d.isoformat(): (d + datetime.timedelta(days=5)).isoformat() for d in FRIDAYS}
    prices = {d.isoformat(): d.isoformat() for d in MONDAYS}
    stocks.update({"2022-06-17": "2022-06-29", "2022-09-02": "2022-09-08",
                   "2022-10-07": "2022-10-13"})
    prices.update({"2022-06-20": "2022-06-29", "2022-06-27": "2022-06-29",
                   "2022-09-05": "2022-09-06", "2022-10-10": "2022-10-11"})
    stocks.update(wpsr or {})
    prices.update(gdu or {})
    if drop:
        prices.pop(drop)
    return ReleaseCalendar.parse(json.dumps({
        "schema": 1, "timezone": "America/New_York",
        "checked_by": checked_by, "checked_on": "2026-10-15",
        "reports": {"wpsr": {"series": [STOCKS], "releases": stocks},
                    "gdu": {"series": [CA, US], "releases": prices}},
    }, sort_keys=True))


def _build(calendar: ReleaseCalendar | None = None) -> dict[str, str]:
    return build(_trace(), EIA_RULES, calendar or _calendar(), QUESTION,
                 name="ca_price_next_week", spec_ref="OMNI-35").files


def _episode(files: dict[str, str], week: str) -> tuple[dict[str, Any], float]:
    fixture = json.loads(files[f"backtests/{week}_fixture.json"])
    return fixture["args"][0], json.loads(files[f"backtests/{week}_expect.json"])["value"]


def _latest(state: dict[str, Any], series: str) -> dict[str, Any]:
    return max((r for r in state["readings"] if r["series"] == series),
               key=lambda r: r["event_time"])


# --- the shape the gate consumes -----------------------------------------------------


def test_one_held_out_episode_per_week_asked_about() -> None:
    files = _build()
    weeks = sorted(f.split("/")[1].removesuffix("_fixture.json")
                   for f in files if f.endswith("_fixture.json"))
    assert weeks[0] == "2022-08-08" and weeks[-1] == "2022-11-28" and len(weeks) == 17
    for week in weeks:
        fixture = parse_fixture(files[f"backtests/{week}_fixture.json"], where=week)
        assert fixture.event_time is not None and len(fixture.args) == 1
        parse_expectation(files[f"backtests/{week}_expect.json"], where=week)
    assert CALENDAR_FILE in files and "17 episodes" in files["README.md"]
    assert "Source: U.S. Energy Information Administration (Oct 2026)" in files["README.md"]


def test_the_answer_is_next_weeks_value_and_the_question_comes_before_it() -> None:
    state, answer = _episode(_build(), "2022-10-03")
    assert answer == 6.213
    # Asked when the 26 Sep price became known: reconstructed Wednesday 17:00 ET.
    assert state["as_of"] == datetime.datetime(2022, 9, 28, 17, tzinfo=EASTERN).isoformat()
    assert _latest(state, CA)["value"] == 5.60
    assert all(r["event_time"] < "2022-10-03" for r in state["readings"] if r["series"] == CA)


def test_no_reading_is_shown_before_it_was_known() -> None:
    files = _build()
    for name, text in files.items():
        if not name.endswith("_fixture.json"):
            continue
        fixture = json.loads(text)
        at = datetime.datetime.fromisoformat(fixture["event_time"])
        floor = at - datetime.timedelta(weeks=54)
        for item in [*fixture["args"][0]["readings"], *fixture["args"][0]["absent"]]:
            assert datetime.datetime.fromisoformat(item["known_at"]) <= at, (name, item)
            assert datetime.datetime.fromisoformat(item["event_time"]) >= floor, (name, item)


def test_the_stock_low_is_not_known_on_the_monday_after_it() -> None:
    # The week ending Fri 30 Sep was published the next Wednesday, after the
    # Monday 3 Oct price: by known_at, the question about 10 Oct cannot see it.
    state, _ = _episode(_build(), "2022-10-10")
    assert all(r["value"] != 24675 for r in state["readings"])
    assert _latest(state, STOCKS)["event_time"].startswith("2022-09-23")
    state, _ = _episode(_build(), "2022-10-17")
    assert _latest(state, STOCKS)["value"] == 24675


def test_the_same_trace_builds_byte_identical_files() -> None:
    assert _build() == _build()


# --- release evidence ---------------------------------------------------------------


def test_a_delayed_release_is_known_late_whatever_the_schedule_said() -> None:
    # Reconstructed: the 16 Sep stocks by Thu 22 Sep, so seen at the question
    # asked on Wed 28 Sep. Released (per the calendar) on Thu 29 Sep: not seen.
    on_time, _ = _episode(_build(), "2022-10-03")
    assert _latest(on_time, STOCKS)["event_time"].startswith("2022-09-16")
    late, _ = _episode(_build(_calendar(wpsr={"2022-09-16": "2022-09-29"})), "2022-10-03")
    assert _latest(late, STOCKS)["event_time"].startswith("2022-09-09")
    shown = [r for r in late["readings"] if r["event_time"].startswith("2022-09-16")]
    assert shown == []


def test_a_reading_the_calendar_does_not_cover_refuses_the_build() -> None:
    with pytest.raises(FixtureRefused, match="does not cover"):
        _build(_calendar(drop="2022-09-12"))


def test_a_calendar_no_human_has_checked_refuses_the_build() -> None:
    with pytest.raises(FixtureRefused, match="not been checked by a human"):
        _calendar(checked_by="")


# --- the gate, on the built fixture -----------------------------------------------


def _gate(tmp_path: pathlib.Path, files: dict[str, str], candidate: str) -> gauntlet.Result | None:
    root = tmp_path / "specs"
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text, encoding="utf-8")
    weeks = sorted(f.removesuffix("_fixture.json") for f in files if f.endswith("_fixture.json"))
    contract = FeatureContract(
        name="ca_price_next_week", spec_ref="OMNI-35", entry="predict",
        entry_module="runtime/ca_price_next_week.py",
        acceptance_tests="specs/ca_price_next_week/tests.py", public_api=("predict",),
        backtests=tuple(Backtest(name=week.split("/")[1],
                                 fixture=str(root / f"{week}_fixture.json"),
                                 expect=str(root / f"{week}_expect.json")) for week in weeks))
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "target.py").write_text(candidate, encoding="utf-8")
    (sandbox / "sitecustomize.py").write_text(gauntlet._NETWORK_GUARD, encoding="utf-8")
    return gauntlet._gate_backtest(gauntlet._GateContext(
        contract=contract, code_str=candidate, tmp=sandbox, tmpdir=str(sandbox),
        env=gauntlet._sandbox_env(home=str(sandbox), pythonpath=str(sandbox)),
        candidate=sandbox / "target.py"))


def test_the_gate_accepts_a_model_that_reproduces_the_episode_and_rejects_persistence(
        tmp_path: pathlib.Path) -> None:
    files = _build()
    answers = {json.loads(files[f])["event_time"]:
               json.loads(files[f.replace("_fixture", "_expect")])["value"]
               for f in files if f.endswith("_fixture.json")}
    lookup = f"ANSWERS = {answers!r}\n\n\ndef predict(state):\n    return ANSWERS[state['as_of']]\n"
    assert _gate(tmp_path / "lookup", files, lookup) is None
    persistence = (f"def predict(state):\n    prices = [r for r in state['readings'] "
                   f"if r['series'] == {CA!r}]\n"
                   "    return max(prices, key=lambda r: r['event_time'])['value']\n")
    result = _gate(tmp_path / "persistence", files, persistence)
    assert result is not None and not result.passed
    assert "holdout" in result.reason
    # Held out: the reason names the episode, never the recorded price (OMNI-162).
    assert "6.213" not in result.reason and "expected" not in result.reason


# --- the committed calendar, and the script ------------------------------------------

CALENDAR_DRAFT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "data" / \
    "eia_release_calendar_2021_2022.json"
LA, SF = "PET.EMM_EPMRR_PTE_Y05LA_DPG.W", "PET.EMM_EPMRR_PTE_Y05SF_DPG.W"


def _checked(raw: str, *, by: str = "test") -> str:
    data = json.loads(raw)
    data.update(checked_by=by, checked_on="2026-10-15" if by else "")
    return json.dumps(data)


def test_the_committed_calendar_covers_every_week_a_fixture_can_show() -> None:
    calendar = ReleaseCalendar.parse(_checked(CALENDAR_DRAFT.read_text(encoding="utf-8")))
    first_shown = QUESTION.first - QUESTION.horizon - QUESTION.lookback
    for series, days in ((STOCKS, FRIDAYS), (CA, MONDAYS), (LA, MONDAYS), (SF, MONDAYS),
                         (US, MONDAYS)):
        for day in days:
            if first_shown <= day <= QUESTION.last:
                released = calendar.released(series, eia_period(day.strftime("%Y%m%d")))
                assert released is not None, (series, day)
                assert 0 <= (released.date() - day).days <= 12, (series, day, released)
    # The one irregular delay in the window, as EIA's archive records it.
    released = calendar.released(STOCKS, eia_period("20220617"))
    assert released is not None and released.date() == datetime.date(2022, 6, 29)


def _script() -> Any:
    import importlib.util

    path = CALENDAR_DRAFT.parent.parent / "build_fixture.py"
    spec = importlib.util.spec_from_file_location("build_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_builds_from_a_trace_and_stages_it_for_a_human(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    from sis import contract_author

    monkeypatch.setattr(contract_author, "CONTRACT_STAGING_DIR", tmp_path / "staging")
    trace = tmp_path / "eia.jsonl"
    trace.write_text("".join(json.dumps(line, sort_keys=True) + "\n" for line in _trace()),
                     encoding="utf-8")
    raw = CALENDAR_DRAFT.read_text(encoding="utf-8")
    unchecked, checked = tmp_path / "unchecked.json", tmp_path / "checked.json"
    unchecked.write_text(_checked(raw, by=""), encoding="utf-8")
    checked.write_text(_checked(raw), encoding="utf-8")
    script = _script()

    assert script.main(["--trace", str(trace), "--calendar", str(unchecked)]) == 1
    assert "not been checked by a human" in capsys.readouterr().err
    assert script.main(["--trace", str(tmp_path / "none.jsonl"),
                        "--calendar", str(checked)]) == 1
    assert script.main(["--trace", str(trace), "--calendar", str(checked)]) == 0
    out = capsys.readouterr().out
    staged = contract_author.load_staged("ca_price_next_week")
    assert f"digest: {staged.digest}" in out and "17 episodes" in out
    assert not (pathlib.Path(contract_author.SPECS_DIR) / "ca_price_next_week").exists()
