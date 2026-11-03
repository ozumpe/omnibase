"""OMNI-133: develop as a running platform. Each merge is green until it passes.

The workers run in the default (subprocess) sandbox: what is tested is the
judgment and the switch, not the isolation, which tests/test_sandbox_worker.py
covers in both modes.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from sis import config, episodic, org
from sis.contract import SUM_OF_DIVISORS
from sis.develop_platform import DevelopPlatform, Judgment, judge_window
from sis.policy import ChangeTier, classify
from sis.sandbox_worker import CallResult, Reply

NAIVE = '''
def sum_of_divisors(n: int) -> int:
    total = 0
    for i in range(1, n + 1):
        if n % i == 0:
            total += i
    return total
'''

FAST = '''
import math


def sum_of_divisors(n: int) -> int:
    total = 0
    for i in range(1, math.isqrt(n) + 1):
        if n % i == 0:
            total += i
            if i != n // i:
                total += n // i
    return total
'''

WRONG = FAST.replace("    return total", "    return total + (1 if n % 3 == 0 else 0)")

RAISES = '''
def sum_of_divisors(n: int) -> int:
    if n % 5 == 0:
        raise ValueError("no fives")
    return 1
'''

BROKEN = "raise ImportError('does not load')\n"


@pytest.fixture(autouse=True)
def _fresh_config() -> Iterator[None]:
    config.reset_config_cache()
    yield
    config.reset_config_cache()


def _platform(blue: str, **kwargs: Any) -> DevelopPlatform:
    options: dict[str, Any] = {"exchanges": 110, "batch": 2, "seed": 7, **kwargs}
    platform = DevelopPlatform(SUM_OF_DIVISORS, blue_source=blue, blue_version="v1",
                               **options)
    platform.start()
    return platform


def _serves(platform: DevelopPlatform, n: int) -> Any:
    return platform._blue.call([[n]]).results[0].value


# --- the platform, end to end in the sandbox --------------------------------------


def test_a_merge_that_passes_switches_blue_and_green() -> None:
    platform = _platform(NAIVE)
    try:
        judgment = platform.judge("v2", FAST)
        assert judgment.switched and judgment.outcome == "develop_switched", judgment.reason
        assert platform.blue_version == "v2" and platform.blue_source == FAST
        assert judgment.previous == "v1" and judgment.verdict is not None
        assert _serves(platform, 12) == 28
    finally:
        platform.close()


def test_a_wrong_merge_is_retired_and_blue_keeps_serving() -> None:
    platform = _platform(NAIVE)
    try:
        judgment = platform.judge("v2", WRONG)
        assert not judgment.switched and not judgment.harness
        assert judgment.outcome == "develop_rolled_back"
        assert judgment.reason.startswith(("invariant violated", "response disagreement"))
        assert platform.blue_version == "v1" and platform.blue_source == NAIVE
        assert _serves(platform, 12) == 28
    finally:
        platform.close()


def test_a_slower_merge_is_retired() -> None:
    platform = _platform(FAST)
    try:
        judgment = platform.judge("v2", NAIVE)
        assert judgment.outcome == "develop_rolled_back"
        assert judgment.reason.startswith("live p"), judgment.reason
        assert platform.blue_version == "v1"
    finally:
        platform.close()


def test_a_merge_that_raises_is_retired_on_its_error_rate() -> None:
    platform = _platform(NAIVE)
    try:
        judgment = platform.judge("v2", RAISES)
        assert judgment.outcome == "develop_rolled_back"
        assert judgment.reason.startswith("live error rate"), judgment.reason
        assert "ValueError" in judgment.reason
        assert platform.blue_version == "v1"
    finally:
        platform.close()


def test_a_merge_that_does_not_load_never_replaces_blue() -> None:
    platform = _platform(NAIVE)
    try:
        judgment = platform.judge("v2", BROKEN)
        assert judgment.outcome == "develop_rolled_back"
        assert judgment.reason.startswith("green did not start")
        assert _serves(platform, 12) == 28 and platform.blue_version == "v1"
    finally:
        platform.close()


def test_a_merge_blue_already_serves_is_not_judged() -> None:
    platform = _platform(NAIVE)
    try:
        judgment = platform.judge("v2", NAIVE)
        assert judgment.outcome == "develop_unjudged" and platform.blue_version == "v1"
    finally:
        platform.close()


# --- the window's judgment, pure --------------------------------------------------


def _reply(values: Sequence[Any], elapsed: float = 0.001, *, fail: int | None = None
           ) -> Reply:
    results = [CallResult(ok=False, error="boom", error_types=("ValueError",))
               if i == fail else CallResult(ok=True, value=v) for i, v in enumerate(values)]
    return Reply(results=results, elapsed_s=elapsed)


def _window(green_values: Sequence[int] | None = None, *, green_elapsed: float = 0.001,
            blue_fail: bool = False, green_fail: bool = False
            ) -> list[tuple[list[list[int]], Reply, Reply]]:
    window = []
    for i in range(110):
        batch = [[i + 2]]
        right = [i + 2]
        green = [green_values[i]] if green_values else right
        window.append((batch, _reply(right, fail=0 if blue_fail and i == 50 else None),
                       _reply(green, green_elapsed, fail=0 if green_fail and i == 3 else None)))
    return window


def _identity(n: int) -> int:
    return n


def test_a_window_with_equal_answers_and_latency_passes() -> None:
    verdict, reason, harness = judge_window(_window(), version="v2", reference=_identity)
    assert verdict is not None and verdict.passed and not harness, reason


def test_green_disagreeing_with_the_reference_fails() -> None:
    wrong = [n + 2 if n != 40 else 0 for n in range(110)]
    verdict, reason, _ = judge_window(_window(wrong), version="v2", reference=_identity)
    assert verdict is not None and not verdict.passed
    assert reason is not None and reason.startswith("invariant violated: agrees_with_reference")


def test_latency_within_the_slack_passes_and_beyond_it_fails() -> None:
    within = judge_window(_window(green_elapsed=0.0012), version="v2", reference=_identity)
    beyond = judge_window(_window(green_elapsed=0.0013), version="v2", reference=_identity)
    assert within[0] is not None and within[0].passed
    assert beyond[0] is not None and not beyond[0].passed
    assert beyond[1] is not None and beyond[1].startswith("live p95 regression")


def test_a_failing_call_on_green_fails_green() -> None:
    verdict, reason, harness = judge_window(_window(green_fail=True), version="v2",
                                            reference=_identity)
    assert verdict is None and not harness
    assert reason is not None and reason.startswith("live error rate: green failed 1 of 110")


def test_a_failing_call_on_blue_is_the_harness_not_green() -> None:
    verdict, reason, harness = judge_window(_window(blue_fail=True), version="v2",
                                            reference=_identity)
    assert verdict is None and harness
    assert reason is not None and reason.startswith("harness:")


def test_a_thin_window_never_switches() -> None:
    verdict, _, _ = judge_window(_window()[:20], version="v2", reference=_identity)
    assert verdict is not None and not verdict.passed


# --- the shell: records, page and revert PR ---------------------------------------


class _Remote:
    def __init__(self, fn: Any) -> None:
        self.remote = fn


class _Actor:
    """A stand-in for a Ray actor handle whose methods return plain values."""

    def __init__(self, **methods: Any) -> None:
        for name, fn in methods.items():
            setattr(self, name, _Remote(fn))


class _FakePlatform:
    def __init__(self, judgment: Judgment) -> None:
        self.spec = SUM_OF_DIVISORS
        self.blue_version = "v1"
        self.blue_source = NAIVE
        self._judgment = judgment
        self.judged: list[tuple[str, str]] = []

    def judge(self, version: str, source: str) -> Judgment:
        self.judged.append((version, source))
        return self._judgment


def _shell(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"record": [], "slots": {}, "branches": [], "prs": [],
                             "pages": [], "emits": []}
    monkeypatch.setattr(org.ray, "get", lambda value: value)
    sm = _Actor(
        contract_for_pr=lambda pr: SUM_OF_DIVISORS,
        set_slot=lambda slot, v: calls["slots"].__setitem__(slot, v),
        record=lambda kind, ref, **d: calls["record"].append((kind, ref, d)),
    )

    class _PR:
        artifact = FAST
        id = "PR-9"

    ws = _Actor(
        get_pr=lambda pr, path: _PR(),
        create_branch=lambda name, base: calls["branches"].append((name, base)),
        open_pr=lambda *a: calls["prs"].append(a) or _PR(),
        notify=lambda sev, title, body: calls["pages"].append((sev, title, body))
        or {"delivered": "x"},
        emit=lambda event, **f: calls["emits"].append(event),
    )
    calls["handles"] = {"SelfModel": sm, "Workspace": ws}
    return calls


def _judgment(*, switched: bool = False, harness: bool = False) -> Judgment:
    return Judgment("v2", "v1", switched=switched, harness=harness,
                    reason="canary passed" if switched else "invariant violated: x")


def test_a_switch_is_recorded_without_a_page_or_a_revert(
        monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    calls, store = _shell(monkeypatch), episodic.JsonlEpisodicStore(tmp_path / "e.jsonl")
    platform = _FakePlatform(_judgment(switched=True))
    line = org.apply_develop_judgment(calls["handles"], store, platform,
                                      {"promoted": True, "pr": "PR-3", "version": "v2"})
    assert platform.judged == [("v2", FAST)]
    assert line is not None and "now serves as blue" in line
    assert calls["slots"] == {"develop_blue": "v2"}
    assert [r[0] for r in calls["record"]] == ["develop.switch"]
    assert not calls["pages"] and not calls["prs"]
    [event] = store.events()
    assert event.outcome == "develop_switched" and event.pr_id == "PR-3"
    assert event.reject_reason is None and event.contract == "sum_of_divisors"


def test_a_rollback_opens_a_revert_pr_and_pages(
        monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    calls, store = _shell(monkeypatch), episodic.JsonlEpisodicStore(tmp_path / "e.jsonl")
    line = org.apply_develop_judgment(calls["handles"], store, _FakePlatform(_judgment()),
                                      {"promoted": True, "pr": "PR-3", "version": "v2"})
    assert calls["branches"] and calls["branches"][0][0] == "revert/v2"
    [(branch, title, artifact, path, _body)] = calls["prs"]
    assert branch == "revert/v2" and artifact == NAIVE and path == SUM_OF_DIVISORS.target_path
    assert "Revert v2" in title
    [(severity, page_title, _)] = calls["pages"]
    assert severity.value == "critical" and "failed" in page_title
    [(kind, ref, detail)] = calls["record"]
    assert kind == "develop.rollback" and ref == "v2" and detail["revert_pr"] == "PR-9"
    assert "develop_blue" not in calls["slots"]
    assert line is not None and "revert PR PR-9" in line
    [event] = store.events()
    assert event.outcome == "develop_rolled_back"
    assert event.reject_gate == "canary_invariant"


def test_a_harness_fault_pages_but_reverts_nothing(
        monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    calls, store = _shell(monkeypatch), episodic.JsonlEpisodicStore(tmp_path / "e.jsonl")
    org.apply_develop_judgment(calls["handles"], store,
                               _FakePlatform(_judgment(harness=True)),
                               {"promoted": True, "pr": "PR-3", "version": "v2"})
    assert not calls["prs"] and calls["pages"]
    assert [r[0] for r in calls["record"]] == ["develop.unjudged"]
    assert store.events()[0].outcome == "develop_unjudged"


def test_a_release_is_not_judged(
        monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    calls = _shell(monkeypatch)
    platform = _FakePlatform(_judgment(switched=True))
    store = episodic.JsonlEpisodicStore(tmp_path / "e.jsonl")
    assert org.apply_develop_judgment(calls["handles"], store, platform,
                                      {"released": True, "pr": "PR-3"}) is None
    assert not platform.judged


def test_the_platform_is_off_by_default() -> None:
    assert config.get("canary.develop_blue_green") is False
    assert org.start_develop_platform({}) == (None, None)


def test_revert_branches_are_safe_names() -> None:
    assert org.revert_branch("abc123") == "revert/abc123"
    assert org.revert_branch("a b/../c") == "revert/a-b-..-c"
    assert org.revert_branch("") == "revert/unknown"


def test_the_platform_is_guardrail_code() -> None:
    assert classify("sis/develop_platform.py") is ChangeTier.FORBIDDEN
