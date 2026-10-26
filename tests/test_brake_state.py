"""OMNI-61: brake state fails closed, is written atomically, and has an operator.

No Ray here — everything that decides is pure or plain I/O. The live-actor half
(a CEO booting tripped, pause/resume on a running cycle) is in
``tests/test_ceo_state.py``, which owns a cluster.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sis import admin, config, org
from sis.atomic import write_text_atomic
from sis.episodic import JsonlEpisodicStore, StateUnreadable
from sis.loop import Action, Tick, Work, decide
from sis.roles import brake_persistence_problem, ensure_brake_persistence, unreadable_brake_state


@pytest.fixture(autouse=True)
def _isolate_config() -> Iterator[None]:
    saved = {key.env: os.environ.get(key.env) for key in config.SCHEMA}
    config.clear_cli_overrides()
    config.reset_config_cache()
    yield
    config.clear_cli_overrides()
    config.reset_config_cache()
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


# --- sis.atomic ------------------------------------------------------------------


def test_an_atomic_write_replaces_the_whole_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("old contents, longer than the new ones", encoding="utf-8")
    write_text_atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # no temp left over


@pytest.mark.parametrize("fails_at", ["fsync", "replace"])
def test_an_interrupted_write_leaves_the_old_file_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails_at: str
) -> None:
    # The acceptance criterion: old state or new state, never a partial file.
    target = tmp_path / "state.json"
    target.write_text('{"ceo": {"spent_usd": 3.25}}', encoding="utf-8")

    def crash(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(f"sis.atomic.os.{fails_at}", crash)
    with pytest.raises(OSError, match="simulated crash"):
        write_text_atomic(target, '{"ceo": {"spent_usd": 9.99}}')
    assert json.loads(target.read_text(encoding="utf-8")) == {"ceo": {"spent_usd": 3.25}}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


# --- the store: unreadable is not "nothing" --------------------------------------


def _store(tmp_path: Path) -> tuple[JsonlEpisodicStore, Path]:
    store = JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    return store, tmp_path / "episodic_state.json"


@pytest.mark.parametrize("contents", ['{"ceo": {"spent_usd": 3.2', "not json", "[1, 2]",
                                      '{"ceo": "a string"}'])
def test_unreadable_state_raises_instead_of_reading_as_empty(
    tmp_path: Path, contents: str
) -> None:
    store, state = _store(tmp_path)
    state.write_text(contents, encoding="utf-8")
    with pytest.raises(StateUnreadable):
        store.load_state("ceo")


def test_absent_state_is_still_just_none(tmp_path: Path) -> None:
    store, state = _store(tmp_path)
    assert store.load_state("ceo") is None
    state.write_text('{"other": {}}', encoding="utf-8")
    assert store.load_state("ceo") is None


def test_a_save_never_overwrites_state_it_could_not_read(tmp_path: Path) -> None:
    # It used to replace a corrupt file with {} — erasing the counted spend.
    store, state = _store(tmp_path)
    state.write_text('{"ceo": {"spent_usd": 3.2', encoding="utf-8")
    with pytest.raises(StateUnreadable):
        store.save_state("ceo", {"spent_usd": 0.0})
    assert state.read_text(encoding="utf-8") == '{"ceo": {"spent_usd": 3.2'


def test_an_interrupted_save_keeps_the_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    store.save_state("ceo", {"spent_usd": 1.5})

    def crash(*args: Any, **kwargs: Any) -> None:
        raise OSError("power cut")

    monkeypatch.setattr("sis.atomic.os.replace", crash)
    with pytest.raises(OSError):
        store.save_state("ceo", {"spent_usd": 2.5})
    assert store.load_state("ceo") == {"spent_usd": 1.5}


# --- OMNI-158 (H13): an older snapshot never replaces a newer one ---------------


def test_an_older_version_saved_last_leaves_the_newer_one(tmp_path: Path) -> None:
    # The reviewer's case: the loop saved $0.70 as version 8, then sis.admin
    # wrote its snapshot from before that cycle, $0.10 as version 7.
    store, _ = _store(tmp_path)
    assert store.save_state("ceo", {"spent_usd": 0.70, "version": 8}) is True
    assert store.save_state("ceo", {"spent_usd": 0.10, "version": 7}) is False
    assert store.load_state("ceo") == {"spent_usd": 0.70, "version": 8}
    # The same version again, or a later one, is saved.
    assert store.save_state("ceo", {"spent_usd": 0.70, "version": 8, "paused": "x"}) is True
    assert store.save_state("ceo", {"spent_usd": 0.75, "version": 9}) is True
    assert store.load_state("ceo") == {"spent_usd": 0.75, "version": 9}


def test_a_newer_version_from_another_process_is_kept(tmp_path: Path) -> None:
    import subprocess
    import sys

    store, _ = _store(tmp_path)
    store.save_state("ceo", {"spent_usd": 0.10, "version": 7})
    other = ("import pathlib, sys\nfrom sis.episodic import JsonlEpisodicStore\n"
             "store = JsonlEpisodicStore(pathlib.Path(sys.argv[1]))\n"
             "assert store.save_state('ceo', {'spent_usd': 0.70, 'version': 8})\n")
    subprocess.run([sys.executable, "-c", other, str(tmp_path / "episodic.jsonl")],
                   check=True, cwd=Path(__file__).resolve().parent.parent)
    assert store.save_state("ceo", {"spent_usd": 0.10, "version": 7}) is False
    assert store.load_state("ceo") == {"spent_usd": 0.70, "version": 8}


def test_the_read_and_the_write_of_a_save_hold_one_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another process's save cannot land between this one's read and write.
    import fcntl

    store, _ = _store(tmp_path)
    held: list[bool] = []
    real_read = JsonlEpisodicStore._read_blob

    def read_while_checking(self: JsonlEpisodicStore) -> dict[str, Any]:
        with open(tmp_path / "episodic_state.lock", "a", encoding="utf-8") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
        return real_read(self)

    monkeypatch.setattr(JsonlEpisodicStore, "_read_blob", read_while_checking)
    store.save_state("ceo", {"version": 1})
    store.save_state("ceo", {"version": 2})
    assert held and all(held)


def test_state_without_a_version_still_saves_last_write_wins(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    assert store.save_state("ceo", {"spent_usd": 1.0, "version": 3}) is True
    assert store.save_state("ceo", {"spent_usd": 2.0}) is True      # a state saved before H13
    assert store.save_state("pending_pr", {"pr": "1"}) is True       # other keys: no version
    assert store.save_state("pending_pr", {"pr": "2"}) is True
    assert store.load_state("ceo") == {"spent_usd": 2.0}
    assert store.load_state("pending_pr") == {"pr": "2"}


def test_versions_are_compared_only_within_one_lineage(tmp_path: Path) -> None:
    # A CEO that could not read its state starts a new lineage at version 0; a
    # file repaired meanwhile has its own numbers, which do not outrank it.
    store, _ = _store(tmp_path)
    store.save_state("ceo", {"spent_usd": 0.40, "version": 57, "lineage": "old"})
    assert store.save_state("ceo", {"spent_usd": 0.0, "version": 1, "lineage": "new"}) is True
    assert store.save_state("ceo", {"spent_usd": 0.4, "version": 0, "lineage": "new"}) is False
    assert store.load_state("ceo") == {"spent_usd": 0.0, "version": 1, "lineage": "new"}


def test_duckdb_keeps_the_newer_version_too(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    from sis.episodic import DuckDBEpisodicStore

    store = DuckDBEpisodicStore(tmp_path / "episodic.duckdb")
    assert store.save_state("ceo", {"spent_usd": 0.70, "version": 8}) is True
    assert store.save_state("ceo", {"spent_usd": 0.10, "version": 7}) is False
    assert store.load_state("ceo") == {"spent_usd": 0.70, "version": 8}
    assert store.save_state("ceo", {"spent_usd": 0.80, "version": 9}) is True
    assert store.load_state("ceo") == {"spent_usd": 0.80, "version": 9}


def test_the_fail_closed_state_holds_the_breaker_and_names_why() -> None:
    state = unreadable_brake_state("episodic_state.json: Expecting value")
    assert state["tripped"] is True
    assert "unreadable" in state["trip_reason"]
    assert "sis.admin reset-breaker" in state["trip_reason"]
    assert "spent_usd" not in state  # unknown, not zero


# --- episodic.store = none -------------------------------------------------------


@pytest.mark.parametrize(
    ("store", "proposer", "adapters", "refused"),
    [
        ("none", "stub", "memory", False),    # the default run and the test suite
        ("none", "claude", "memory", True),   # real money, no durable cap
        ("none", "stub", "real", True),       # real systems, no audit trail
        ("none", "claude", "real", True),
        ("jsonl", "claude", "real", False),
        ("duckdb", "claude", "real", False),
    ],
)
def test_none_is_refused_only_when_the_brakes_protect_something(
    store: str, proposer: str, adapters: str, refused: bool
) -> None:
    problem = brake_persistence_problem(store, proposer, adapters)
    assert (problem is not None) is refused
    if problem:
        assert "no durable spend cap" in problem and "no audit trail" in problem


def _configure(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for name in ("SIS_EPISODIC_STORE", "SIS_PROPOSER", "SIS_ADAPTERS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    config.reset_config_cache()


@pytest.mark.parametrize("env", [
    {"SIS_EPISODIC_STORE": "none", "SIS_PROPOSER": "claude"},
    {"SIS_EPISODIC_STORE": "none", "SIS_ADAPTERS": "real"},
])
def test_bootstrap_refuses_before_a_cluster_exists(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    _configure(monkeypatch, **env)

    def _no_cluster(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("ray.init reached: the refusal came too late")

    monkeypatch.setattr(org.ray, "init", _no_cluster)
    with pytest.raises(RuntimeError, match="episodic.store=none"):
        org.bootstrap()


def test_none_with_the_stub_and_memory_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, SIS_EPISODIC_STORE="none", SIS_PROPOSER="stub",
               SIS_ADAPTERS="memory")
    ensure_brake_persistence()


# --- the loop while paused -------------------------------------------------------


def test_a_paused_loop_idles_rather_than_stopping() -> None:
    work = Work("t", "b")
    assert decide(Tick(breaker_open=False, budget_ok=True, work=work, paused=True)) is Action.SKIP
    # An open breaker still stops it outright; a pause does not soften that.
    assert decide(Tick(breaker_open=True, budget_ok=True, work=work, paused=True)) is Action.STOP
    assert decide(Tick(breaker_open=False, budget_ok=True, work=work)) is Action.RUN


# --- sis.admin -------------------------------------------------------------------

_RUNNING: dict[str, Any] = {"tripped": False, "paused": None, "spent_usd": 0.4,
                            "consecutive_failures": 1.0, "accepted": 2}
_REASON = "looking into a spend spike"


def test_every_change_needs_a_reason_for_the_audit_log() -> None:
    for action in ("pause", "resume", "reset-breaker"):
        refused = admin.plan(action, _RUNNING, "because")
        assert refused.refused and refused.method is None


def test_status_changes_nothing_and_needs_no_reason() -> None:
    result = admin.plan("status", {**_RUNNING, "tripped": True, "trip_reason": "x"}, "")
    assert result.method is None and not result.refused
    assert "OPEN (x)" in result.message


def test_pause_and_resume() -> None:
    assert admin.plan("pause", _RUNNING, _REASON).method == "pause"
    assert admin.plan("resume", _RUNNING, _REASON).method is None  # not paused
    paused = {**_RUNNING, "paused": "earlier"}
    assert admin.plan("resume", paused, _REASON).method == "resume"
    still_tripped = admin.plan("resume", {**paused, "tripped": True}, _REASON)
    assert "still open" in still_tripped.message


def test_reset_breaker_only_when_open_and_warns_after_unreadable_state() -> None:
    assert admin.plan("reset-breaker", _RUNNING, _REASON).method is None
    tripped = {**_RUNNING, "tripped": True, "trip_reason": "consecutive failure threshold"}
    assert admin.plan("reset-breaker", tripped, _REASON).method == "reset_breaker"
    unreadable = {**_RUNNING, **unreadable_brake_state("corrupt")}
    plan = admin.plan("reset-breaker", unreadable, _REASON)
    assert plan.method == "reset_breaker"
    assert "move it aside" in plan.message


def test_an_unknown_action_is_refused() -> None:
    assert admin.plan("delete-budget", _RUNNING, _REASON).refused
