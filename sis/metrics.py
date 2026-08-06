"""sis.metrics — pure latency/percentile helpers for the canary path.

Kept separate from the adapters on purpose, per the project convention that
decision logic lives in pure functions and I/O lives in the actors/adapters:
``InMemoryCloud.live_metrics`` (and later ``ServeCloud``) computes a window
here, and ``evaluate_canary`` compares two windows with the same code. One
implementation, unit-testable without Ray, Serve, or a clock.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# The keys every Cloud.live_metrics() implementation returns, so callers can
# rely on the shape regardless of which adapter produced it.
METRIC_KEYS: tuple[str, ...] = ("p50", "p95", "p99", "error_rate", "samples")


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile of ``values`` (``p`` in 0..100).

    Nearest-rank rather than an interpolating definition because a canary
    window is a sample of *observed* request latencies: every value returned is
    one that actually happened, which is the honest thing to compare against an
    SLO. It is also defined for a single sample, where interpolating variants
    (including ``statistics.quantiles``) raise — and a canary window with one
    request in it is a real state the loop has to report on, not an error.
    """
    if not 0 <= p <= 100:
        raise ValueError(f"percentile p must be in 0..100, got {p}")
    if not values:
        return 0.0
    ordered = sorted(values)
    # Rank is 1-based: p=0 → first element, p=100 → last.
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class Window:
    """A summarised window of observed requests for one deployed version."""

    p50: float
    p95: float
    p99: float
    error_rate: float
    samples: int

    def as_metrics(self) -> dict[str, float]:
        """The ``Cloud.live_metrics()`` wire shape (all floats, see METRIC_KEYS)."""
        return {"p50": self.p50, "p95": self.p95, "p99": self.p99,
                "error_rate": self.error_rate, "samples": float(self.samples)}


def summarise(latencies: Sequence[float], *, errors: int = 0) -> Window:
    """Summarise observed latencies (seconds) + an error count into a Window.

    An empty window is all-zeros with ``samples=0`` rather than an exception:
    "no traffic yet" is a normal state early in a canary, and the *caller*
    decides whether the sample count clears its floor (see the window-sizing
    open problem in docs/SERVE_CANARY.md). Reporting zeros keeps that decision
    in one place instead of forcing every caller into a try/except.
    """
    total = len(latencies)
    return Window(
        p50=percentile(latencies, 50),
        p95=percentile(latencies, 95),
        p99=percentile(latencies, 99),
        error_rate=(errors / total) if total else 0.0,
        samples=total,
    )
