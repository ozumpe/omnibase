"""OMNI-62: a breaker trip must page a human, not only file a TES bug. No Ray here.

The live wiring — a real cycle that trips the breaker sends exactly one page —
is in ``tests/test_org_failures.py``.
"""

from __future__ import annotations

import importlib.util
import os
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sis import config
from sis.adapters import InMemoryNotifier, InMemoryTelemetry
from sis.adapters_real import SNSNotifier, sns_subject
from sis.episodic import JsonlEpisodicStore
from sis.loop import Tick, Work, run_loop, stop_alert
from sis.org import record_page_outcome
from sis.paths import PROJECT_ROOT
from sis.ports import Notifier, Severity

_ARN = "arn:aws:sns:us-east-1:123456789012:sis-first-run-alerts"


@pytest.fixture(autouse=True)
def _isolate_config() -> Iterator[None]:
    saved = {key.env: os.environ.get(key.env) for key in config.SCHEMA}
    config.reset_config_cache()
    yield
    config.reset_config_cache()
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


# --- the adapters ----------------------------------------------------------------


def test_the_in_memory_notifier_records_what_it_would_send() -> None:
    tel = InMemoryTelemetry()
    notifier = InMemoryNotifier(tel)
    assert isinstance(notifier, Notifier)
    delivery = notifier.notify(Severity.CRITICAL, "circuit breaker open", "details")
    assert notifier.sent() == [{"id": delivery, "severity": "critical",
                                "title": "circuit breaker open", "body": "details"}]
    assert any(e["event"] == "notify.sent" for e in tel.events())


class _FakeSNS:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, **kwargs: Any) -> dict[str, Any]:
        self.published.append(kwargs)
        return {"MessageId": "m-1"}

    def get_topic_attributes(self, **kwargs: Any) -> dict[str, Any]:
        return {"Attributes": {"SubscriptionsConfirmed": "1"}}


def test_sns_publishes_to_its_one_topic() -> None:
    fake = _FakeSNS()
    notifier = SNSNotifier(_ARN, InMemoryTelemetry(), client=fake)
    assert isinstance(notifier, Notifier)
    assert notifier.notify(Severity.WARNING, "sandbox broken during TES-4", "body") == "m-1"
    (call,) = fake.published
    assert call["TopicArn"] == _ARN
    assert call["Subject"] == "[sis warning] sandbox broken during TES-4"
    assert call["Message"] == "body"
    assert "1 confirmed" in notifier.check()


@pytest.mark.parametrize("arn", ["", "not-an-arn", "arn:aws:sqs:us-east-1:1:q",
                                 "arn:aws:sns:us-east-1:only-five"])
def test_a_malformed_topic_is_refused_up_front(arn: str) -> None:
    with pytest.raises(ValueError, match="SNS topic ARN"):
        SNSNotifier(arn, InMemoryTelemetry(), client=_FakeSNS())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("circuit breaker open: hard spend cap exceeded",
         "circuit breaker open: hard spend cap exceeded"),
        ("two\nlines\tand\rcontrols", "two lines and controls"),   # would be rejected
        ("non-ascii — em dash, µs", "non-ascii em dash, s"),
        ("", "sis alert"),
    ],
)
def test_the_subject_is_something_sns_accepts(raw: str, expected: str) -> None:
    assert sns_subject(raw) == expected


def test_a_long_subject_is_cut_below_the_limit() -> None:
    assert len(sns_subject("x" * 500)) < 100


# --- a page that could not be sent ------------------------------------------------


def test_a_failed_page_is_kept_in_the_episodic_store(tmp_path: Path) -> None:
    store = JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    record_page_outcome(store, Severity.CRITICAL, "circuit breaker open", {"delivered": "m-1"})
    assert store.load_state("notifier") is None  # a delivered page leaves nothing
    record_page_outcome(store, Severity.CRITICAL, "circuit breaker open",
                        {"error": "AuthorizationError"})
    assert store.load_state("notifier") == {"last_failure": {
        "title": "circuit breaker open", "severity": "critical", "error": "AuthorizationError"}}


# --- the loop stopping (L24) -------------------------------------------------------


def test_running_out_of_budget_pages_instead_of_stopping_silently() -> None:
    alert = stop_alert(Tick(breaker_open=False, budget_ok=False, work=None), cycles_run=4)
    assert alert is not None and "budget exhausted" in alert[0]


def test_a_breaker_that_tripped_this_run_is_not_paged_twice() -> None:
    assert stop_alert(Tick(breaker_open=True, budget_ok=True, work=None), cycles_run=3) is None


def test_a_breaker_open_before_the_first_cycle_is_paged() -> None:
    alert = stop_alert(Tick(breaker_open=True, budget_ok=True, work=None), cycles_run=0)
    assert alert is not None and "did not start" in alert[0]


def test_the_loop_reports_the_tick_that_stopped_it() -> None:
    ticks = iter([Tick(breaker_open=False, budget_ok=True, work=Work("t", "b")),
                  Tick(breaker_open=False, budget_ok=False, work=None)])
    stopped: list[tuple[Tick, int]] = []
    run_loop(lambda: next(ticks), lambda work: {}, sleep=lambda _s: None,
             on_stop=lambda tick, n: stopped.append((tick, n)))
    assert len(stopped) == 1
    assert stopped[0][0].budget_ok is False and stopped[0][1] == 1


# --- the preflight ------------------------------------------------------------------


def _check_connections() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_connections", PROJECT_ROOT / "scripts" / "check_connections.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Settings:
    def __init__(self, env: str) -> None:
        self.env = env


def test_a_missing_pager_fails_the_preflight_only_on_the_aws_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIS_NOTIFY_SNS_TOPIC_ARN", raising=False)
    config.reset_config_cache()
    check = _check_connections().check_pager
    assert check(_Settings("aws")) is False
    assert check(_Settings("local")) is None
