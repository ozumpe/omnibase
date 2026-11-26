"""OMNI-228: stop retraining that does not help, and page "likely missing input".

The pure decision first (N non-improving retrains stop; an improving one
resets the count; fewer than N never stop), then a trigger driven by a canned
error series through :class:`sis.futility.Guard`: it starts N retrains, pages
once, and starts no more until a reset or a new input.
"""

from __future__ import annotations

import datetime
import pathlib

import pytest

from sis import admin, config, futility, holdout, policy
from sis.adapters import InMemoryNotifier, InMemoryTelemetry
from sis.episodic import EpisodicEvent, JsonlEpisodicStore, ScoredWeek
from sis.futility import Guard, Rule, judge

C = "ca_price_wholesale"
BASIS = "inputs-v1"
RULE = Rule(retrains=3, min_improvement=0.05)


class Ticking:
    """A clock that moves a second each time it is read, so records order by ts."""

    def __init__(self) -> None:
        self._at = datetime.datetime(2026, 11, 26, tzinfo=datetime.UTC)

    def now(self) -> datetime.datetime:
        self._at += datetime.timedelta(seconds=1)
        return self._at


def _started(maes: list[float | None], *, contract: str = C, basis: str = BASIS
             ) -> list[EpisodicEvent]:
    return [EpisodicEvent(cycle_id=f"c{i}", ts=f"2026-11-{i + 1:02d}T00:00:00+00:00",
                          outcome=futility.RETRAIN_STARTED, contract=contract,
                          trigger="error_budget", trigger_mae=mae, trigger_basis=basis)
            for i, mae in enumerate(maes)]


# --- the pure decision ------------------------------------------------------------


def test_n_non_improving_retrains_stop() -> None:
    verdict = judge(_started([0.30, 0.30, 0.29]), C, basis=BASIS, current_mae=0.31, rule=RULE)
    assert verdict.stop and verdict.newly and verdict.streak == 3
    assert "likely missing input" in verdict.reason


def test_fewer_than_n_never_stop() -> None:
    for maes in ([], [0.30], [0.30, 0.31]):
        verdict = judge(_started(maes), C, basis=BASIS, current_mae=0.32, rule=RULE)
        assert not verdict.stop, maes


def test_an_improving_retrain_resets_the_count() -> None:
    # The second retrain cut the error 0.30 -> 0.20 (more than 5%): only the two
    # after it count, and two is fewer than three.
    verdict = judge(_started([0.40, 0.30, 0.20, 0.20]), C, basis=BASIS, current_mae=0.20,
                    rule=RULE)
    assert not verdict.stop and verdict.streak == 2


def test_a_cut_smaller_than_the_minimum_is_not_an_improvement() -> None:
    # 0.300 -> 0.290 is 3.3%, under the 5% minimum.
    verdict = judge(_started([0.300, 0.300, 0.300]), C, basis=BASIS, current_mae=0.290,
                    rule=RULE)
    assert verdict.stop
    assert futility.improved(0.30, 0.285, 0.05) is True
    assert futility.improved(0.30, 0.29, 0.05) is False
    assert futility.improved(None, 0.29, 0.05) is None


def test_the_latest_retrain_with_nothing_after_it_is_not_judged() -> None:
    verdict = judge(_started([0.30, 0.30, 0.30]), C, basis=BASIS, current_mae=None, rule=RULE)
    assert not verdict.stop and verdict.streak == 2


def test_other_contracts_and_other_bases_do_not_count() -> None:
    events = [*_started([0.3, 0.3, 0.3], contract="roman"),
              *_started([0.3, 0.3, 0.3], basis="inputs-v0")]
    assert not judge(events, C, basis=BASIS, current_mae=0.3, rule=RULE).stop


def test_a_reset_starts_the_count_again() -> None:
    events = [*_started([0.3, 0.3, 0.3]),
              EpisodicEvent(cycle_id="r", ts="2026-11-10T00:00:00+00:00",
                            outcome=futility.FUTILITY_RESET, contract=C)]
    assert not judge(events, C, basis=BASIS, current_mae=0.3, rule=RULE).stop


def test_a_recorded_stop_holds_without_a_new_page() -> None:
    events = [*_started([0.3, 0.3, 0.3]),
              EpisodicEvent(cycle_id="s", ts="2026-11-10T00:00:00+00:00",
                            outcome=futility.FUTILE_STOPPED, contract=C, trigger_basis=BASIS)]
    verdict = judge(events, C, basis=BASIS, current_mae=0.1, rule=RULE)
    assert verdict.stop and not verdict.newly
    # A new input is a new basis: the stop does not hold there.
    assert not judge(events, C, basis="inputs-v2", current_mae=0.3, rule=RULE).stop


@pytest.mark.parametrize("retrains, improvement", [(0, 0.05), (3, 1.0), (3, -0.1)])
def test_a_rule_out_of_range_is_refused(retrains: int, improvement: float) -> None:
    with pytest.raises(ValueError):
        Rule(retrains, improvement)


def test_worst_weeks_take_each_weeks_latest_revision() -> None:
    def row(week: str, error: float | None, revision: int = 0) -> ScoredWeek:
        return ScoredWeek(target="T", predictor="p", capture="x", week=week, at=week,
                          next_week=week, predicted=1.0, actual=None if error is None else 1.0,
                          actual_known_at=None, error=error, climatology=None,
                          climatology_error=None, skill=None, revision=revision)
    rows = [row("2026-01-05", 0.9), row("2026-01-05", 0.1, 1), row("2026-01-12", -0.5),
            row("2026-01-19", None), row("2026-01-26", 0.2),
            ScoredWeek(**{**row("2026-02-02", 5.0).__dict__, "target": "other"})]
    worst = futility.worst_weeks(rows, "T", k=2)
    assert [(w.week, w.error) for w in worst] == [("2026-01-12", -0.5), ("2026-01-26", 0.2)]


# --- the trigger, driven by a canned series --------------------------------------


def _guard(tmp_path: pathlib.Path) -> tuple[Guard, JsonlEpisodicStore, InMemoryNotifier]:
    store = JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    notifier = InMemoryNotifier(InMemoryTelemetry())
    guard = Guard(store, notifier, RULE, history_dir=tmp_path / "history", clock=Ticking())
    return guard, store, notifier


def _drive(guard: Guard, maes: list[float], *, basis: str = BASIS) -> list[str]:
    """A trigger that fires every tick with the window MAE *maes[i]*: what it did."""
    did = []
    for i, mae in enumerate(maes):
        verdict = guard.may_start(C, basis=basis, current_mae=mae, target="T")
        if verdict.stop:
            did.append("stopped")
            continue
        guard.started(C, cycle_id=f"{basis}-{i}", trigger="error_budget", basis=basis, mae=mae)
        did.append("started")
    return did


def test_the_trigger_starts_n_retrains_then_pages_once_and_stops(tmp_path: pathlib.Path
                                                                 ) -> None:
    guard, store, notifier = _guard(tmp_path)
    store.append_scored([ScoredWeek(
        target="T", predictor="p", capture="x", week="2026-10-05T00:00:00-04:00", at="",
        next_week="", predicted=5.0, actual=4.2, actual_known_at=None, error=0.8,
        climatology=None, climatology_error=None, skill=None)])
    did = _drive(guard, [0.30, 0.30, 0.31, 0.30, 0.29, 0.30])
    assert did == ["started"] * 3 + ["stopped"] * 3
    pages = notifier.sent()
    assert len(pages) == 1
    assert pages[0]["severity"] == "warning"
    assert pages[0]["title"] == f"likely missing input: {C}"
    assert "2026-10-05  error +0.8" in pages[0]["body"]
    assert "reset-futility" in pages[0]["body"]
    outcomes = [e.outcome for e in store.events()]
    assert outcomes.count(futility.FUTILE_STOPPED) == 1
    # No spend and no breaker count: none of these records cost anything, and
    # none is a judgement on the held-out fixture.
    assert all(e.cost_usd == 0 for e in store.events())
    assert not any(holdout.judged_holdout(e) for e in store.events())


def test_a_reset_or_a_new_input_lets_retrains_start_again(tmp_path: pathlib.Path) -> None:
    guard, store, notifier = _guard(tmp_path)
    assert _drive(guard, [0.3] * 4) == ["started"] * 3 + ["stopped"]
    assert _drive(guard, [0.3], basis="inputs-v2") == ["started"]
    futility.reset(store, C, "checked by hand: new wholesale input", clock=guard.clock)
    assert _drive(guard, [0.3, 0.3]) == ["started", "started"]
    assert len(notifier.sent()) == 1


def test_an_improving_series_never_stops(tmp_path: pathlib.Path) -> None:
    guard, _, notifier = _guard(tmp_path)
    assert _drive(guard, [0.40, 0.30, 0.20, 0.15, 0.10]) == ["started"] * 5
    assert notifier.sent() == []


def test_a_stop_survives_a_new_box(tmp_path: pathlib.Path) -> None:
    guard, store, _ = _guard(tmp_path)
    _drive(guard, [0.3] * 4)
    # A new box: its own empty store, and the earlier run's log in history.
    run = tmp_path / "history" / "run-1"
    run.mkdir(parents=True)
    (run / "episodic.jsonl").write_text(store.path.read_text())
    fresh = Guard(JsonlEpisodicStore(tmp_path / "new" / "episodic.jsonl"),
                  InMemoryNotifier(InMemoryTelemetry()), RULE, history_dir=tmp_path / "history")
    verdict = fresh.may_start(C, basis=BASIS, current_mae=0.3)
    assert verdict.stop and not verdict.newly


# --- the surfaces -----------------------------------------------------------------


def test_the_config_keys_and_the_policy_tier() -> None:
    assert config.get("brakes.futility_retrains") == 3
    assert config.get("brakes.futility_min_improvement") == 0.05
    assert Rule(int(config.get("brakes.futility_retrains")),
                float(config.get("brakes.futility_min_improvement")))
    assert policy.classify("sis/futility.py") is policy.ChangeTier.FORBIDDEN


def test_admin_reset_needs_a_contract_and_a_reason(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    store.append_scored([])
    monkeypatch.setattr("sis.episodic.get_episodic_store", lambda kind=None: store)
    monkeypatch.setattr("sis.holdout.HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(admin, "OPERATOR_AUDIT_JSONL", tmp_path / "audit.jsonl")
    for event in _started([0.3, 0.3, 0.3]):
        store.append(event)
    assert admin.reset_futility("nope", "a reason long enough to count")[0] == 1
    assert admin.reset_futility(C, "x")[0] == 1
    code, message = admin.reset_futility(C, "new input added, start counting again")
    assert code == 0 and "0 retrain(s) since the last reset" in message
    assert store.events()[-1].outcome == futility.FUTILITY_RESET
    assert (tmp_path / "audit.jsonl").exists()
    assert any(line.startswith(f"{C}: 0 retrain(s)") for line in admin.futility_lines())
    assert not admin.plan("reset-futility", {}, "a reason long enough to count").method
