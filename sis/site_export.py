"""sis.site_export — what the public landing page may show (OMNI-229).

The landing page (``site/``) is public, so this module is the door between
what the engine records and what anyone on the web can read. It decides what
goes through in pure functions and is POLICY-FORBIDDEN: a loop able to edit it
could publish its own exam, or a licensed value.

- **Only public series reach the page.** A scoreboard is built only for a
  target that EIA lets us redistribute (:data:`PUBLIC_TARGETS`); the licensed
  LA spot price is modelled, never shown. The finished page data is checked
  once more for any licensed series id (:func:`check_public`).
- **A rejection shows neither error.** What the loop tried is published as
  its verdict and a fixed plain sentence for the gate that stopped it, never
  the gate's own reason text, which may carry numbers from the held-out exam.
- **Every field is named here.** Nothing from a record is copied through
  wholesale, so a field added to the episodic log later stays private until
  someone adds it below.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sis.adapters_real import EIA_PUBLIC_SERIES, EIA_SERIES
from sis.episodic import ACCEPTED_OUTCOMES, ScoredWeek

PUBLIC_TARGETS: frozenset[str] = EIA_PUBLIC_SERIES
LICENSED_SERIES: frozenset[str] = frozenset(EIA_SERIES) - EIA_PUBLIC_SERIES

# How many weeks the scoreboard's chart shows.
WEEKS_SHOWN = 13

# What each contract asks, in words a visitor understands. A contract missing
# here is not shown: the page never guesses what a contract is about.
QUESTIONS: Mapping[str, str] = {
    "ca_price_wholesale": "What will California's average retail gasoline price be next week?",
    "ca_price_persistence": "What will California's average retail gasoline price be next week?",
}

# The plain sentence shown for each verdict. Keyed by the episodic outcome,
# then by the gate that stopped a cycle. The gate's own reason is never shown.
_GATE_SENTENCES: Mapping[str, str] = {
    "syntax": "The code did not parse.",
    "noop": "It changed nothing that matters.",
    "mypy": "It failed the type check.",
    "interface": "It did not keep the contract's interface.",
    "acceptance": "It failed the contract's acceptance tests.",
    "invariants": "It broke a rule every answer must keep.",
    "backtest": "It did not beat the baseline on history it had never seen.",
    "differential": "Its answers differed from the reference.",
    "benchmark": "It was not measurably faster.",
    "slo": "It missed its service level.",
    "review": "Its own review did not approve it.",
    "built": "Nothing left to improve: the merged version already passes every gate.",
}
_OUTCOME_WORDS: Mapping[str, str] = {
    "verified_awaiting_human_merge": "passed every gate",
    "human_merged": "merged by a human",
    "promoted": "promoted",
    "feature_step": "passed every gate",
    "rolled_back": "rejected",
    "no_gain": "nothing to improve",
    "inconclusive": "inconclusive",
    "error": "failed to run",
}


class ExportRefused(ValueError):
    """The page data would show something it must not. Nothing is written."""


@dataclass(frozen=True)
class Week:
    """One scoreboard week: what the engine said, what persistence said, what happened."""

    week: str             # ISO: the last week the predictor was shown
    next_week: str        # ISO: the week it forecast
    predicted: float
    baseline: float | None  # persistence: the value of ``week`` (the previous row's actual)
    actual: float | None

    @property
    def beat_baseline(self) -> bool | None:
        if self.actual is None or self.baseline is None:
            return None
        return abs(self.predicted - self.actual) < abs(self.baseline - self.actual)


def weeks(series: Iterable[ScoredWeek], *, target: str,
          predictor: str | None = None) -> list[Week]:
    """The scored weeks of one predictor on one public target, oldest first.

    Each week's latest revision wins. With no *predictor*, the one scored most
    recently is shown. Persistence's forecast for ``next_week`` is the actual
    of ``week``, read from the row that forecast ``week``.
    """
    if target not in PUBLIC_TARGETS:
        raise ExportRefused(f"{target}: not a public series, so the page never shows it")
    rows = [row for row in series if row.target == target]
    if predictor is None and rows:
        predictor = max(rows, key=lambda row: (row.ts, row.week)).predictor
    latest: dict[str, ScoredWeek] = {}
    for row in rows:
        if row.predictor != predictor:
            continue
        held = latest.get(row.week)
        if held is None or row.revision >= held.revision:
            latest[row.week] = row
    value_at = {row.next_week: row.actual for row in latest.values()}
    return [Week(week=row.week, next_week=row.next_week, predicted=row.predicted,
                 baseline=value_at.get(row.week), actual=row.actual)
            for row in sorted(latest.values(), key=lambda row: row.week)]


def streak(shown: Sequence[Week]) -> int:
    """How many of the latest judged weeks in a row the engine beat persistence."""
    count = 0
    for week in reversed(shown):
        verdict = week.beat_baseline
        if verdict is None:
            continue
        if not verdict:
            break
        count += 1
    return count


def tried(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """What the loop tried, oldest first: verdict and a fixed sentence, never the reason."""
    out: list[dict[str, Any]] = []
    for record in records:
        outcome = str(record.get("outcome") or "")
        if outcome not in _OUTCOME_WORDS:
            continue
        gate = record.get("reject_gate")
        if outcome == "verified_awaiting_human_merge":
            sentence = "It passed every gate and opened a pull request for a human to merge."
        elif outcome == "human_merged":
            sentence = "A human read the pull request and merged it."
        elif isinstance(gate, str) and gate in _GATE_SENTENCES:
            sentence = _GATE_SENTENCES[gate]
        elif isinstance(gate, str) and gate:
            sentence = "A gate stopped it."
        else:
            sentence = ""
        pr = record.get("pr_id")
        out.append({
            "ts": str(record.get("ts") or ""),
            "contract": str(record.get("contract") or ""),
            "verdict": _OUTCOME_WORDS[outcome],
            "why": sentence,
            "cost_usd": round(float(record.get("cost_usd") or 0.0), 2),
            "pr": str(pr) if pr else None,
        })
    return out


def cost(records: Iterable[Mapping[str, Any]], *, budget_usd: float) -> dict[str, Any]:
    """Spend in these records, the budget, and the cost of each accepted improvement."""
    listed = list(records)
    spent = sum(float(record.get("cost_usd") or 0.0) for record in listed)
    accepted = sum(1 for record in listed if record.get("outcome") in ACCEPTED_OUTCOMES)
    return {
        "spent_usd": round(spent, 2),
        "budget_usd": round(budget_usd, 2),
        "accepted": accepted,
        "per_accepted_usd": round(spent / accepted, 2) if accepted else None,
    }


def page_data(*, records: Sequence[Mapping[str, Any]], series: Iterable[ScoredWeek],
              contract: str, target: str, budget_usd: float, generated_at: str,
              code_version: str, repo_url: str, test_repo_url: str) -> dict[str, Any]:
    """Everything the landing page shows, checked to be public."""
    if contract not in QUESTIONS:
        raise ExportRefused(f"{contract}: no plain question for it, so the page does not show it")
    shown = weeks(series, target=target)[-WEEKS_SHOWN:]
    ours = [record for record in records if record.get("contract") == contract]
    data: dict[str, Any] = {
        "generated_at": generated_at,
        "code_version": code_version,
        "repo_url": repo_url,
        "test_repo_url": test_repo_url,
        "contract": contract,
        "question": QUESTIONS[contract],
        "baseline": "persistence: next week's price will be the same as this week's",
        "margin_pct": 10,
        "scoreboard": {
            "weeks": [{"week": w.week, "next_week": w.next_week,
                       "predicted": round(w.predicted, 3),
                       "baseline": None if w.baseline is None else round(w.baseline, 3),
                       "actual": None if w.actual is None else round(w.actual, 3),
                       "beat_baseline": w.beat_baseline} for w in shown],
            "streak": streak(shown),
        },
        "tried": tried(ours),
        "cost": cost(ours, budget_usd=budget_usd),
    }
    check_public(data)
    return data


def check_public(data: Mapping[str, Any]) -> None:
    """Refuse page data that names a licensed series anywhere."""
    text = json.dumps(data, sort_keys=True)
    named = sorted(series for series in LICENSED_SERIES if series in text)
    if named:
        raise ExportRefused(f"{', '.join(named)}: licensed, never shown on the public page")
