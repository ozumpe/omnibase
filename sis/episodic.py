"""sis.episodic — the episodic log / provenance store, behind a port.

The most valuable output of the loop isn't the optimised code — it's the record
of *what was proposed, why, and what happened*: spec → diff → gauntlet verdict →
outcome, including every rejected diff and the gate that caught it. That record
is the dataset a self-improving system learns from.

The store is a **port** (`EpisodicStore`) with three interchangeable backends,
selected by ``SIS_EPISODIC_STORE``:

- ``jsonl`` (default): append-only JSON lines. Zero deps, durable, git-diffable.
- ``duckdb``: an embedded analytical DB — SQL/rollups over the events. Optional
  dependency (``poetry install --with analytics``); single-writer, so write from
  one process (the loop driver), which is how the loop already runs.
- ``none``: a no-op store, for when you don't want a log at all.

Swapping to Postgres+pgvector later (for a multi-node cluster / embedding
retrieval) is a new backend implementing the same port — the loop doesn't change.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Protocol

from sis.clock import Clock, now_iso
from sis.paths import EPISODIC_DUCKDB, EPISODIC_JSONL

# Outcomes that count as an accepted improvement (passed gauntlet + QA, in a PR).
ACCEPTED_OUTCOMES = frozenset({"verified_awaiting_human_merge", "promoted"})


@dataclass
class EpisodicEvent:
    """One cycle outcome. The schema *is* the value — keep it queryable."""

    cycle_id: str
    ts: str
    # outcome: verified_awaiting_human_merge | rolled_back | qa_rejected |
    #          budget_denied | circuit_breaker_open | ...
    outcome: str
    proposer: str = "stub"
    model: str | None = None
    spec_id: str | None = None
    story_id: str | None = None
    pr_id: str | None = None
    candidate_sha: str | None = None
    gauntlet_passed: bool | None = None
    # reject_gate: ast | noop | mypy | interface | acceptance | invariant
    #            | backtest | correctness | benchmark | policy | timeout
    #            | pytest    (pre-OMNI-17 name for `acceptance`; still emitted
    #                         by nothing, still recognised for old rows)
    #            | harness   ("harness" = the gate could not run, not a verdict
    #                         on the candidate)
    #            | canary_evidence | canary_invariant | canary_disagreement
    #            | canary_regression  (evaluate_canary, OMNI-14: the ONLINE
    #                         analogue of correctness/benchmark, named
    #                         distinctly so rejected_by_gate can tell whether
    #                         the sandbox or live traffic caught it)
    reject_gate: str | None = None
    reject_reason: str | None = None
    baseline_latency: float | None = None
    candidate_latency: float | None = None
    improvement_pct: float | None = None
    cost_usd: float = 0.0

    @staticmethod
    def new_cycle_id() -> str:
        return uuid.uuid4().hex[:12]


_FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in fields(EpisodicEvent))


def _now(clock: Clock | None = None) -> str:
    """Event time for an episode. Wall clock unless a replay drives it.

    Almost always the wall clock: ``ts`` records when the *engine* ran a cycle,
    which is audit-trail time and should not be movable. The parameter exists
    for the one case where it should — a replay driver reconstructing episodes
    stamps them in the timeline they belong to, not the timeline they are being
    recomputed in. See sis.clock for the engine-time/world-time distinction.
    """
    return now_iso(clock)


def _from_dict(data: dict[str, Any]) -> EpisodicEvent:
    """Build an event from a dict, ignoring unknown keys (schema-evolution safe)."""
    return EpisodicEvent(**{k: v for k, v in data.items() if k in set(_FIELD_NAMES)})


def summarize(events: list[EpisodicEvent]) -> dict[str, Any]:
    """Pure rollup used by every backend, so summaries are identical across them."""
    accepted = sum(1 for e in events if e.outcome in ACCEPTED_OUTCOMES)
    total_cost = round(sum(e.cost_usd for e in events), 6)
    return {
        "total": len(events),
        "by_outcome": dict(Counter(e.outcome for e in events)),
        "rejected_by_gate": dict(Counter(e.reject_gate for e in events if e.reject_gate)),
        "accepted": accepted,
        "total_cost_usd": total_cost,
        "cost_per_accepted_usd": round(total_cost / accepted, 6) if accepted else None,
    }


# --------------------------------------------------------------------------
# Port + backends
# --------------------------------------------------------------------------


class EpisodicStore(Protocol):
    def append(self, event: EpisodicEvent) -> None: ...
    def events(self) -> list[EpisodicEvent]: ...
    def summary(self) -> dict[str, Any]: ...
    # Small latest-wins key/value state alongside the append-only event log —
    # e.g. the CEO's persisted brake/spend state (L9). Distinct from events so
    # it survives cluster/actor restart and rehydrates on bootstrap.
    def save_state(self, key: str, value: dict[str, Any]) -> None: ...
    def load_state(self, key: str) -> dict[str, Any] | None: ...


class NullEpisodicStore:
    """Discards events. For when you don't want a log (SIS_EPISODIC_STORE=none)."""

    def append(self, event: EpisodicEvent) -> None:
        return None

    def events(self) -> list[EpisodicEvent]:
        return []

    def summary(self) -> dict[str, Any]:
        return summarize([])

    def save_state(self, key: str, value: dict[str, Any]) -> None:
        return None

    def load_state(self, key: str) -> dict[str, Any] | None:
        return None


class JsonlEpisodicStore:
    """Append-only JSON lines — the durable, zero-dependency default."""

    def __init__(self, path: Path = EPISODIC_JSONL) -> None:
        self.path = path
        # Latest-wins state sits next to the event log (…/episodic_state.json),
        # so constructing the store on a tmp path isolates both in tests.
        self._state_path = path.with_name(f"{path.stem}_state.json")

    def append(self, event: EpisodicEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(event)) + "\n")

    def save_state(self, key: str, value: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        blob: dict[str, Any] = {}
        if self._state_path.exists():
            try:
                blob = json.loads(self._state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                blob = {}  # corrupt/legacy → overwrite rather than crash
        blob[key] = value
        self._state_path.write_text(json.dumps(blob, indent=2), encoding="utf-8")

    def load_state(self, key: str) -> dict[str, Any] | None:
        if not self._state_path.exists():
            return None
        try:
            blob = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        value = blob.get(key)
        return value if isinstance(value, dict) else None

    def events(self) -> list[EpisodicEvent]:
        if not self.path.exists():
            return []
        out: list[EpisodicEvent] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(_from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue  # skip malformed / legacy lines rather than crash
        return out

    def summary(self) -> dict[str, Any]:
        return summarize(self.events())


# DuckDB column types, keyed by field name. Asserted to match the dataclass so
# the schema can't silently drift.
_DUCK_TYPES: dict[str, str] = {
    "cycle_id": "VARCHAR", "ts": "VARCHAR", "outcome": "VARCHAR",
    "proposer": "VARCHAR", "model": "VARCHAR", "spec_id": "VARCHAR",
    "story_id": "VARCHAR", "pr_id": "VARCHAR", "candidate_sha": "VARCHAR",
    "gauntlet_passed": "BOOLEAN", "reject_gate": "VARCHAR", "reject_reason": "VARCHAR",
    "baseline_latency": "DOUBLE", "candidate_latency": "DOUBLE",
    "improvement_pct": "DOUBLE", "cost_usd": "DOUBLE",
}
assert tuple(_DUCK_TYPES) == _FIELD_NAMES, "DuckDB schema drifted from EpisodicEvent"


class DuckDBEpisodicStore:
    """Embedded analytical store. Single-writer — use from the loop driver only.

    Exposes the same port as the JSONL store, plus ``sql()`` for ad-hoc analytics
    (e.g. reject-rate by gate over time). ``duckdb`` is an optional dependency.
    """

    def __init__(self, path: Path = EPISODIC_DUCKDB) -> None:
        import duckdb

        path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(path))
        cols = ", ".join(f"{name} {dtype}" for name, dtype in _DUCK_TYPES.items())
        self._con.execute(f"CREATE TABLE IF NOT EXISTS episodes ({cols})")
        # Latest-wins key/value state (e.g. persisted CEO brake state, L9).
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS kv_state (key VARCHAR PRIMARY KEY, value VARCHAR)")

    def append(self, event: EpisodicEvent) -> None:
        cols = ", ".join(_FIELD_NAMES)
        placeholders = ", ".join("?" for _ in _FIELD_NAMES)
        self._con.execute(
            f"INSERT INTO episodes ({cols}) VALUES ({placeholders})",
            [getattr(event, name) for name in _FIELD_NAMES],
        )

    def events(self) -> list[EpisodicEvent]:
        cols = ", ".join(_FIELD_NAMES)
        rows = self._con.execute(f"SELECT {cols} FROM episodes ORDER BY ts").fetchall()
        return [EpisodicEvent(*row) for row in rows]

    def summary(self) -> dict[str, Any]:
        return summarize(self.events())

    def save_state(self, key: str, value: dict[str, Any]) -> None:
        self._con.execute(
            "INSERT INTO kv_state (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [key, json.dumps(value)],
        )

    def load_state(self, key: str) -> dict[str, Any] | None:
        row = self._con.execute(
            "SELECT value FROM kv_state WHERE key = ?", [key]).fetchone()
        if row is None:
            return None
        parsed: Any = json.loads(row[0])
        return parsed if isinstance(parsed, dict) else None

    def sql(self, query: str) -> list[tuple[Any, ...]]:
        """Run an ad-hoc analytical query against the `episodes` table."""
        # Annotated local (not cast): duckdb is an optional dep, so its return
        # type is `Any` when the extra isn't installed (CI) but concrete when it
        # is (local) — a cast would be "redundant" in one and required in the other.
        rows: list[tuple[Any, ...]] = self._con.execute(query).fetchall()
        return rows


def get_episodic_store(kind: str | None = None) -> EpisodicStore:
    """Return the configured store (``SIS_EPISODIC_STORE``: jsonl|duckdb|none)."""
    kind = (kind or os.getenv("SIS_EPISODIC_STORE") or "jsonl").lower()
    if kind == "none":
        return NullEpisodicStore()
    if kind == "duckdb":
        return DuckDBEpisodicStore()
    return JsonlEpisodicStore()


# --------------------------------------------------------------------------
# Mapping a control-loop result → an episodic event
# --------------------------------------------------------------------------


def gate_from_reason(reason: str | None) -> str | None:
    """Best-effort: which gauntlet gate produced a rejection reason."""
    if not reason:
        return None
    r = reason.lower()
    # Timeout first: a timed-out gate's reason names the gate ("mypy gate timed
    # out"), so this must win over the gate-name checks below (L12).
    if "timed out" in r or "timeout" in r:
        return "timeout"
    # Harness faults next: their reason names the gate that could not run
    # ("... the pytest gate cannot run"), so this must win over the gate-name
    # checks below — otherwise a broken harness is counted as a candidate that
    # failed pytest, and the analytics blame the wrong side.
    if r.startswith("harness:"):
        return "harness"
    # The contract's interface gate: the candidate has the wrong shape, which is
    # a distinct failure from "its behaviour is wrong" (CLASS2_CONTRACT.md).
    if r.startswith("interface:"):
        return "interface"
    # The backtest gate (sis/backtest.py): the candidate is well-formed and
    # passes its acceptance tests, but does not reproduce a recorded episode.
    # Distinct from "correctness" — that gate compares against a reference
    # oracle evaluated on demand, this one against history that actually
    # happened, and for a world-model the second is the evidence that counts.
    if r.startswith("backtest failed"):
        return "backtest"
    # The offline invariant gate (sis/invariant.py). The reason says "in
    # sandbox" specifically because the canary's own violation reason starts
    # "invariant violated" too — the same predicates, applied to live traffic.
    # A shared prefix here would shadow the canary rule below and silently
    # report every live violation as an offline one, losing exactly the
    # sandbox-vs-production distinction the canary exists to add.
    if r.startswith("invariant violated in sandbox"):
        return "invariant"
    # The canary's own gates (sis/canary.py: evaluate_canary), each a live
    # analogue of an offline concept but named distinctly — conflating a live
    # regression with an offline "no improvement" (or a live invariant
    # violation with the offline differential-correctness "correctness" gate)
    # would make rejected_by_gate ambiguous about whether the sandbox or live
    # traffic caught it, which is exactly the distinction the canary adds.
    if r.startswith("insufficient evidence"):
        return "canary_evidence"
    if r.startswith("invariant violated"):
        return "canary_invariant"
    if r.startswith("response disagreement"):
        return "canary_disagreement"
    if r.startswith("live "):  # "live p95/p99 regression: ..."
        return "canary_regression"
    if "syntaxerror" in r:
        return "ast"
    if "no change" in r:  # candidate identical to the baseline (no-op)
        return "noop"
    if "mypy" in r:
        return "mypy"
    # The contract's trusted acceptance tests. Renamed from `pytest` in OMNI-17:
    # the gate is named for what it checks (the spec's cases) rather than for
    # the tool that happens to run them, which matters once a ToolchainAdapter
    # can run junit or cargo test instead. Rows written before that rename still
    # read `pytest`, and the rule below still recognises the old reason string,
    # so `rejected_by_gate` shows both names across the transition rather than
    # silently losing the older half.
    if r.startswith("acceptance"):
        return "acceptance"
    if "pytest" in r:
        return "pytest"
    if "correctness mismatch" in r:
        return "correctness"
    if "no improvement" in r:
        return "benchmark"
    if "policy" in r:
        return "policy"
    return None


def event_from_cycle_result(
    result: dict[str, Any], *, cost_usd: float = 0.0,
    proposer: str = "stub", model: str | None = None,
    clock: Clock | None = None,
) -> EpisodicEvent:
    """Build an EpisodicEvent from an ``org.run_cycle`` result dict.

    *clock* defaults to the wall clock, so every existing caller is unchanged;
    pass one only when replaying (see :func:`_now`).
    """
    status = str(result.get("status", "unknown"))
    base = result.get("baseline_latency")
    cand = result.get("candidate_latency")
    improvement = (
        round((1 - cand / base) * 100, 1)
        if isinstance(base, int | float) and isinstance(cand, int | float) and base
        else None
    )
    reason = result.get("reason")
    gauntlet_passed = (
        True if status in ACCEPTED_OUTCOMES
        else False if status in ("rolled_back", "no_change")
        else None
    )
    return EpisodicEvent(
        cycle_id=EpisodicEvent.new_cycle_id(),
        ts=_now(clock),
        outcome=status,
        proposer=proposer,
        model=model,
        spec_id=_opt_str(result.get("spec_id")),
        story_id=_opt_str(result.get("story_id")),
        pr_id=_opt_str(result.get("pr_id")),
        candidate_sha=_opt_str(result.get("candidate_sha")),
        gauntlet_passed=gauntlet_passed,
        reject_gate=gate_from_reason(reason if isinstance(reason, str) else None),
        reject_reason=reason if isinstance(reason, str) else None,
        baseline_latency=base if isinstance(base, int | float) else None,
        candidate_latency=cand if isinstance(cand, int | float) else None,
        improvement_pct=improvement,
        cost_usd=cost_usd,
    )


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)
