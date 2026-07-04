"""sis.supervisor — named detached Ray actor (the "CTO").

Holds the SLO, cost budget, and circuit-breaker state. Drives one
improvement cycle: benchmark → propose → gauntlet → promote/rollback → log.
"""

import datetime
import os
import shutil
from typing import Any

import ray

from sis import episodic, gauntlet, proposer
from sis.paths import TARGET_BACKUP_PATH, TARGET_PATH
from sis.worker import WorkerActor

SLO_THRESHOLD_SECONDS = 0.0002  # trigger a cycle if baseline exceeds this (~200 µs)
MAX_CONSECUTIVE_FAILURES = 3


def _log_episode(store: episodic.EpisodicStore, result: dict[str, Any],
                 cost_usd: float = 0.0) -> None:
    """Record a micro-loop outcome to the shared episodic store (never fatal)."""
    try:
        kind = os.getenv("SIS_PROPOSER", "stub")
        store.append(episodic.event_from_cycle_result(
            result, cost_usd=cost_usd, proposer=kind,
            model=proposer.MODEL if kind == "claude" else None))
    except Exception:  # noqa: BLE001 - logging must not break the loop
        pass


@ray.remote
class SupervisorActor:
    """CTO-level supervisor. Call run_cycle() to execute one full loop."""

    def __init__(self) -> None:
        self._failures = 0
        self._worker = WorkerActor.remote()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------

    def run_cycle(self) -> dict[str, object]:
        """Run one full self-improvement cycle and return a summary dict."""
        if self._failures >= MAX_CONSECUTIVE_FAILURES:
            return {
                "status": "circuit_breaker_open",
                "consecutive_failures": self._failures,
            }

        store = episodic.get_episodic_store()

        # --- 1. Measure baseline ---
        baseline: float = ray.get(self._worker.benchmark.remote())
        print(f"[supervisor] baseline latency: {baseline:.6f}s  SLO: {SLO_THRESHOLD_SECONDS}s")

        if baseline <= SLO_THRESHOLD_SECONDS:
            result = {
                "status": "slo_met",
                "baseline_latency": baseline,
                "timestamp": _now(),
            }
            _log_episode(store, result)
            return result

        # --- 2. Propose ---
        current_source: str = ray.get(self._worker.get_source.remote())
        candidate_code = proposer.propose(current_source, baseline)
        cost_usd = proposer.last_cost_usd()

        # --- 3. Gauntlet ---
        report = gauntlet.validate(candidate_code, baseline)

        if not report.passed:
            self._failures += 1
            result = {
                "status": "rolled_back",
                "reason": report.reason,
                "errors": report.errors,
                "baseline_latency": baseline,
                "timestamp": _now(),
                "consecutive_failures": self._failures,
            }
            _log_episode(store, result, cost_usd)
            print(f"[supervisor] rolled back — {report.reason}")
            return result

        # --- 4. Promote ---
        self._failures = 0
        _deploy(candidate_code)
        ray.get(self._worker.reload.remote())
        after: float = ray.get(self._worker.benchmark.remote())

        result = {
            "status": "promoted",
            "baseline_latency": baseline,
            "new_latency": after,
            "improvement_pct": round((1 - after / baseline) * 100, 1),
            "timestamp": _now(),
        }
        _log_episode(store, result, cost_usd)
        print(
            f"[supervisor] promoted — {baseline:.6f}s → {after:.6f}s"
            f" ({result['improvement_pct']}% faster)"
        )
        return result


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _deploy(candidate_code: str) -> None:
    """Replace the live target with the validated candidate, keeping a backup."""
    shutil.copy(TARGET_PATH, TARGET_BACKUP_PATH)
    TARGET_PATH.write_text(candidate_code, encoding="utf-8")


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()
