"""sis.loadgen — concurrent synthetic traffic against a served target.

Nothing external calls omnibase's target, so a canary has no traffic to judge a
candidate on. This closes that bootstrap gap and keeps the whole canary path
runnable in CI.

**Concurrency is the point, not a detail.** A canary exists to see what the
offline benchmark structurally cannot, and contention is top of that list: the
gauntlet times one call at a time in a quiet sandbox, so queueing, lock
contention and GC pauses under parallel load are invisible to it. Latency here
is measured *client-side and end to end* — including time spent queued — because
that is what a caller actually experiences and what a p99 SLO is written
against.

## Where the inputs come from

From the contract's oracle (``random_input``), not a separate generator. That
function already defines "a valid input for this target", already produces args
tuples, and is already the trusted source the offline differential gate draws
from. A second definition would be free to drift from it — and since a canary's
verdict is only as good as its input distribution (see the sort contract's
oracle for how that bit once), the two must not disagree.

`docs/SERVE_CANARY.md` step 8 anticipated generating load from a Hypothesis
`strategy` on the contract's invariants instead. That is still worth doing when
the Class-2 `InvariantGate` (OMNI-18) introduces strategies for its own reasons
— shrinking a failing case is genuinely useful there. It buys a load generator
nothing today, and no invariant field exists yet, so Hypothesis stays unadded
rather than becoming a dependency with no caller.
"""

from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol

from sis.contract import OptimizationContract
from sis.metrics import Window, summarise


class SupportsObserve(Protocol):
    """The ``observe`` seam on :class:`~sis.adapters.InMemoryCloud`.

    Deliberately *not* part of the ``Cloud`` port: a real cloud learns its
    metrics from the serving layer, not from whoever generated the load. This
    Protocol lets the generator feed the in-memory fake without pretending
    ``observe`` is something every adapter owes.
    """

    def observe(self, version: str, latency_seconds: float, *, error: bool = False) -> None:
        ...


@dataclass(frozen=True)
class Observation:
    """One request, as the caller saw it."""

    latency: float
    response: Any
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None


@dataclass(frozen=True)
class LoadReport:
    """What one load run observed, from the caller's side of the wire.

    Held as per-request :class:`Observation` records rather than parallel
    lists, so latency, response and error/success stay bound to the *same*
    request. Errors are not the last N requests — they land wherever they land —
    and anything that pairs them up positionally will be quietly wrong.
    """

    version: str
    observations: list[Observation] = field(default_factory=list)
    wall_seconds: float = 0.0

    @property
    def requests(self) -> int:
        return len(self.observations)

    @property
    def latencies(self) -> list[float]:
        return [o.latency for o in self.observations]

    @property
    def responses(self) -> list[Any]:
        return [o.response for o in self.observations]

    @property
    def errors(self) -> list[str]:
        return [o.error for o in self.observations if o.error is not None]

    @property
    def error_count(self) -> int:
        return sum(1 for o in self.observations if o.failed)

    @property
    def throughput(self) -> float:
        """Requests per second actually achieved."""
        return self.requests / self.wall_seconds if self.wall_seconds else 0.0

    def window(self) -> Window:
        """Summarise into the same shape ``Cloud.live_metrics`` returns, so a
        load run and a live window are directly comparable."""
        return summarise(self.latencies, errors=self.error_count)

    def record_into(self, cloud: SupportsObserve) -> None:
        """Feed every observation to *cloud*, so ``live_metrics`` summarises
        real measurements rather than numbers a test invented."""
        for observation in self.observations:
            cloud.observe(self.version, observation.latency, error=observation.failed)


def inputs_for(
    contract: OptimizationContract, count: int, *, seed: int | None = None
) -> list[tuple[Any, ...]]:
    """*count* valid, varied argument tuples for *contract*'s entry function.

    *seed* makes a run reproducible; leaving it None draws from system entropy,
    which is the right default when the traffic is meant to be unpredictable.
    """
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    oracle = contract.load_oracle()
    rng = random.Random(seed)
    return [tuple(oracle.random_input(rng)) for _ in range(count)]


def _timed(call: Any, args: tuple[Any, ...]) -> tuple[float, Any, str | None]:
    """Run one request, returning (latency, response, error). Latency covers
    queueing too — the whole point of driving this concurrently."""
    start = time.perf_counter()
    try:
        response = call(args)
    except Exception as exc:  # noqa: BLE001 - a failed request is data, not a crash
        return time.perf_counter() - start, None, f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - start
    # The HTTP path reports a failed request in-band (see sis.serving: a bad
    # request must not kill the replica), so a 200 carrying {"error": ...} is
    # still an error for error-rate purposes.
    if isinstance(response, dict) and "error" in response:
        return elapsed, response, str(response["error"])
    return elapsed, response, None


def _drive(
    call: Any,
    inputs: list[tuple[Any, ...]],
    *,
    version: str,
    concurrency: int,
) -> LoadReport:
    """Run *inputs* through *call* with *concurrency* callers in flight."""
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        observations = [
            Observation(latency=latency, response=response, error=error)
            for latency, response, error in pool.map(lambda a: _timed(call, a), inputs)
        ]
    wall = time.perf_counter() - start

    return LoadReport(version=version, observations=observations, wall_seconds=wall)


def drive_handle(
    handle: Any,
    inputs: list[tuple[Any, ...]],
    *,
    version: str,
    concurrency: int = 8,
) -> LoadReport:
    """Drive a Serve ``DeploymentHandle`` directly — no HTTP, no JSON.

    The lower-overhead path, and the one a shadow comparison wants: with the
    transport out of the way, a latency difference between two versions is the
    versions differing, not the serialization.
    """
    return _drive(lambda args: handle.invoke.remote(*args).result(),
                  inputs, version=version, concurrency=concurrency)


def drive_http(
    url: str,
    inputs: list[tuple[Any, ...]],
    *,
    version: str,
    concurrency: int = 8,
    timeout: float = 10.0,
) -> LoadReport:
    """Drive the HTTP endpoint, the way a real client would.

    Measures what a caller actually experiences (transport included), which is
    what a p99 SLO is written against.
    """
    import requests

    session = requests.Session()

    def call(args: tuple[Any, ...]) -> Any:
        response = session.post(url, json={"args": list(args)}, timeout=timeout)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        return response.json()

    try:
        return _drive(call, inputs, version=version, concurrency=concurrency)
    finally:
        session.close()


def _main() -> None:
    """``python -m sis.loadgen`` — point it at a running server (RUNBOOK 0b)."""
    import argparse

    from sis.contract import DEFAULT_CONTRACTS

    parser = argparse.ArgumentParser(description="Generate load against a served target.")
    parser.add_argument("--url", default="http://127.0.0.1:8000/sort")
    parser.add_argument("--contract", default="sort",
                        help="which contract's oracle generates the inputs")
    parser.add_argument("-n", "--requests", type=int, default=200)
    parser.add_argument("-c", "--concurrency", type=int, default=8)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    contract = next((c for c in DEFAULT_CONTRACTS if c.name == args.contract), None)
    if contract is None:
        raise SystemExit(f"unknown contract {args.contract!r}; "
                         f"known: {[c.name for c in DEFAULT_CONTRACTS]}")

    payloads = inputs_for(contract, args.requests, seed=args.seed)
    report = drive_http(args.url, payloads, version=args.url,
                        concurrency=args.concurrency)
    w = report.window()
    print(f"{report.requests} requests, {args.concurrency} concurrent, "
          f"{report.throughput:.0f} req/s over {report.wall_seconds:.2f}s")
    print(f"  p50={w.p50 * 1000:.2f}ms  p95={w.p95 * 1000:.2f}ms  "
          f"p99={w.p99 * 1000:.2f}ms  error_rate={w.error_rate:.1%}")
    for error in report.errors[:3]:
        print(f"  error: {error}")


if __name__ == "__main__":
    _main()
