"""OMNI-198: the predictor scored after each capture, its error series kept in the episodic store.

What these pin: two captures score only the new weeks, and scoring twice after
one capture adds nothing; the series reads back after a restart, in both
backends; a later vintage of an actual is a revision row and a withdrawal is
one too, never an overwrite; a week that could not be scored is asked again;
a new predictor version has its own series; and the series feeds the Phase B
triggers as steps.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import pathlib
from collections.abc import Sequence
from typing import Any

import pytest

from sis import episodic, error_series, model_error, prediction_error
from sis.adapters import InMemoryTelemetry
from sis.adapters_real import EIA_RULES, eia_period, eia_published
from sis.episodic import JsonlEpisodicStore, NullEpisodicStore, ScoredWeek
from sis.paths import PROJECT_ROOT
from sis.prediction_error import Question
from sis.sensor_trace import Fetched, capture_lines, read_back

CA = prediction_error.CALIFORNIA
US = "PET.EMM_EPMRR_PTE_NUS_DPG.W"
UNIT = "Dollars per Gallon"
FETCHED = datetime.datetime(2026, 10, 15, 2, 0, tzinfo=datetime.UTC)
PROVENANCE = {"url": "https://www.eia.gov/opendata/bulk/PET.zip", "sha256": "ab" * 32,
              "attribution": "Source: U.S. Energy Information Administration (Oct 2026)"}
SOURCE = "def predict(state):\n    return 0.0\n"      # a version label; asking is in-process
OTHER = "def predict(state):\n    return 1.0\n"


def _mondays(first: str, n: int) -> list[datetime.date]:
    day = datetime.date.fromisoformat(first)
    return [day + datetime.timedelta(weeks=i) for i in range(n)]


DAYS = _mondays("2022-08-01", 14)
PRICES = {day: round(5.0 + 0.05 * i, 3) for i, day in enumerate(DAYS)}


def _capture(prior: list[dict[str, Any]], prices: dict[datetime.date, float | str], n: int
             ) -> list[dict[str, Any]]:
    """The block capture *n* appends: *prices* for California, a flat US series."""
    def series(values: dict[datetime.date, Any]) -> list[tuple[object, object]]:
        return [(eia_period(day.strftime("%Y%m%d")), value) for day, value in values.items()]

    fetched = FETCHED + datetime.timedelta(days=7 * n)
    return capture_lines(
        prior, [Fetched(CA, UNIT, series(prices)),
                Fetched(US, UNIT, series({day: 3.9 for day in prices}))],
        EIA_RULES, capture=f"eia-capture-{n}", fetched_at=fetched, published=eia_published,
        provenance=PROVENANCE)


def _persistence(state: dict[str, Any]) -> float:
    ca = [r for r in state["readings"] if r["series"] == CA]
    return float(max(ca, key=lambda r: r["event_time"])["value"])


class _Asker:
    """Asks the trusted persistence model in-process, and counts what it was asked."""

    def __init__(self) -> None:
        self.weeks: list[datetime.datetime] = []

    def __call__(self, asked: Sequence[Question]) -> list[Any]:
        self.weeks.extend(question.week for question in asked)
        return prediction_error.ask(_persistence, asked)


def _score(lines: list[dict[str, Any]], store: episodic.EpisodicStore, asker: _Asker, *,
           source: str = SOURCE, since: datetime.datetime | None = None,
           telemetry: InMemoryTelemetry | None = None) -> error_series.Scored:
    readings, absent = read_back(lines, EIA_RULES)
    return error_series.score_new(
        readings, absent, source=source, ask=asker, store=store,
        capture=error_series.last_capture(lines), unit=UNIT,
        since=since or datetime.datetime(2022, 1, 1, tzinfo=datetime.UTC),
        telemetry=telemetry)


# -- each week once ----------------------------------------------------------------

def test_two_captures_score_only_the_new_weeks(tmp_path: pathlib.Path) -> None:
    store = JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    lines = _capture([], {day: PRICES[day] for day in DAYS[:10]}, 0)
    asker = _Asker()
    one = _score(lines, store, asker)
    # Ten weeks known: nine have a next week to compare with.
    assert (one.new, one.revised) == (9, 0) and one.capture == "eia-capture-0"
    for n, known in ((1, 11), (2, 12)):
        lines = lines + _capture(lines, {day: PRICES[day] for day in DAYS[:known]}, n)
        asked_before = len(asker.weeks)
        later = _score(lines, store, asker)
        # One more week published: the week before it became scorable, only it.
        assert (later.new, later.revised, later.capture) == (1, 0, f"eia-capture-{n}")
        assert [w.date() for w in asker.weeks[asked_before:]] == [DAYS[known - 2]]
    rows = store.scored()
    assert [row.week[:10] for row in rows] == [d.isoformat() for d in DAYS[:11]]
    assert {row.capture for row in rows} == {f"eia-capture-{n}" for n in range(3)}
    assert all(row.revision == 0 and row.predictor == error_series.version(SOURCE)
               for row in rows)
    # Persistence's error is last week's change, signed.
    assert rows[0].error == pytest.approx(PRICES[DAYS[0]] - PRICES[DAYS[1]])


def test_a_catch_up_capture_scores_only_what_was_not_already_known(
        tmp_path: pathlib.Path) -> None:
    # Two weeks arrive in one capture: the first of them was asked with its
    # next week already known, so it is skipped, never scored (M34).
    store = JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    lines = _capture([], {day: PRICES[day] for day in DAYS[:10]}, 0)
    _score(lines, store, _Asker())
    lines = lines + _capture(lines, {day: PRICES[day] for day in DAYS[:12]}, 1)
    later = _score(lines, store, _Asker())
    assert later.new == 1
    assert DAYS[10].isoformat() not in {row.week[:10] for row in store.scored()}


def test_scoring_twice_after_one_capture_adds_nothing(tmp_path: pathlib.Path) -> None:
    store = JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    lines = _capture([], PRICES, 0)
    asker = _Asker()
    _score(lines, store, asker)
    held, asked = len(store.scored()), len(asker.weeks)
    again = _score(lines, store, asker)
    assert (again.new, again.revised, again.asked) == (0, 0, 0)
    assert len(store.scored()) == held and len(asker.weeks) == asked
    assert "0 new week(s), 0 revision(s)" in again.report()


def test_a_week_that_could_not_be_scored_is_asked_again(tmp_path: pathlib.Path) -> None:
    store = JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    lines = _capture([], PRICES, 0)
    readings, absent = read_back(lines, EIA_RULES)
    fails = DAYS[4]

    def flaky(asked: Sequence[Question]) -> list[Any]:
        return [RuntimeError("boom") if q.week.date() == fails else _persistence(q.state)
                for q in asked]

    error_series.score_new(readings, absent, source=SOURCE, ask=flaky, store=store,
                           capture="c0", since=datetime.datetime(2022, 1, 1, tzinfo=datetime.UTC))
    assert fails.isoformat() not in {row.week[:10] for row in store.scored()}
    asker = _Asker()
    again = _score(lines, store, asker)
    assert [w.date() for w in asker.weeks] == [fails] and again.new == 1


def test_the_first_scoring_starts_a_quarter_back(tmp_path: pathlib.Path) -> None:
    store = JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    days = _mondays("2022-01-03", 30)
    lines = _capture([], {day: 5.0 + i / 100 for i, day in enumerate(days)}, 0)
    readings, absent = read_back(lines, EIA_RULES)
    asker = _Asker()
    error_series.score_new(readings, absent, source=SOURCE, ask=asker, store=store, capture="c0")
    asked = prediction_error.questions(readings, absent)
    last = max(q.at for q in asked)
    assert asker.weeks and all(
        q.at >= last - datetime.timedelta(weeks=error_series.FIRST_WEEKS)
        for q in asked if q.week in asker.weeks)
    assert len(asker.weeks) <= error_series.FIRST_WEEKS + 1
    # Later scorings start where the series starts, not a quarter back again.
    assert error_series.default_since(store.scored(), asked, target=CA) == min(
        datetime.datetime.fromisoformat(row.at) for row in store.scored())


def test_a_new_predictor_version_has_its_own_series(tmp_path: pathlib.Path) -> None:
    store = JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    lines = _capture([], PRICES, 0)
    _score(lines, store, _Asker())
    other = _score(lines, store, _Asker(), source=OTHER)
    assert other.new == len(DAYS) - 1
    versions = {row.predictor for row in store.scored()}
    assert versions == {error_series.version(SOURCE), error_series.version(OTHER)}


# -- it survives a restart ---------------------------------------------------------

def test_a_restart_reads_the_series_back(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "episodic.jsonl"
    lines = _capture([], PRICES, 0)
    _score(lines, JsonlEpisodicStore(path), _Asker())
    reopened = JsonlEpisodicStore(path)
    rows = reopened.scored()
    assert len(rows) == len(DAYS) - 1
    asker = _Asker()
    assert _score(lines, reopened, asker).new == 0 and not asker.weeks
    # A torn last line (a crash mid-write) is skipped, not fatal.
    with reopened.scored_path.open("a", encoding="utf-8") as fh:
        fh.write('{"target": "PET')
    assert len(JsonlEpisodicStore(path).scored()) == len(rows)


def test_the_duckdb_backend_keeps_the_series(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("duckdb")
    path = tmp_path / "episodic.duckdb"
    lines = _capture([], PRICES, 0)
    store = episodic.DuckDBEpisodicStore(path)
    _score(lines, store, _Asker())
    held = store.scored()
    del store
    reopened = episodic.DuckDBEpisodicStore(path)
    assert reopened.scored() == held
    assert _score(lines, reopened, _Asker()).new == 0
    count = reopened.sql("SELECT COUNT(*) FROM scored_weeks")[0][0]
    assert count == len(DAYS) - 1


def test_the_null_store_keeps_nothing_and_score_trace_refuses_it(
        tmp_path: pathlib.Path) -> None:
    null = NullEpisodicStore()
    null.append_scored([])
    assert null.scored() == []
    with pytest.raises(ValueError, match="none"):
        error_series.score_trace(tmp_path / "eia.jsonl", rules=EIA_RULES,
                                 predictor=error_series.live_predictor(), store=null)


# -- revisions, never overwrites -----------------------------------------------------

def test_a_revision_is_a_new_row_and_a_withdrawal_is_one_too(tmp_path: pathlib.Path) -> None:
    store = JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    first = _capture([], PRICES, 0)
    _score(first, store, _Asker())
    before = {row.week[:10]: row for row in store.scored()}
    revised: dict[datetime.date, float | str] = dict(PRICES)
    revised[DAYS[4]] = 5.75          # week 3's actual changes
    revised[DAYS[7]] = "NA"          # week 6's actual is withdrawn
    lines = first + _capture(first, revised, 1)
    asker = _Asker()
    telemetry = InMemoryTelemetry()
    scored = _score(lines, store, asker, telemetry=telemetry)
    assert (scored.new, scored.revised) == (0, 2) and not asker.weeks
    rows = store.scored()
    assert len(rows) == len(before) + 2                 # appended, nothing overwritten
    old = before[DAYS[3].isoformat()]
    new = [row for row in rows if row.week == old.week and row.revision == 1]
    assert len(new) == 1
    (row,) = new
    assert row.note == "revised" and row.predicted == old.predicted
    assert row.actual == 5.75 and row.error == pytest.approx(old.predicted - 5.75)
    assert row.capture == "eia-capture-1" and row.actual_known_at is not None
    assert row.actual_known_at > (old.actual_known_at or "")
    (gone,) = (r for r in rows if r.week[:10] == DAYS[6].isoformat() and r.revision == 1)
    assert gone.note == "withdrawn" and gone.actual is None and gone.error is None
    # The latest row of each week is the one that counts.
    latest = error_series.latest(rows, target=CA, predictor=error_series.version(SOURCE))
    assert latest[old.week].revision == 1
    events = [e for e in telemetry.events() if e.get("event") == "prediction.error"]
    assert sorted(e["revision"] for e in events) == [1, 1]  # type: ignore[type-var]
    # A third scoring of the same trace revises nothing again.
    assert _score(lines, store, _Asker()).revised == 0


def test_the_running_skill_is_over_the_latest_row_of_each_week() -> None:
    def row(week: int, error: float, clim: float, revision: int = 0) -> ScoredWeek:
        day = datetime.datetime(2022, 8, 1, tzinfo=datetime.UTC) + datetime.timedelta(weeks=week)
        return ScoredWeek(target=CA, predictor="p", capture="c", week=day.isoformat(),
                          at=day.isoformat(), next_week=day.isoformat(), predicted=0.0,
                          actual=0.0, actual_known_at=None, error=error,
                          climatology=0.0, climatology_error=clim, skill=None,
                          revision=revision)

    held = {r.week: r for r in (row(0, 0.1, 0.2), row(1, 0.3, 0.2))}
    plan = error_series.Plan(new=(), revised=())
    assert error_series.rows(plan, [], target=CA, predictor="p", capture="c", known={},
                             held=held, ts="t") == []
    steps = error_series.as_steps(held.values(), target=CA, predictor="p")
    assert [s.error for s in steps] == [0.1, 0.3]


# -- what the triggers will read -----------------------------------------------------

def test_the_series_feeds_the_phase_b_triggers(tmp_path: pathlib.Path) -> None:
    store = JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    _score(_capture([], PRICES, 0), store, _Asker())
    steps = error_series.as_steps(store.scored(), target=CA,
                                  predictor=error_series.version(SOURCE))
    assert [s.week.date() for s in steps] == DAYS[:-1]
    window = model_error.latest_window(steps, 8)
    decision = model_error.error_budget(
        window, model_error.BudgetRule(mae_budget=0.01, min_scored=4, windows=1),
        consecutive=0, counted_through=None)
    assert decision.in_breach and decision.stats.scored == 8


# -- the scripts ---------------------------------------------------------------------

def _module(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        name, PROJECT_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_score_trace_runs_the_stub_in_the_sandbox(tmp_path: pathlib.Path) -> None:
    trace = tmp_path / "eia.jsonl"
    trace.write_text("".join(json.dumps(line, sort_keys=True) + "\n"
                             for line in _capture([], PRICES, 0)), encoding="utf-8")
    store = JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    scored = error_series.score_trace(trace, rules=EIA_RULES,
                                      predictor=error_series.live_predictor(), store=store,
                                      since=datetime.datetime(2022, 1, 1, tzinfo=datetime.UTC))
    assert scored.new == len(DAYS) - 1 and scored.capture == "eia-capture-0"
    assert scored.predictor == error_series.version(
        error_series.live_predictor().read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="no trace"):
        error_series.score_trace(tmp_path / "missing.jsonl", rules=EIA_RULES,
                                 predictor=error_series.live_predictor(), store=store)


def test_the_capture_script_has_help_and_never_captures_for_it(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    capture = _module("capture_sensor")

    def no_fetch(*_: object, **__: object) -> None:
        raise AssertionError("--help must not capture")

    monkeypatch.setattr(capture, "EIASensor", no_fetch)
    with pytest.raises(SystemExit) as exit_:
        capture.main(["--help"])
    assert exit_.value.code == 0 and "--no-score" in capsys.readouterr().out


def test_the_score_script_refuses_without_a_trace(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("SIS_EPISODIC_STORE", "jsonl")
    assert _module("score_capture").main(["--trace", str(tmp_path / "none.jsonl")]) == 1
    assert "no trace" in capsys.readouterr().err
