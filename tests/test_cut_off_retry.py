"""OMNI-219 (M38): a proposal cut off at its output limit is retried once.

The weekly run of 2026-11-14 had two of three cycles cut off at the limit and
repeated each with the same limit, so the breaker opened after one judged
candidate. Olaf's choice (option B): the cut-off still counts, but its
proposal is retried once at ``proposer.retry_output_limit``, reserved before
it is sent; cut off again, the run stops with a reason that names the limit.
Both calls are charged.

Its own module, so it gets its own Ray cluster and its own CEO (needs Ray).
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sis import cost, llm, org, review
from sis.config import key_for

# --- pure ---------------------------------------------------------------------

_CUT = {"passed": False, "cut_off": True, "cost_usd": 0.01, "contract": "sum_of_divisors",
        "model": None, "candidate_sha": None, "pr_id": None,
        "usage": {"output_tokens": 16000, "output_limit": 16000, "stop_reason": "max_tokens"},
        "reason": "proposer: the claude-opus-4-8 answer stopped at the token limit "
                  "(max_tokens=16000), so it was not judged"}


def test_only_a_cut_off_is_retried_and_only_at_a_larger_limit() -> None:
    assert org.cut_off_retry_limit(_CUT, 32000) == 32000
    assert org.cut_off_retry_limit({**_CUT, "cut_off": False}, 32000) is None
    assert org.cut_off_retry_limit({"passed": False, "reason": "mypy: ..."}, 32000) is None
    # No larger than the call it repeats: it would be cut off the same way.
    assert org.cut_off_retry_limit(_CUT, 16000) is None
    assert org.cut_off_retry_limit(_CUT, 12000) is None
    # A provider that reports no usage: the configured retry limit stands.
    assert org.cut_off_retry_limit({**_CUT, "usage": None}, 32000) == 32000


def test_the_stop_reason_names_both_limits_and_the_key_to_raise() -> None:
    reason = org.cut_off_twice(16000, 32000)
    assert "16000 and then 32000" in reason
    assert "proposer.retry_output_limit" in reason


def test_the_retry_reservation_covers_the_retry_and_its_review_at_their_limits() -> None:
    limit = key_for("proposer.retry_output_limit").default
    in_price, out_price = cost.price(llm.DEFAULT_ANTHROPIC_MODEL)
    floor = (limit + review.MAX_TOKENS) * out_price / 1_000_000
    estimate = org.retry_estimate_usd(limit)
    assert floor < estimate < floor + 0.2
    # Priced at the model the cut-off call used, not the default.
    assert org.retry_estimate_usd(limit, "claude-haiku-4-5") < estimate


def test_the_retry_limit_is_larger_than_the_first_and_forbidden() -> None:
    from sis.config import ConfigTier

    retry, first = key_for("proposer.retry_output_limit"), key_for("proposer.output_limit")
    assert retry.default > first.default
    assert retry.tier is ConfigTier.FORBIDDEN
    # The first call stays a plain request; the retry needs a streamed one.
    assert not llm.needs_streaming(first.default)
    assert llm.needs_streaming(retry.default)


# --- with the real roles ----------------------------------------------------------

ray = pytest.importorskip("ray")

from sis import episodic  # noqa: E402


@pytest.fixture(scope="module")
def handles():  # type: ignore[no-untyped-def]
    h = org.bootstrap()
    yield h
    ray.shutdown()


@pytest.fixture(autouse=True)
def _a_clean_streak(request: pytest.FixtureRequest) -> None:
    if "handles" in request.fixturenames:
        ray.get(request.getfixturevalue("handles")["CEO"].reset_breaker.remote())


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> episodic.JsonlEpisodicStore:
    real = episodic.JsonlEpisodicStore(tmp_path / "episodic.jsonl")
    monkeypatch.setattr(episodic, "get_episodic_store", lambda: real)
    return real


def _ceo(handles: dict[str, Any]) -> tuple[float, float]:
    state = ray.get(handles["CEO"].state_snapshot.remote())
    return float(state["spent_usd"]), float(state["consecutive_failures"])


class _Steps:
    """The SWE's implement(): returns *results* in turn, recording each call's
    output limit and what the CEO had saved when the call was made."""

    def __init__(self, store: episodic.JsonlEpisodicStore, *results: dict[str, Any]) -> None:
        self._results, self._store = list(results), store
        self.limits: list[int | None] = []
        self.saved_reservations: list[float] = []

    def remote(self, story_id: str, contract_name: str | None = None,
               output_limit: int | None = None, earlier: Any = ()) -> Any:
        self.limits.append(output_limit)
        self.saved_reservations.append(
            float((self._store.load_state("ceo") or {}).get("reserved_usd") or 0.0))
        return ray.put(self._results.pop(0))


def _swe(steps: _Steps) -> SimpleNamespace:
    return SimpleNamespace(already_built=SimpleNamespace(remote=lambda *a, **k: ray.put(None)),
                           implement=steps)


_REJECTED = {"passed": False, "cost_usd": 0.02, "contract": "sum_of_divisors", "model": None,
             "candidate_sha": "c0ffee", "pr_id": None,
             "usage": {"output_tokens": 21000, "output_limit": 32000, "stop_reason": "end_turn"},
             "reason": "mypy: error: Missing return statement"}


def test_a_cut_off_is_retried_once_at_the_larger_limit_and_both_calls_are_charged(
    handles: dict[str, Any], store: episodic.JsonlEpisodicStore,
) -> None:
    steps = _Steps(store, _CUT, _REJECTED)
    spent, streak = _ceo(handles)
    result = org.run_cycle({**handles, "SWE": _swe(steps)}, "t", "b")

    assert steps.limits == [None, 32000]
    # The retry was reserved and saved before it was sent, the cut-off charged.
    assert steps.saved_reservations[1] == pytest.approx(org.retry_estimate_usd(32000))
    # The retry's own verdict decides the cycle: one failure, not two.
    assert result["status"] == "rolled_back"
    assert result["reason"] == _REJECTED["reason"]
    assert result["cost_usd"] == pytest.approx(0.03)
    assert _ceo(handles) == (pytest.approx(spent + 0.03), streak + 1)
    assert not ray.get(handles["CEO"].breaker_open.remote())
    saved = store.load_state("ceo")
    assert saved["spent_usd"] == pytest.approx(spent + 0.03) and not saved["reserved_usd"]
    (event,) = store.events()
    assert event.cost_usd == pytest.approx(0.03) and event.output_limit == 32000


def test_a_retry_cut_off_too_stops_the_run_and_says_which_limit(
    handles: dict[str, Any], store: episodic.JsonlEpisodicStore,
) -> None:
    again = {**_CUT, "usage": {**_CUT["usage"], "output_tokens": 32000, "output_limit": 32000},
             "reason": _CUT["reason"].replace("16000", "32000")}
    steps = _Steps(store, _CUT, again)
    spent, streak = _ceo(handles)
    result = org.run_cycle({**handles, "SWE": _swe(steps)}, "t", "b")

    assert steps.limits == [None, 32000]
    assert _ceo(handles) == (pytest.approx(spent + 0.02), streak + 1)
    state = ray.get(handles["CEO"].state_snapshot.remote())
    assert state["tripped"] and state["trip_reason"] == org.cut_off_twice(16000, 32000)
    pages = [e for e in ray.get(handles["Workspace"].events.remote())
             if e["event"] == "notify.sent" and "cut off twice" in str(e["title"])]
    assert pages, "a person is told the limit is too small"
    assert result["breaker_bug_id"]
    # The next cycle does not run.
    after = org.run_cycle({**handles, "SWE": _swe(_Steps(store))}, "t", "b")
    assert after["status"] == "circuit_breaker_open"


def test_a_retry_the_spend_cap_cannot_cover_is_not_sent(
    handles: dict[str, Any], store: episodic.JsonlEpisodicStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(org, "retry_estimate_usd", lambda limit, model=None: 1000.0)
    steps = _Steps(store, _CUT)
    spent, streak = _ceo(handles)
    result = org.run_cycle({**handles, "SWE": _swe(steps)}, "t", "b")

    assert steps.limits == [None]
    assert result["status"] == "rolled_back"
    assert "not retried at 32000" in result["reason"]
    assert result["reason"].startswith("proposer:")  # still the proposer's gate
    assert _ceo(handles) == (pytest.approx(spent + 0.01), streak + 1)
    assert not store.load_state("ceo")["reserved_usd"]


@ray.remote
class _DiesOnRetry:
    """An SWE whose proposal is cut off, and whose process ends during the retry."""

    def already_built(self, *args: Any) -> None:
        return None

    def implement(self, story_id: str, contract_name: str | None = None,
                  output_limit: int | None = None, earlier: Any = ()) -> dict[str, Any]:
        if output_limit is None:
            return _CUT
        import os

        os._exit(1)


def test_a_retry_whose_process_dies_is_charged_its_reservation(
    handles: dict[str, Any], store: episodic.JsonlEpisodicStore,
) -> None:
    # Nothing reports what the retry spent, so it is charged what was
    # reserved for it, on top of the cut-off call already charged.
    spent, _streak = _ceo(handles)
    result = org.run_cycle({**handles, "SWE": _DiesOnRetry.remote()}, "t", "b")

    assert result["status"] == "error"
    charged = 0.01 + org.retry_estimate_usd(32000)
    assert result["cost_usd"] == pytest.approx(charged)
    state = ray.get(handles["CEO"].state_snapshot.remote())
    assert (state["spent_usd"], state["reserved_usd"]) == (pytest.approx(spent + charged), 0.0)
