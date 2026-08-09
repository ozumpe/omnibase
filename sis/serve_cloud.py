"""sis.serve_cloud — the ``Cloud`` port backed by a real Ray Serve deployment.

The third ``Cloud`` implementation, and the first one where "deploy a canary"
means a process actually starts answering requests:

- :class:`~sis.adapters.InMemoryCloud` — a working *model* of a weighted canary
  (weight map + ``observe()`` → windowed percentiles). No traffic, but enough
  behaviour to build and test the canary flow with no Ray Serve running.
- ``RealCloud`` (``sis.adapters_real``) — records deployments; raises on
  ``shift_traffic``/``live_metrics`` rather than no-op'ing, because a silent
  no-op would let a real run report a "passing canary" that never routed a
  request.
- **``ServeCloud`` (here)** — real Serve deployments, a real weighted split, real
  shadow dispatch, real per-version latency windows.

``docs/SERVE_CANARY.md`` sketched this into ``sis/adapters_real.py``. It lives in
its own module instead: ``adapters_real`` is the credential-carrying tier
(``--with real``: requests/boto3/pyyaml, Confluence/Jira/GitHub), and this needs
none of that — only ``ray[serve]``, which is a core dependency. Keeping them
apart means serving a canary locally does not drag in a credential path, and
vice versa.

**Why the port's ``deploy_canary`` grew an optional ``source``.** Every other
adapter records a *version string*; this one has to run actual code, and the
candidate source lives on the PR artifact. It is keyword-only with a default, so
``ServeCloud`` still satisfies the ``Cloud`` protocol unchanged — and it raises
rather than defaults when it is missing, because a green slot serving the same
source as blue is a canary that can only ever report "no difference".

**Promotion here is a real redeployment**, not a bookkeeping entry: blue is
re-run with the candidate's source and the green application is torn down. The
human gate is unchanged and lives upstream — ``Cloud.promote``'s only caller is
``DevOps.observe_merge()``, which acts solely after reading a PR back as merged
(OMNI-15). Nothing here decides to promote.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING, Any, Protocol

from sis.canary import CanaryMode, LiveSample
from sis.contract import OptimizationContract
from sis.metrics import summarise
from sis.ports import DeployRecord
from sis.serving import app_name, build_canary, build_candidate, route_prefix

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sis.loadgen import LoadReport


class SupportsEmit(Protocol):
    """The one method ``ServeCloud`` needs from a telemetry sink.

    Narrowed from the concrete ``InMemoryTelemetry`` on purpose (OMNI-14): a
    role actor holds a *Ray handle* to ``Workspace``, not the raw adapter
    instance living inside it, so it cannot hand `ServeCloud` an
    ``InMemoryTelemetry`` and have events land in the shared audit trail. A
    thin shim forwarding ``emit`` through ``Workspace.emit.remote(...)``
    satisfies this Protocol without ``ServeCloud`` needing to know the
    difference — same reasoning as ``loadgen.SupportsObserve``.
    """

    def emit(self, event: str, **fields: object) -> None: ...


# Where a canary starts: a small slice of traffic, so a candidate that is bad in
# a way every offline gate missed damages a few requests rather than all of
# them. Ramping up is a deliberate act (``shift_traffic``), not a default.
DEFAULT_CANARY_WEIGHT = 0.05

# What "live metrics" covers when a caller doesn't say. Minutes, not seconds:
# a percentile needs a population (see the noise-floor discussion in
# docs/SERVE_CANARY.md), and the router buffers well past this.
DEFAULT_WINDOW_S = 300.0


class ServeCloud:
    """``Cloud`` over Ray Serve: weighted split, shadow dispatch, live windows.

    **Two Serve applications, and the split between them is the design.** The
    public one holds a :class:`~sis.serving.CanaryRouter` in front of blue; the
    candidate is a second, unrouted application the router *attaches* to.

    The obvious alternative — one application containing router, blue and green
    — was built first and measured: re-running that graph to add a canary
    **restarts the blue replica** (its construction identity changes; Serve
    drains gracefully, so nothing is dropped, but the process cycles). Cycling
    the stable version in order to start observing a candidate throws away
    blue's warm state at the exact moment blue becomes the baseline under
    comparison. So green stands up on its own, and deploy/ramp/rollback are
    control-plane calls on the router rather than redeploys. Blue is never
    touched between ``serve_blue()`` calls.

    Ray must already be initialised and Serve started; this adapter does not own
    the cluster's lifecycle. ``serve_blue()`` stands the baseline up.
    """

    def __init__(
        self,
        telemetry: SupportsEmit,
        contract: OptimizationContract,
        *,
        mode: CanaryMode = CanaryMode.SHADOW,
        initial_weight: float = DEFAULT_CANARY_WEIGHT,
        scrub_green_env: bool = True,
    ) -> None:
        self._tel = telemetry
        self._contract = contract
        self._mode = mode
        self._initial_weight = initial_weight
        self._scrub = scrub_green_env

        self._blue_version: str | None = None
        self._blue_source: str | None = None
        self._green_version: str | None = None
        self._green_source: str | None = None
        self._router: Any = None
        self._records: list[DeployRecord] = []

    # --- lifecycle -------------------------------------------------------

    @property
    def app_name(self) -> str:
        return app_name(self._contract, "canary")

    @property
    def green_app_name(self) -> str:
        return app_name(self._contract, "green")

    @property
    def route_prefix(self) -> str:
        return route_prefix(self._contract, "blue")

    def serve_blue(self, *, source: str | None = None, version: str = "live") -> DeployRecord:
        """Stand up the baseline. ``source`` defaults to the committed target.

        Calling it again redeploys with a new baseline — which is exactly what
        :meth:`promote` does once a human merge has been observed.
        """
        from ray import serve

        self._blue_source = (
            source if source is not None
            else pathlib.Path(self._contract.target_file).read_text(encoding="utf-8")
        )
        self._blue_version = version
        self._router = serve.run(
            build_canary(self._contract, blue_source=self._blue_source,
                         blue_version=version, mode=self._mode),
            name=self.app_name,
            route_prefix=self.route_prefix,
        )
        record = DeployRecord(version=version, slot="blue", live=True)
        self._records.append(record)
        self._tel.emit("serve.blue_deployed", version=version, app=self.app_name)
        return record

    def shutdown(self) -> None:
        """Tear both applications down. Leaves the Serve instance running."""
        from ray import serve

        if self._green_version is not None:
            serve.delete(self.green_app_name)
            self._green_version = self._green_source = None
        if self._router is not None:
            serve.delete(self.app_name)
            self._router = None
        self._tel.emit("serve.shutdown", app=self.app_name)

    def _require_router(self) -> Any:
        if self._router is None:
            raise RuntimeError(
                "ServeCloud has nothing deployed — call serve_blue() first "
                "(it needs a baseline to route to, and to compare a canary against)"
            )
        return self._router

    # --- Cloud port ------------------------------------------------------

    def deploy_canary(
        self,
        version: str,
        *,
        metrics: dict[str, float] | None = None,
        source: str | None = None,
    ) -> DeployRecord:
        """Deploy ``source`` as green at :data:`DEFAULT_CANARY_WEIGHT`.

        ``metrics`` is the offline verdict (the gauntlet's sandboxed benchmark)
        carried onto the record for provenance; it is *not* what the canary
        judges on — that is the whole point of measuring again under live load.
        """
        from ray import serve

        if source is None:
            raise ValueError(
                "ServeCloud.deploy_canary needs the candidate's source: a green "
                "slot running the same code as blue is a canary that can only "
                "ever report 'no difference'. Pass source=<PR artifact>."
            )
        router = self._require_router()
        self._green_source = source
        self._green_version = version

        # Green is its own application, so standing it up does not re-run the
        # blue graph. route_prefix=None keeps it off the public path: shadow
        # traffic must reach it through the router, never directly.
        serve.run(
            build_candidate(self._contract, source=source, version=version,
                            scrub_env=self._scrub),
            name=self.green_app_name,
            route_prefix=None,
        )
        # A fresh canary starts from an empty window: observations from the
        # previous candidate would otherwise be summarised into this one's
        # verdict under the same blue version key.
        router.reset.remote().result()
        router.attach_green.remote(
            self.green_app_name, version, self._initial_weight).result()

        record = DeployRecord(
            version=version, slot="green", live=False, metrics=dict(metrics or {}))
        self._records.append(record)
        self._tel.emit("canary.deployed", version=version, slot="green",
                       weight=self._initial_weight, mode=self._mode.value,
                       metrics=record.metrics)
        return record

    def shift_traffic(self, version: str, fraction: float) -> None:
        """Route ``fraction`` of traffic to ``version``.

        Naming the *blue* version is accepted and mirrored (blue at 0.9 means
        green at 0.1), so a caller can express a ramp from either side without
        having to know which slot it is holding.
        """
        router = self._require_router()
        if version == self._green_version:
            green_fraction = fraction
        elif version == self._blue_version:
            green_fraction = 1.0 - fraction
        else:
            raise ValueError(
                f"unknown version {version!r}; deployed: "
                f"blue={self._blue_version!r} green={self._green_version!r}"
            )
        router.set_weight.remote(green_fraction).result()
        self._tel.emit("canary.traffic_shifted", version=version, fraction=fraction)

    def set_mode(self, mode: CanaryMode) -> None:
        """Switch shadow/split dispatch in place (not on the ``Cloud`` port).

        Changing it by redeploying would restart the router and throw away the
        window it has accumulated, which is the one thing a canary cannot spare.
        """
        self._mode = mode
        self._require_router().set_mode.remote(mode).result()

    def live_window(
        self, version: str, window_s: float = DEFAULT_WINDOW_S
    ) -> tuple[list[float], int]:
        """Raw ``(latencies, error_count)`` for ``version`` over the window.

        Not on the ``Cloud`` port, and needed alongside :meth:`live_metrics`
        because the two consumers want different things: the loop's breach
        trigger wants a summarised snapshot, while ``evaluate_canary`` compares
        latency *arrays* — it computes its own percentiles so that the offline
        and online gates use one definition. Handing it p95/p99 would force it
        to trust someone else's arithmetic.

        The window is filtered inside the router, where the observations were
        timestamped, so no clock is ever compared across two processes.
        """
        window: tuple[list[float], int] = (
            self._require_router().window.remote(version, window_s).result())
        return window

    def live_metrics(self, version: str, window_s: float = DEFAULT_WINDOW_S) -> dict[str, float]:
        """Observed p50/p95/p99 + error rate for ``version``, from real traffic."""
        latencies, errors = self.live_window(version, window_s)
        return summarise(latencies, errors=errors).as_metrics()

    def live_samples(self, limit: int | None = None) -> list[LiveSample]:
        """Paired (request, blue answer, green answer) records for
        ``evaluate_canary``. Empty under ``SPLIT`` — nothing pairs there."""
        samples: list[LiveSample] = self._require_router().samples.remote(limit).result()
        return samples

    def warm_up(self, count: int = 150, *, concurrency: int = 8) -> LoadReport:
        """Drive *count* requests from the contract's oracle through the router.

        The bootstrap answer to "where does a canary's live traffic come
        from" (``docs/SERVE_CANARY.md``): nothing external calls the served
        target yet, so a caller collecting a window has to fill it
        synthetically — the same role ``sis.loadgen`` already plays for manual
        testing (RUNBOOK Level 0c/0d), now called from inside the canary flow
        instead of by hand. Uses the direct handle path (``CanaryRouter.invoke``),
        not HTTP, since the caller and the router share a process.

        Not on the ``Cloud`` port: no other adapter needs a traffic source, and
        an in-memory or recording adapter has no window to fill.
        """
        from sis import loadgen

        inputs = loadgen.inputs_for(self._contract, count)
        return loadgen.drive_handle(
            self._require_router(), inputs,
            version=str(self._green_version), concurrency=concurrency)

    def promote(self, version: str) -> DeployRecord:
        """Make the candidate the new baseline: green's source becomes blue's.

        See ``Cloud.promote`` — the human gate is the observed merge, upstream
        in ``DevOps.observe_merge()``. Here promotion is a real redeployment
        rather than a bookkeeping entry: blue is re-run with the candidate's
        source, and the green application is torn down.

        Blue *does* restart here, unavoidably and correctly — it is becoming
        different code. That is the opposite of the canary-deploy case, where a
        restart was a defect precisely because nothing about blue had changed.
        """
        from ray import serve

        if version != self._green_version or self._green_source is None:
            raise ValueError(
                f"cannot promote {version!r}: it is not the deployed candidate "
                f"(green={self._green_version!r}). Promotion moves the *canary* "
                "into the live slot."
            )
        # Re-run blue with the candidate's source, then retire green. Ordered
        # this way so there is no instant where the route has nothing behind it.
        candidate = self._green_source
        record = self.serve_blue(source=candidate, version=version)
        serve.delete(self.green_app_name)
        self._green_source = self._green_version = None
        self._tel.emit("canary.promoted", version=version, slot="blue")
        return record

    def rollback(self, version: str) -> None:
        """Stop green's traffic, then delete the candidate application.

        Ordered on purpose: the router detaches *first*, so by the time the
        deployment is torn down no request can still be routed to it. Doing it
        the other way round leaves a window where the router holds a handle to
        a dying replica.
        """
        from ray import serve

        if self._router is not None and self._green_version is not None:
            self._router.detach_green.remote().result()
            serve.delete(self.green_app_name)
            self._green_source = self._green_version = None
        self._tel.emit("canary.rolledback", version=version)

    def live_version(self) -> str | None:
        return self._blue_version

    # --- inspection (not part of the port) -------------------------------

    def status(self) -> dict[str, Any]:
        """What is deployed right now — for the RUNBOOK and for tests."""
        router = self._require_router()
        status: dict[str, Any] = router.status.remote().result()
        return status

    def deploys(self) -> list[DeployRecord]:
        return list(self._records)


def _main() -> None:
    """``python -m sis.serve_cloud`` — the whole online path, once.

    Stands blue and a candidate up, drives real concurrent load through the
    router, and runs the live window through ``evaluate_canary``. The first
    time serving, load generation, live metrics and the verdict all meet
    (docs/RUNBOOK.md, Level 0d).
    """
    import argparse
    import logging

    import ray
    from ray import serve

    from sis import loadgen
    from sis.adapters import InMemoryTelemetry
    from sis.canary import evaluate_canary
    from sis.contract import DEFAULT_CONTRACTS

    parser = argparse.ArgumentParser(description="Run one canary end to end.")
    parser.add_argument("--contract", default="sort")
    parser.add_argument("-n", "--requests", type=int, default=300)
    parser.add_argument("-c", "--concurrency", type=int, default=8)
    parser.add_argument("--mode", choices=[m.value for m in CanaryMode],
                        default=CanaryMode.SHADOW.value)
    parser.add_argument("--weight", type=float, default=1.0,
                        help="fraction of traffic the candidate sees (1.0 = every "
                             "request is shadowed, which is what a demo wants)")
    args = parser.parse_args()

    contract = next((c for c in DEFAULT_CONTRACTS if c.name == args.contract), None)
    if contract is None:
        raise SystemExit(f"unknown contract {args.contract!r}; "
                         f"known: {[c.name for c in DEFAULT_CONTRACTS]}")
    if contract.stub_candidate_path is None:
        raise SystemExit(f"contract {contract.name!r} ships no stub candidate to canary")

    ray.init(logging_level=logging.ERROR, ignore_reinit_error=True)
    serve.start(logging_config={"log_level": "ERROR"})

    cloud = ServeCloud(InMemoryTelemetry(), contract, mode=CanaryMode(args.mode))
    cloud.serve_blue(version="blue-live")
    candidate = pathlib.Path(contract.stub_candidate_path).read_text(encoding="utf-8")
    cloud.deploy_canary("green-candidate", source=candidate)
    cloud.shift_traffic("green-candidate", args.weight)

    url = f"http://127.0.0.1:8000{cloud.route_prefix}"
    print(f"blue=blue-live green=green-candidate mode={args.mode} weight={args.weight}")
    print(f"driving {args.requests} requests at concurrency {args.concurrency} → {url}\n")
    report = loadgen.drive_http(
        url, loadgen.inputs_for(contract, args.requests),
        version="green-candidate", concurrency=args.concurrency)

    blue_lat, blue_err = cloud.live_window("blue-live")
    green_lat, green_err = cloud.live_window("green-candidate")
    for label, metrics in (("blue ", cloud.live_metrics("blue-live")),
                           ("green", cloud.live_metrics("green-candidate"))):
        print(f"  {label}  p50={metrics['p50'] * 1000:7.2f}ms  "
              f"p95={metrics['p95'] * 1000:7.2f}ms  p99={metrics['p99'] * 1000:7.2f}ms  "
              f"errors={metrics['error_rate']:.1%}  n={int(metrics['samples'])}")
    print(f"  client-side: {report.throughput:.0f} req/s over {report.wall_seconds:.2f}s")

    samples = cloud.live_samples()
    verdict = evaluate_canary(
        [], samples, blue_lat, green_lat,
        version="green-candidate", mode=CanaryMode(args.mode),
        min_samples=min(50, args.requests))
    print(f"\nverdict: {'PASS' if verdict.passed else 'FAIL'} — {verdict.reason}")
    print(f"  samples={verdict.samples} disagreements={verdict.response_disagreements} "
          f"blue_errors={blue_err} green_errors={green_err}")
    print("\n(passing is eligibility, not promotion — the human PR merge promotes)")

    cloud.shutdown()
    serve.shutdown()  # type: ignore[no-untyped-call]  # ray.serve ships no stub


if __name__ == "__main__":
    _main()
