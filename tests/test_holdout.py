"""OMNI-207: the holdout budget (docs/OMNITRACK_VISION.md D5).

The pure count first: what is a judgement, keyed by which fixture. Then the
surfaces it reaches: the contract field, the config key, the policy tier, the
console line, the scheduled runner, ``sis.admin``.
"""

from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import replace
from typing import Any

import pytest

from sis import admin, config, holdout, org, policy
from sis.backtest import Backtest, Split
from sis.contract import ROMAN, FeatureContract
from sis.episodic import EpisodicEvent, JsonlEpisodicStore, event_from_cycle_result
from sis.feature import resubmitted

A = "a" * 64
B = "b" * 64


def _private(name: str, fixture: str = A, expect: str = B) -> Backtest:
    return Backtest(name=name, fixture=f"specs/x/private/{name}.json",
                    expect=f"specs/x/private/{name}.expect.json",
                    fixture_sha256=fixture, expect_sha256=expect)


def _never(path: str) -> bytes:
    raise AssertionError(f"a private episode's file was read: {path}")


def _event(cycle: str = "c1", *, outcome: str = "rolled_back", reason: str | None = None,
           digest: str | None = "d", contract: str = "k",
           candidate_sha: str | None = "sha") -> EpisodicEvent:
    recorded = event_from_cycle_result(
        {"status": outcome, "reason": reason, "contract": contract,
         "holdout_digest": digest, "candidate_sha": candidate_sha})
    return replace(recorded, cycle_id=cycle)


# --- the fixture's digest ----------------------------------------------------


def test_a_contract_without_held_out_episodes_has_no_digest() -> None:
    train = replace(_private("t"), split=Split.TRAIN)
    assert holdout.fixture_digest((), _never) is None
    assert holdout.fixture_digest((train,), _never) is None


def test_the_digest_is_the_data_not_its_order_and_reads_no_private_file() -> None:
    one, two = _private("w1"), _private("w2", fixture="c" * 64)
    assert (holdout.fixture_digest((one, two), _never)
            == holdout.fixture_digest((two, one), _never))


def test_a_changed_episode_is_a_new_fixture() -> None:
    before = holdout.fixture_digest((_private("w1"),), _never)
    assert holdout.fixture_digest((_private("w1", expect="e" * 64),), _never) != before
    assert holdout.fixture_digest((_private("w1"), _private("w2")), _never) != before


def test_a_public_episode_is_hashed_from_its_files() -> None:
    files = {"f.json": b"1", "e.json": b"2"}
    public = Backtest(name="p", fixture="f.json", expect="e.json")
    first = holdout.fixture_digest((public,), lambda p: files[p])
    files["e.json"] = b"3"
    assert holdout.fixture_digest((public,), lambda p: files[p]) != first


# --- what is a judgement -----------------------------------------------------


@pytest.mark.parametrize(("outcome", "reason", "judged"), [
    ("verified_awaiting_human_merge", None, True),
    ("feature_step", None, True),
    ("rolled_back", "backtest failed: candidate did not beat the baseline on held-out "
                    "history (mean absolute error not far enough below it) — n", True),
    ("qa_rejected", "backtest failed: candidate did not reproduce recorded history", True),
    ("rolled_back", "slo exceeded: p95 2.0s over 1.0s", True),
    ("review_requested", "review requested: name the constant", True),
    ("no_gain", "already built: every gate passes", True),
    ("rolled_back", "backtest gate timed out", True),
    ("rolled_back", "tampered: the candidate changed the exam files while the backtest "
                    "gate ran (x)", True),
    ("rolled_back", "harness: the slo gate raised (OSError: x)", True),
    # Never reached the backtest gate, or no verdict there.
    ("rolled_back", "acceptance tests failed", False),
    ("rolled_back", "mypy --strict failed", False),
    ("rolled_back", "interface: candidate does not export 'f'", False),
    ("rolled_back", "invariant violated in sandbox: plausible", False),
    ("rolled_back", "SyntaxError: bad", False),
    ("rolled_back", "mypy gate timed out", False),
    ("rolled_back", "tampered: the candidate changed the exam files while the mypy "
                    "gate ran (x)", False),
    ("rolled_back", "harness: the backtest gate raised (ValueError: x)", False),
    ("rolled_back", "harness: backtest 'w1' references a missing file — the backtest "
                    "gate cannot run", False),
    ("rolled_back", "harness: the sandbox failed its self-check after the backtest gate "
                    "failed — x; the candidate was not judged", False),
    ("rolled_back", "proposer: the proposal stopped at the output limit", False),
    ("paused", None, False),
    ("budget_denied", None, False),
    ("holdout_spent", None, False),
])
def test_only_a_verdict_of_the_backtest_gate_is_a_judgement(
    outcome: str, reason: str | None, judged: bool,
) -> None:
    assert holdout.judged_holdout(_event(outcome=outcome, reason=reason)) is judged


def test_a_resubmission_was_not_judged_again() -> None:
    again = resubmitted("backtest failed: candidate did not reproduce recorded history")
    assert holdout.judged_holdout(_event(reason=again)) is False


def test_an_exception_counts_only_once_a_candidate_existed() -> None:
    assert holdout.judged_holdout(_event(outcome="error", reason="error: X: y")) is True
    assert holdout.judged_holdout(
        _event(outcome="error", reason="error: X: y", candidate_sha=None)) is False


# --- the count ---------------------------------------------------------------


_FAIL = "backtest failed: candidate did not reproduce recorded history"


def test_the_count_is_per_contract_and_per_fixture() -> None:
    events = [
        _event("1", reason=_FAIL),
        _event("2", outcome="verified_awaiting_human_merge"),
        _event("3", reason="mypy --strict failed"),          # never reached it
        _event("4", reason=_FAIL, contract="other"),          # another contract
        _event("5", reason=_FAIL, digest="new"),               # a re-promoted fixture
        _event("6", reason=_FAIL, digest=None),                # before OMNI-207
    ]
    assert holdout.spent(events, "k", "d") == 2
    assert holdout.spent(events, "k", "new") == 1


def test_a_cycle_logged_twice_is_counted_once() -> None:
    twice = _event("1", reason=_FAIL)
    assert holdout.spent([twice, twice, _event("2", reason=_FAIL)], "k", "d") == 2


def test_the_contract_declares_the_limit_else_the_default() -> None:
    held = (_private("w1"),)
    events = [_event(str(n), reason=_FAIL,
                     digest=holdout.fixture_digest(held, _never)) for n in range(3)]
    own = holdout.budget("k", held, 3, 50, events, _never)
    assert own is not None and own.exhausted and own.remaining == 0
    default = holdout.budget("k", held, None, 50, events, _never)
    assert default is not None and (default.used, default.limit) == (3, 50)
    assert not default.exhausted
    assert holdout.budget("k", (), 3, 50, events, _never) is None


def test_the_page_and_the_line_name_no_held_out_value() -> None:
    b = holdout.Budget("ca_price_wholesale", "f" * 64, 50, 50)
    assert "50 of 50" in b.describe() and "ffffffffffff" in b.describe()
    page = holdout.refusal(b)
    assert "D5" in page and "nothing was spent" in page and "$" not in page


def test_earlier_runs_logs_are_read_and_a_copy_of_this_boxs_is_not_double_counted(
    tmp_path: pathlib.Path,
) -> None:
    local = JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    here = _event("here", reason=_FAIL)
    local.append(here)
    earlier = JsonlEpisodicStore(tmp_path / "history" / "20261113-1500" / "episodic.jsonl")
    earlier.append(_event("then", reason=_FAIL))
    earlier.append(here)  # this box uploaded its own log before
    events = holdout.recorded_events(local, tmp_path / "history")
    assert len(events) == 3
    assert holdout.spent(events, "k", "d") == 2
    assert holdout.history_events(tmp_path / "absent") == []


# --- where the budget is declared and protected -------------------------------


def test_the_cycle_record_carries_the_fixture_digest() -> None:
    assert _event(digest="d").holdout_digest == "d"
    assert event_from_cycle_result({"status": "paused"}).holdout_digest is None


@pytest.mark.parametrize("bad", [0, -1, True, 2.5])
def test_a_contract_refuses_a_budget_that_is_not_a_positive_whole_number(bad: Any) -> None:
    with pytest.raises(ValueError, match="holdout_budget"):
        replace(ROMAN, holdout_budget=bad)
    assert isinstance(replace(ROMAN, holdout_budget=7), FeatureContract)


def test_the_default_is_a_forbidden_config_key() -> None:
    key = next(k for k in config.SCHEMA if k.path == "brakes.holdout_budget")
    assert key.tier is config.ConfigTier.FORBIDDEN and key.positive
    assert isinstance(config.get("brakes.holdout_budget"), int)


def test_the_loop_cannot_edit_how_judgements_are_counted() -> None:
    assert policy.classify("sis/holdout.py") is policy.ChangeTier.FORBIDDEN


# --- what a human sees ---------------------------------------------------------


def test_each_cycle_line_reports_the_budget_left() -> None:
    line = org.cycle_summary({"status": "rolled_back", "reason": _FAIL,
                              "holdout": {"remaining": 41, "limit": 50}})
    assert "holdout 41 of 50 left" in line
    refused = org.cycle_summary({"status": "holdout_spent",
                                 "holdout": {"remaining": 0, "limit": 50}})
    assert "holdout budget spent, no cycle ran" in refused
    assert "0 of 50 left" not in refused


def test_admin_shows_the_budgets_without_a_running_loop(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sis import episodic

    monkeypatch.setattr(episodic, "get_episodic_store",
                        lambda kind=None: JsonlEpisodicStore(tmp_path / "e.jsonl"))
    monkeypatch.setattr(holdout, "HISTORY_DIR", tmp_path / "history")
    assert admin.plan("holdout", {}, "").method is None
    assert admin.main(["holdout"]) == 0
    out = capsys.readouterr().out
    # ca_price_wholesale is registered when its private index is committed.
    from sis.contract import CA_PRICE_WHOLESALE
    if CA_PRICE_WHOLESALE is not None:
        assert "holdout budget for ca_price_wholesale: 0 of" in out
    else:
        assert "no registered contract has held-out backtests" in out


def test_the_scheduled_run_fetches_earlier_logs_and_fails_closed() -> None:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
    import aws_scheduled_run as sr

    script = sr.runner_script(sr.RunConfig(contract="ca_price_wholesale",
                                           run_id="20261121T000000Z"))
    fetch = script.index("runtime/history/")
    assert fetch < script.index("main.py"), "fetched before the loop starts"
    assert "--include '*/episodic.jsonl'" in script
    assert "--exclude '*'" in script, "only the episodic logs, nothing else from runs/"
    assert "[scheduled] 20261121T000000Z exit 1" in script[fetch:script.index("main.py")]


def test_fetched_logs_are_never_uploaded_again(tmp_path: pathlib.Path) -> None:
    from sis.artifact_sync import files_to_sync

    (tmp_path / "history" / "r").mkdir(parents=True)
    (tmp_path / "history" / "r" / "episodic.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "episodic.jsonl").write_text(json.dumps({}) + "\n", encoding="utf-8")
    assert files_to_sync(tmp_path) == [tmp_path / "episodic.jsonl"]


def test_the_loop_stops_after_a_refused_cycle_rather_than_polling_into_it() -> None:
    from sis.loop import holdout_stop_note, stop_summary

    note = holdout_stop_note({"status": "holdout_spent", "contract": "ca_price_wholesale",
                              "holdout": {"used": 50}})
    assert note == "ca_price_wholesale has used its holdout budget (50 judgement(s))"
    assert holdout_stop_note({"status": "rolled_back"}) is None
    line = stop_summary(None, 1, max_cycles=10, interrupted=True, trip_reason=None,
                        spent_usd=0.0, budget_usd=1.0, converged_note=note)
    assert "holdout budget" in line and "interrupted" not in line
