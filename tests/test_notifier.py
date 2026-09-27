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
from sis.adapters_real import SNSNotifier, pager_problem, sns_subject
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


# --- OMNI-122: a pager nobody confirmed is not a pager ---------------------------


class _Topic:
    """An SNS client whose topic has the given subscription counts."""

    def __init__(self, confirmed: int, pending: int) -> None:
        self._attrs = {"SubscriptionsConfirmed": str(confirmed),
                       "SubscriptionsPending": str(pending)}

    def get_topic_attributes(self, **kwargs: Any) -> dict[str, Any]:
        return {"Attributes": self._attrs}


def test_a_confirmed_subscription_is_a_working_pager() -> None:
    assert pager_problem(1, 0) is None
    assert pager_problem(2, 3) is None


def test_the_first_aws_runs_topic_is_a_pager_problem() -> None:
    # Exactly the run's state: the link mailed, never clicked.
    problem = pager_problem(0, 1)
    assert problem is not None
    assert "delivered to nobody" in problem and "click the link" in problem


def test_a_topic_with_nothing_subscribed_says_where_to_look() -> None:
    problem = pager_problem(0, 0)
    assert problem is not None and "alert_email" in problem


def test_the_check_raises_rather_than_ticking_an_unconfirmed_topic() -> None:
    notifier = SNSNotifier(_ARN, InMemoryTelemetry(), client=_Topic(0, 1))
    assert notifier.subscriptions() == (0, 1)
    with pytest.raises(RuntimeError, match="delivered to nobody"):
        notifier.check()
    assert "1 confirmed" in SNSNotifier(_ARN, InMemoryTelemetry(), client=_Topic(1, 0)).check()


@pytest.mark.parametrize(("confirmed", "expected"), [(0, False), (1, True)])
def test_the_preflight_fails_an_unconfirmed_pager(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    confirmed: int, expected: bool,
) -> None:
    import sis.adapters_real as real

    monkeypatch.setenv("SIS_NOTIFY_SNS_TOPIC_ARN", _ARN)
    config.reset_config_cache()
    monkeypatch.setattr(real, "SNSNotifier", lambda arn, tel: SNSNotifier(
        arn, tel, client=_Topic(confirmed, 1 - confirmed)))
    assert _check_connections().check_pager(_Settings("aws")) is expected
    if not expected:
        # Printed whole: the part that says what to do is at the end.
        assert "click the link AWS mailed to alert_email" in capsys.readouterr().out

