"""sis.slo — a latency budget declared by the spec (OMNI-24).

**A budget, not a definition of correctness.** Every correctness gate —
acceptance, invariants, backtest — has already passed by the time this one runs.
A candidate that is correct and slow fails here, and that is a *different*
verdict from a candidate that is wrong: its reject gate is ``slo``, and the
CEO's circuit breaker counts it at a reduced weight (``brakes.slo_failure_weight``)
rather than as a full failure. Too slow can be non-functional, so it still
counts; it is not the same thing as broken.

It is also deliberately not a second benchmark. The Class-1 benchmark asks "is
it *better* than the baseline?" by timing both over the same workload; this asks
"is it *within budget*?" against an absolute number the spec states. Conflating
the two is how the fixed-input timing fragility the live canary replaced (L5:
~30% jitter flipping accept/reject) would come back.

Three choices keep it cheap and stable:

- **A fixed workload.** The same inputs every run, either written into the
  contract (``inputs``) or produced by a named, deterministic function in the
  contract's oracle module (``workload``), resolved inside the sandbox exactly
  like a backtest comparator. Random inputs would make a percentile noisy.
- **Best of up to ``max_repeats`` per input, stopping at the first run under
  budget.** An input is within budget iff its best run is, and any single run
  under budget proves that — so a fast candidate costs one timed call per input,
  and only a slow one pays for the retries that stop a single noisy run failing
  it.
- **Last in the profile.** It runs only after every correctness gate has
  passed, so a wrong candidate never pays for timing at all.

An absolute budget is machine-dependent: the docker sandbox's CPU limit
(``sandbox.cpus``) changes timings. Budgets need headroom over what the
reference machine measures.

Pure like :mod:`sis.backtest`: dataclass, validation, a script builder and the
verdict. Sandbox execution lives in :mod:`sis.gauntlet`.
"""

from __future__ import annotations

import ast
import math
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# Only latency today. Accuracy is already decided by the correctness gates, and
# a softer accuracy target would be an advisory signal, not a budget gate.
METRICS: tuple[str, ...] = ("latency",)

DEFAULT_PERCENTILE = 95.0
DEFAULT_MAX_REPEATS = 3

EXIT_NO_ENTRY = 4     # candidate does not export the contract's entry point
EXIT_NO_WORKLOAD = 5  # the named workload function does not exist (harness fault)
EXIT_BAD_WORKLOAD = 6  # the workload returned something that is not a list of arg tuples
EXIT_RAISED = 7       # the candidate raised on a workload input


@dataclass(frozen=True)
class DomainSLO:
    """A latency budget over a fixed workload.

    Exactly one of *inputs* and *workload* is set. *inputs* is a tuple of
    argument tuples, each passed as ``entry(*args)`` — the same convention as a
    Class-1 oracle's ``BENCH_INPUTS``. They must be Python literals, because
    they reach the sandbox via ``repr`` and ``ast.literal_eval``, never
    ``eval`` (the same rule the contract-author applies to spec examples).
    *workload* names a zero-argument function in the contract's oracle module
    that returns such a sequence; it must be deterministic, or the budget is
    judged against a different workload each run.
    """

    budget_ms: float
    inputs: tuple[tuple[Any, ...], ...] | None = None
    workload: str | None = None
    metric: str = "latency"
    percentile: float = DEFAULT_PERCENTILE
    max_repeats: int = DEFAULT_MAX_REPEATS

    def __post_init__(self) -> None:
        if self.metric not in METRICS:
            raise ValueError(
                f"DomainSLO metric {self.metric!r} is not supported (one of {METRICS}); "
                "correctness is judged by the acceptance/invariant/backtest gates"
            )
        if not self.budget_ms > 0:
            raise ValueError(f"DomainSLO budget_ms must be > 0, got {self.budget_ms}")
        if not 0.0 < self.percentile <= 100.0:
            raise ValueError(f"DomainSLO percentile must be in (0, 100], got {self.percentile}")
        if self.max_repeats < 1:
            raise ValueError(f"DomainSLO max_repeats must be >= 1, got {self.max_repeats}")
        if (self.inputs is None) == (self.workload is None):
            raise ValueError(
                "DomainSLO needs exactly one of `inputs` (literal argument tuples) "
                "or `workload` (a function in the contract's oracle module)"
            )
        if self.workload is not None and not self.workload.isidentifier():
            raise ValueError(f"DomainSLO workload must be a function name, got {self.workload!r}")
        if self.inputs is not None:
            if not self.inputs:
                raise ValueError("DomainSLO inputs must not be empty")
            if not all(isinstance(args, tuple) for args in self.inputs):
                raise ValueError("DomainSLO inputs must be a tuple of argument tuples")
            try:
                round_tripped = ast.literal_eval(repr(self.inputs))
            except (ValueError, SyntaxError) as exc:
                raise ValueError(
                    "DomainSLO inputs must be Python literals (they reach the sandbox "
                    f"via repr + ast.literal_eval): {exc}"
                ) from exc
            if round_tripped != self.inputs:
                raise ValueError(
                    "DomainSLO inputs do not survive repr + ast.literal_eval unchanged"
                )

    @property
    def budget_seconds(self) -> float:
        return self.budget_ms / 1000.0


def percentile_value(values: Sequence[float], percentile: float) -> float:
    """Nearest-rank percentile. Pure; ``values`` must be non-empty.

    Nearest-rank rather than interpolated so the reported number is a latency
    that was actually measured on some input, which is what a reader of the
    rejection wants to go and look at.
    """
    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100.0 * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class SloVerdict:
    passed: bool
    observed_seconds: float
    reason: str


def evaluate_slo(slo: DomainSLO, best_seconds: Sequence[float]) -> SloVerdict:
    """Judge per-input best times against the budget. Pure.

    The reason wording is load-bearing: :func:`sis.episodic.gate_from_reason`
    maps the ``slo exceeded`` prefix to the ``slo`` reject gate, and the CEO
    weights that gate differently from a correctness failure.
    """
    observed = percentile_value(best_seconds, slo.percentile)
    budget = slo.budget_seconds
    if observed <= budget:
        return SloVerdict(True, observed, "")
    over = [i for i, t in enumerate(best_seconds) if t > budget]
    worst = max(range(len(best_seconds)), key=lambda i: best_seconds[i])
    return SloVerdict(
        False,
        observed,
        f"slo exceeded: p{slo.percentile:g} latency {observed * 1000:.3f}ms is over the "
        f"{slo.budget_ms:g}ms budget ({len(over)}/{len(best_seconds)} workload inputs over "
        f"budget; slowest is input #{worst} at {best_seconds[worst] * 1000:.3f}ms, best of "
        f"up to {slo.max_repeats} runs) — correct but over budget",
    )


def build_script(
    *,
    candidate_path: str,
    oracle_path: str | None,
    entry: str,
    slo: DomainSLO,
) -> str:
    """Build the in-sandbox timing script. Pure string building.

    Prints ``TIMINGS <json list of per-input best seconds>`` on success. The
    verdict is computed back in the main process by :func:`evaluate_slo`, so
    the thresholds live in one tested place rather than inside a string.
    """
    return textwrap.dedent(
        f"""\
        import sys, json, time, ast, importlib.util

        def _load(path, name):
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        cand = _load({candidate_path!r}, "candidate")
        oracle_path = {oracle_path!r}

        entry_fn = getattr(cand, {entry!r}, None)
        if not callable(entry_fn):
            print("NOENTRY", {entry!r})
            sys.exit({EXIT_NO_ENTRY})

        workload_name = {slo.workload!r}
        if workload_name is None:
            inputs = ast.literal_eval({repr(slo.inputs)!r})
        else:
            oracle = _load(oracle_path, "oracle") if oracle_path else None
            make = getattr(oracle, workload_name, None) if oracle else None
            if not callable(make):
                print("NOWORKLOAD", workload_name)
                sys.exit({EXIT_NO_WORKLOAD})
            try:
                inputs = [tuple(args) for args in make()]
            except Exception as exc:
                print("BADWORKLOAD", workload_name, repr(exc))
                sys.exit({EXIT_BAD_WORKLOAD})
            if not inputs:
                print("BADWORKLOAD", workload_name, "returned no inputs")
                sys.exit({EXIT_BAD_WORKLOAD})

        budget = {slo.budget_seconds!r}
        max_repeats = {slo.max_repeats!r}

        # One warm-up call, so first-call costs (lazy imports, caches) are not
        # charged to whichever input happens to come first.
        try:
            entry_fn(*inputs[0])
        except Exception as exc:
            print("RAISED", 0, repr(exc))
            sys.exit({EXIT_RAISED})

        bests = []
        for index, args in enumerate(inputs):
            best = float("inf")
            for _ in range(max_repeats):
                start = time.perf_counter()
                try:
                    entry_fn(*args)
                except Exception as exc:
                    print("RAISED", index, repr(exc))
                    sys.exit({EXIT_RAISED})
                best = min(best, time.perf_counter() - start)
                if best <= budget:
                    break  # one run under budget proves this input is within it
            bests.append(best)

        print("TIMINGS", json.dumps(bests))
        """
    )
