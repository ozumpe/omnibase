"""OMNI-82 (L37): the preflight checks the proposer's key and model. No network.

The scheduled AWS run of 2026-10-29 (``runs/20261029-1457/``) started with an
Anthropic key the API rejected: ``check_connections.py --deep`` passed, and
all three cycles failed with ``AuthenticationError: 401`` after filing their
specs, epics and stories. The runner ends a run before any spend when the
preflight fails, so the preflight is where a bad key or model must show.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from sis import config, llm
from sis.paths import PROJECT_ROOT


def _check_connections() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_connections", PROJECT_ROOT / "scripts" / "check_connections.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Rejected(Exception):
    """What the SDK raises for a key it does not know (``AuthenticationError``)."""


def _fake_anthropic(retrieve: Any) -> SimpleNamespace:
    return SimpleNamespace(Anthropic=lambda: SimpleNamespace(
        models=SimpleNamespace(retrieve=retrieve)))


@pytest.fixture
def real_proposer(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SIS_PROPOSER", "claude")
    monkeypatch.delenv("SIS_LLM_MODEL", raising=False)
    config.reset_config_cache()
    yield
    config.reset_config_cache()


def test_a_rejected_key_fails_the_preflight(
    real_proposer: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def retrieve(model: str) -> Any:
        raise _Rejected("Error code: 401 - invalid x-api-key")

    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(retrieve))
    assert _check_connections().check_llm() is False
    out = capsys.readouterr().out
    assert "LLM" in out and "invalid x-api-key" in out


def test_a_known_key_and_model_pass_the_preflight(
    real_proposer: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    asked: list[str] = []

    def retrieve(model: str) -> Any:
        asked.append(model)
        return SimpleNamespace(id=model, display_name="Claude Opus 4.8")

    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(retrieve))
    assert _check_connections().check_llm() is True
    assert asked == [llm.DEFAULT_ANTHROPIC_MODEL], "the model the proposer would call"
    assert "Claude Opus 4.8" in capsys.readouterr().out


def test_an_unpriced_model_fails_the_preflight_without_a_call(
    real_proposer: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The loop would refuse it at the first cycle (L25): the preflight says so first.
    monkeypatch.setenv("SIS_LLM_MODEL", "claude-unpriced-0")
    config.reset_config_cache()

    def retrieve(model: str) -> Any:
        raise AssertionError("no call for a model the loop would refuse")

    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(retrieve))
    assert _check_connections().check_llm() is False


def test_the_stub_proposer_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIS_PROPOSER", "stub")
    config.reset_config_cache()
    monkeypatch.setitem(sys.modules, "anthropic", None)   # importing it would fail
    try:
        assert _check_connections().check_llm() is None
    finally:
        config.reset_config_cache()


def test_a_bad_key_fails_the_whole_preflight(
    real_proposer: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without --deep too: the key is a credential like the others.
    module = _check_connections()
    for name in ("check_confluence", "check_jira", "check_github", "check_aws", "check_pager"):
        monkeypatch.setattr(module, name, lambda settings: None)
    monkeypatch.setattr(module, "load_settings", lambda: None)
    monkeypatch.setattr(module, "settings_summary", lambda settings: {})
    monkeypatch.setattr(sys, "argv", ["check_connections.py"])

    def retrieve(model: str) -> Any:
        raise _Rejected("Error code: 401 - invalid x-api-key")

    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(retrieve))
    assert module.main() == 1
