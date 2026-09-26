"""OMNI-49 (KNOWN_ISSUES M19): the Serve canary refuses LLM-written candidates.

A green replica is an ordinary Ray worker in the control-plane cluster, not the
gauntlet's sandbox (H3). Until OMNI-48 isolates it, only the stub's hand-written
candidate may run there. Same shape as the M1 rule in ``tests/test_gauntlet.py``,
minus the override: the legacy canary costs nothing to fall back to.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from typing import Any

import pytest

import main
from sis import config, gauntlet, org
from sis.contract import SORT
from sis.serve_cloud import ServeCloud


@pytest.fixture(autouse=True)
def _isolate_config() -> Iterator[None]:
    # config.config() caches, and main() applies a CLI overlay; neither may
    # leak into the next test in this xdist worker (see tests/test_config.py).
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


# --- the decision (pure) ------------------------------------------------------


def test_the_stub_may_use_the_serve_canary() -> None:
    assert gauntlet.serve_canary_problem("serve", "stub") is None


@pytest.mark.parametrize("backend", ["legacy", ""])
def test_the_legacy_canary_runs_any_proposer(backend: str) -> None:
    assert gauntlet.serve_canary_problem(backend, "claude") is None


def test_an_llm_proposer_may_not_use_the_serve_canary() -> None:
    problem = gauntlet.serve_canary_problem("serve", "claude")
    assert problem is not None
    # The reason names the risk and the way out, not just "no".
    assert "Ray worker" in problem and "IMDS" in problem
    assert "OMNI-48" in problem and "canary.backend unset" in problem


# --- the configured check -----------------------------------------------------


def _configure(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for name in ("SIS_PROPOSER", "SIS_CANARY", "SIS_ENV"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    config.reset_config_cache()


def test_the_configured_backend_is_used_when_none_is_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, SIS_PROPOSER="claude", SIS_CANARY="serve")
    with pytest.raises(RuntimeError, match="OMNI-48"):
        gauntlet.ensure_canary_allows_proposer()


def test_an_explicit_backend_wins_over_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, SIS_PROPOSER="claude")
    with pytest.raises(RuntimeError, match="OMNI-48"):
        gauntlet.ensure_canary_allows_proposer("serve")


def test_the_default_configuration_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, SIS_PROPOSER="claude")  # canary.backend unset
    gauntlet.ensure_canary_allows_proposer()


def test_aws_alone_is_not_a_reason_to_refuse_the_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    # The rule keys on who wrote the candidate, not where it runs: the stub's
    # hand-written candidate is as trustworthy on the AWS box as on a laptop.
    _configure(monkeypatch, SIS_PROPOSER="stub", SIS_CANARY="serve", SIS_ENV="aws")
    gauntlet.ensure_canary_allows_proposer()


# --- where it is enforced -----------------------------------------------------


def test_main_refuses_at_startup_before_any_cluster_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The acceptance case from the ticket: real proposer + Serve canary on AWS.
    _configure(monkeypatch, SIS_PROPOSER="claude", SIS_CANARY="serve", SIS_ENV="aws")
    monkeypatch.setattr(sys, "argv", ["main.py", "--loop"])

    def _no_bootstrap() -> Any:
        raise AssertionError("bootstrap reached: the refusal came too late")

    monkeypatch.setattr(org, "bootstrap", _no_bootstrap)
    with pytest.raises(RuntimeError, match="OMNI-48"):
        main.main()


class _Telemetry:
    def emit(self, *args: Any, **kwargs: Any) -> None:
        pass


def test_the_deployment_itself_refuses_even_if_a_caller_forgot_to_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The backstop sits on the line that turns candidate source into a Ray
    # worker, and fires before anything touches Serve — no cluster needed.
    _configure(monkeypatch, SIS_PROPOSER="claude")
    cloud = ServeCloud(_Telemetry(), SORT)
    with pytest.raises(RuntimeError, match="OMNI-48"):
        cloud.deploy_canary("v2", source="def sort_numbers(v): return sorted(v)\n")
