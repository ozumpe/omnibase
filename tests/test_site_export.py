"""The landing page's door (OMNI-229): only public series, no gate reasons, named fields only."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from sis.episodic import ScoredWeek
from sis.paths import PROJECT_ROOT
from sis.prediction_error import CALIFORNIA
from sis.site_export import (
    LICENSED_SERIES,
    ExportRefused,
    check_public,
    cost,
    page_data,
    streak,
    tried,
    weeks,
)

LA_SPOT = "PET.EER_EPMRR_PF4_Y05LA_DPG.D"
SITE_DATA = PROJECT_ROOT / "site" / "data" / "site.json"


def _row(week: str, next_week: str, predicted: float, actual: float | None, *,
         target: str = CALIFORNIA, predictor: str = "abc", revision: int = 0,
         ts: str = "2026-11-27T00:00:00+00:00") -> ScoredWeek:
    return ScoredWeek(target=target, predictor=predictor, capture="c", week=week, at=week,
                      next_week=next_week, predicted=predicted, actual=actual,
                      actual_known_at=None, error=None, climatology=None,
                      climatology_error=None, skill=None, revision=revision, ts=ts)


def _page(records: list[dict[str, Any]], series: list[ScoredWeek] | None = None) -> dict[str, Any]:
    return page_data(records=records, series=series or [], contract="ca_price_wholesale",
                     target=CALIFORNIA, budget_usd=2.5, generated_at="2026-11-27T00:00:00+00:00",
                     code_version="v0.9.0", repo_url="https://example.invalid/r",
                     test_repo_url="https://example.invalid/t")


def test_la_spot_is_licensed() -> None:
    assert LA_SPOT in LICENSED_SERIES
    assert CALIFORNIA not in LICENSED_SERIES


def test_a_licensed_target_is_refused() -> None:
    with pytest.raises(ExportRefused, match="not a public series"):
        weeks([_row("2026-11-02", "2026-11-09", 2.7, 2.8, target=LA_SPOT)], target=LA_SPOT)


def test_page_data_naming_a_licensed_series_is_refused() -> None:
    with pytest.raises(ExportRefused, match="licensed"):
        check_public({"note": f"made from {LA_SPOT}"})


def test_persistence_is_the_week_before_and_the_latest_revision_wins() -> None:
    series = [
        _row("2026-11-02", "2026-11-09", 4.60, 4.70),
        _row("2026-11-09", "2026-11-16", 4.72, 4.75),
        _row("2026-11-09", "2026-11-16", 4.72, 4.74, revision=1),
    ]
    shown = weeks(series, target=CALIFORNIA)
    assert [w.week for w in shown] == ["2026-11-02", "2026-11-09"]
    assert shown[0].baseline is None
    assert shown[1].baseline == 4.70 and shown[1].actual == 4.74
    assert shown[1].beat_baseline is True


def test_only_the_latest_predictor_is_shown_by_default() -> None:
    series = [_row("2026-11-02", "2026-11-09", 1.0, 4.70, predictor="old", ts="2026-11-01"),
              _row("2026-11-02", "2026-11-09", 4.69, 4.70, predictor="new", ts="2026-11-27")]
    assert [w.predicted for w in weeks(series, target=CALIFORNIA)] == [4.69]


def test_streak_counts_the_latest_judged_weeks() -> None:
    series = [
        _row("2026-10-26", "2026-11-02", 4.00, 4.60),   # no baseline yet
        _row("2026-11-02", "2026-11-09", 4.00, 4.70),   # lost: baseline 4.60 was closer
        _row("2026-11-09", "2026-11-16", 4.74, 4.75),   # won
        _row("2026-11-16", "2026-11-23", 4.79, 4.80),   # won
        _row("2026-11-23", "2026-11-30", 4.85, None),   # not judged yet
    ]
    assert streak(weeks(series, target=CALIFORNIA)) == 2


def test_a_rejection_shows_its_gate_never_its_reason() -> None:
    reason = 'backtest failed — {"candidate_mae": 0.123456, "baseline_mae": 0.130001}'
    shown = tried([{"outcome": "rolled_back", "reject_gate": "backtest",
                    "reject_reason": reason, "cost_usd": 0.2, "contract": "ca_price_wholesale",
                    "ts": "t", "model": "secret-model", "output_tokens": 9}])
    text = json.dumps(shown)
    assert "0.123456" not in text and "0.130001" not in text and "mae" not in text
    assert shown[0]["verdict"] == "rejected"
    assert "baseline" in shown[0]["why"]
    assert set(shown[0]) == {"ts", "contract", "verdict", "why", "cost_usd", "pr"}


def test_an_unknown_gate_gets_a_generic_sentence() -> None:
    shown = tried([{"outcome": "rolled_back", "reject_gate": "new_gate",
                    "reject_reason": "error 0.42", "ts": "t"}])
    assert shown[0]["why"] == "A gate stopped it."


def test_cost_per_accepted_improvement() -> None:
    records = [{"outcome": "rolled_back", "cost_usd": 0.25},
               {"outcome": "verified_awaiting_human_merge", "cost_usd": 0.35}]
    assert cost(records, budget_usd=2.5) == {"spent_usd": 0.6, "budget_usd": 2.5,
                                             "accepted": 1, "per_accepted_usd": 0.6}
    assert cost(records[:1], budget_usd=2.5)["per_accepted_usd"] is None


def test_a_contract_without_a_plain_question_is_refused() -> None:
    with pytest.raises(ExportRefused, match="no plain question"):
        page_data(records=[], series=[], contract="sort", target=CALIFORNIA, budget_usd=1,
                  generated_at="", code_version="", repo_url="", test_repo_url="")


def test_other_contracts_records_are_left_out() -> None:
    data = _page([{"outcome": "rolled_back", "reject_gate": "benchmark", "contract": "sort",
                   "cost_usd": 1.0, "ts": "t"}])
    assert data["tried"] == [] and data["cost"]["spent_usd"] == 0


def test_the_committed_page_data_is_public() -> None:
    data = json.loads(SITE_DATA.read_text(encoding="utf-8"))
    check_public(data)
    assert set(data) == set(_page([]))
    for item in data["tried"]:
        assert set(item) == {"ts", "contract", "verdict", "why", "cost_usd", "pr"}


def test_the_page_reads_only_its_own_data_file() -> None:
    script = (PROJECT_ROOT / "site" / "app.js").read_text(encoding="utf-8")
    assert 'fetch("data/site.json"' in script
    assert "innerHTML" not in script


def test_the_export_script_writes_what_page_data_builds(tmp_path: pathlib.Path) -> None:
    import scripts.export_site_data as export

    log = tmp_path / "episodic.jsonl"
    log.write_text(json.dumps({"outcome": "verified_awaiting_human_merge", "pr_id": "24",
                               "contract": "ca_price_wholesale", "cost_usd": 0.35,
                               "ts": "2026-11-21T06:50:05+00:00"}) + "\n", encoding="utf-8")
    out = tmp_path / "site.json"
    assert export.main(["--episodic", str(log), "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["tried"][0]["pr"] == "24" and data["cost"]["accepted"] == 1
