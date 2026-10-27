"""sis.serving — a contract's target behind Ray Serve.

The first thing in this codebase to actually *serve traffic*. Until now the loop
optimised a function nobody called; a canary needs somewhere for real requests
to go, and two versions to send them to. This module provides the deployment
(:class:`TargetDeployment`) and the weighted/shadow front end
(:class:`CanaryRouter`); the ``Cloud`` adapter that drives them is
``sis.serve_cloud.ServeCloud``.

**Stateless by construction.** A replica holds the entry function and nothing
else — no cache, no counters, no cross-request state. That is a deliberate
constraint, not an oversight: it lets the canary mechanics be proven without
also solving state hand-off between blue and green. Stateful targets (an LRU
cache, a rate limiter) are explicitly deferred until the mechanics work.

## Where the sandbox boundary is — read before extending this

A replica **never executes the source it is handed** (OMNI-48, H3). It starts
a :class:`sis.sandbox_worker.SandboxWorker` for that source — the gauntlet's
own sandbox, the one every offline gate already judged the candidate in — and
forwards each request over the worker's pipe. The replica is a Ray worker; the
candidate is not. Before this, a green replica ``exec``'d the candidate
in-process, so candidate code was a Ray worker in the control-plane cluster
and could ``ray.get_actor(..., namespace="sis")`` any detached role actor
before a human had reviewed it.

What the candidate can reach is then what the configured sandbox allows:

- **docker** (what a real proposer requires, M1): no Ray installed, no
  network, no host environment. The cluster is out of reach.
- **subprocess**, the soft sandbox: a scrubbed environment, but the host's
  packages and loopback. Ray's gRPC client does not go through the Python
  sockets the egress block patches, so a candidate that imports Ray and asks
  for ``address="auto"`` still finds the cluster. That is M1's reason for
  refusing it with a real proposer, and it applies here unchanged.

The driver's sandbox settings are bound into every deployment
(:func:`sandbox_settings`) rather than read in the replica, because green's
environment is scrubbed and a blank ``SIS_SANDBOX`` would fall through to
``config.yml`` — silently serving a candidate in the soft sandbox when the
driver had asked for docker.

The OMNI-13 environment scrub (:func:`scrubbed_env_vars`) stays on green's
replica as defence in depth: the replica now runs only trusted code, but it
is still the process that starts the candidate.

**Still open, deliberately:** :func:`sis.gauntlet.serve_canary_problem` still
refuses ``canary.backend=serve`` with any non-stub proposer (OMNI-49, M19).
Lifting that refusal is a separate, human decision.
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from collections import deque
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ray import serve

from sis import config
from sis.canary import CanaryMode, LiveSample
from sis.contract import OptimizationContract
from sis.gauntlet import ENV_ALLOWLIST
from sis.sandbox_worker import SandboxWorker, _rebuilt

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.requests import Request

# Slot names match SelfModel._slots and DeployRecord.slot, so a served
# deployment, a deploy record and the digital twin all say "green" about the
# same thing. Divergent vocabularies here would make the provenance graph and
# the live topology impossible to line up during an incident.
BLUE = "blue"
GREEN = "green"

# Every replica here — TargetDeployment or CanaryRouter — is I/O- and
# GIL-bound, not something that needs a physically dedicated core reserved at
# *scheduling* time. Left at Ray's default (1 whole CPU per actor, the same
# default the org's own role actors use), a live canary competes for integer
# CPU slots against the always-running actor org (CEO/PM/CTO/SWE/QA/DevOps/
# Designer/Workspace/SelfModel — 9 actors) plus Serve's own controller and
# HTTP proxy. On a machine with CPU to spare that competition is invisible;
# on a constrained one (a CI runner has far fewer cores than a workstation) it
# starves outright: `serve.run()` blocks forever waiting for a replica that
# can never be scheduled, with no timeout to surface the deadlock. Confirmed
# by reproducing under `ray.init(num_cpus=2)` — canary deploy/blue succeeded,
# adding the green replica hung indefinitely until this was set to 0.
_LIGHTWEIGHT_REPLICA: dict[str, Any] = {"num_cpus": 0}

# A Ray worker needs more of its environment than a bare gauntlet subprocess
# does — it has to import Ray and talk to the cluster before any of our code
# runs — so the replica allowlist widens the gauntlet's by these *operational*
# prefixes. None of them is credential-shaped, which is the property that
# matters: no secret this project handles (ANTHROPIC_/AWS_/ATLASSIAN_/GITHUB_/
# SIS_) begins with RAY_ or PYTHON.
_REPLICA_ENV_ALLOW_PREFIXES = ("RAY_", "PYTHON")
_REPLICA_ENV_ALLOW = (*ENV_ALLOWLIST, "HOME", "TMPDIR", "TEMP", "TMP", "VIRTUAL_ENV")

# How many observations/samples a router keeps. A canary window is minutes, not
# days, and an unbounded buffer in a long-lived replica is a slow leak.
DEFAULT_BUFFER = 20_000
DEFAULT_SAMPLE_BUFFER = 2_000


def scrubbed_env_vars() -> dict[str, str]:
    """Env-var overrides that blank every non-allowlisted variable.

    Fed to a green replica's ``runtime_env`` so candidate code cannot read a
    credential out of the environment it inherited from the driver.

    **Blanking rather than omitting is the whole trick.** Ray's
    ``runtime_env["env_vars"]`` *merges* with the worker's inherited
    environment — listing only the safe vars would leave every unlisted secret
    intact. So this returns an explicit ``""`` for each variable that is not
    allowlisted, which is an allowlist implemented through overrides.

    Two honest limits, both structural rather than oversights:

    - It can only blank what the *driver* can see. A variable exported on a
      remote worker node but not here is invisible to this function. Identical
      on a single node (local, CI), which is where this runs today.
    - It is not a filesystem boundary. ``~/.aws/credentials`` and
      ``secrets.local.yml`` stay readable — closing that needs the gauntlet's
      container, which a network-serving replica cannot be. See the module
      docstring.
    """
    return {
        name: ""
        for name in os.environ
        if name not in _REPLICA_ENV_ALLOW
        and not name.startswith(_REPLICA_ENV_ALLOW_PREFIXES)
    }


def sandbox_settings() -> dict[str, Any]:
    """The driver's effective sandbox configuration, for a replica's worker.

    Read here, on the driver, and bound into the deployment: a replica's own
    environment is not the driver's (green's is scrubbed, and any Ray worker
    snapshots its environment when it is created), so a replica that read its
    own configuration could start its candidate in a different sandbox from
    the one the driver chose. ``proposer.backend`` comes along so the worker's
    M1 check (``gauntlet.ensure_sandbox_ready``) sees the real proposer.
    """
    paths = [key.path for key in config.SCHEMA if key.path.startswith("sandbox.")]
    return {path: config.get(path) for path in (*paths, "proposer.backend")}


def _start_worker(source: str, entry: str, settings: Mapping[str, Any] | None) -> SandboxWorker:
    """Start *source* in the sandbox *settings* name and wait until it has loaded.

    Raises ``WorkerStartError`` (``missing`` names *entry*) for a source that
    does not export it, so a bad version fails at deploy time rather than at
    its first request.
    """
    if settings:
        config.apply_cli_overrides(settings)
    return SandboxWorker(source, entry).start()


@serve.deployment
class TargetDeployment:
    """Serves one contract's entry function over HTTP and via a handle.

    Two call paths on purpose:

    - ``invoke(*args)`` — the direct path, used by in-process callers holding a
      ``DeploymentHandle``. This is what the canary's shadow dispatch will use
      to send one request to *both* versions and compare answers, with no HTTP
      or JSON round-trip in the middle to muddy the latency comparison.
    - ``__call__(request)`` — the HTTP path, for real clients and the load
      generator (OMNI-12).

    The wire format is ``{"args": [...]}`` — the entry function's positional
    arguments as a list. That mirrors the oracle's ``random_input`` and
    ``BENCH_INPUTS``, which are already args *tuples*, so generated load and
    benchmark inputs feed this endpoint with no translation.
    """

    def __init__(
        self,
        source: str,
        entry: str,
        version: str,
        slot: str,
        sandbox: Mapping[str, Any] | None = None,
    ) -> None:
        self._source = source
        self._entry = entry
        self._version = version
        self._slot = slot
        self._sandbox = dict(sandbox) if sandbox is not None else None
        self._lock = threading.Lock()
        self._worker: SandboxWorker | None = _start_worker(source, entry, self._sandbox)

    def _call(self, args: tuple[Any, ...]) -> Any:
        """One call through the worker, restarting it first if it died.

        A worker is killed when a call outlives its timeout, and a candidate can
        exit its own process. Either way that request fails (an error the canary
        counts) and the next one gets a fresh worker: the version is stateless,
        so nothing is lost but the time to start it.
        """
        with self._lock:
            worker = self._worker
            if worker is None or not worker.running:
                if worker is not None:
                    worker.close()
                self._worker = None
                worker = self._worker = _start_worker(self._source, self._entry, self._sandbox)
        result = worker.call([list(args)]).results[0]
        if result.ok:
            return result.value
        raise _rebuilt(result)

    async def invoke(self, *args: Any) -> Any:
        """Call the served entry function. No state is kept between calls.

        The call waits on the worker's pipe, so it runs in a thread: a hung
        candidate must not stall the replica's event loop until its timeout.
        """
        return await asyncio.to_thread(self._call, args)

    def __del__(self) -> None:
        worker, self._worker = getattr(self, "_worker", None), None
        if worker is not None:
            worker.close()

    def info(self) -> dict[str, str]:
        """Which version/slot is answering — so a canary can attribute samples
        to a version rather than inferring it from routing."""
        return {"version": self._version, "slot": self._slot, "entry": self._entry}

    async def __call__(self, request: Request) -> dict[str, Any]:
        payload = await request.json()
        args = payload.get("args", [])
        if not isinstance(args, list):
            return {"error": "'args' must be a list of the entry function's arguments"}
        try:
            result = await self.invoke(*args)
        except Exception as exc:  # noqa: BLE001 - a served version must not take the node down
            # Surfaced as an error *response*, not a crashed replica: the canary
            # reads the error rate off live traffic, and a replica that dies on
            # a bad request would look like an infrastructure fault instead of
            # the candidate defect it is.
            return {"error": f"{type(exc).__name__}: {exc}", "version": self._version}
        return {"result": result, "version": self._version, "slot": self._slot}


async def _timed_call(handle: Any, args: tuple[Any, ...]) -> tuple[float, Any, str | None]:
    """Await one downstream call, returning (latency, response, error)."""
    start = time.perf_counter()
    try:
        response = await handle.invoke.remote(*args)
    except Exception as exc:  # noqa: BLE001 - a failed version is a measurement
        return time.perf_counter() - start, None, f"{type(exc).__name__}: {exc}"
    return time.perf_counter() - start, response, None


@serve.deployment
class CanaryRouter:
    """Weighted split / shadow dispatch in front of a blue and a green version.

    **This exists because Ray Serve has no weighted traffic split.** Serve routes
    a request to one application by path prefix; "send 5% of traffic to the
    candidate" is not a config knob, it is a component. So is shadow dispatch.
    Both live here, in front of two ``TargetDeployment`` handles.

    Modes (:class:`sis.canary.CanaryMode`, a per-target choice):

    - ``SPLIT`` — each request reaches exactly one version, chosen by weight.
      No paired baseline response exists, so ``evaluate_canary`` skips the
      response-agreement gate.
    - ``SHADOW`` (default) — every sampled request goes to *both*, and **only
      blue's answer is returned to the caller**. That containment is the point:
      the candidate can be wrong without a client ever seeing it wrong. It buys
      a paired latency comparison (same request, same instant, so the split's
      traffic-mix confounder disappears) and a direct answer-agreement check,
      at the cost of double compute.

    It is also the metrics accumulator — it is the only component that sees both
    versions answer, so it is where a *paired* observation can be recorded at
    all. Pinned to one replica for that reason (see :func:`build_canary`): a
    second replica would hold a second, partial set of observations, and
    ``live_metrics`` would silently summarise a fraction of the traffic. A
    production deployment ships observations to a metrics store instead and
    drops that constraint.
    """

    def __init__(
        self,
        blue: Any,
        *,
        blue_version: str = "blue",
        mode: CanaryMode = CanaryMode.SHADOW,
        buffer: int = DEFAULT_BUFFER,
        sample_buffer: int = DEFAULT_SAMPLE_BUFFER,
    ) -> None:
        self._blue = blue
        self._blue_version = blue_version
        # Green is *attached at runtime*, not bound at build time — see
        # attach_green() for why that distinction is load-bearing.
        self._green: Any = None
        self._green_version: str | None = None
        self._mode = CanaryMode(mode)
        self._weight = 0.0
        # (observed_at, version, latency, is_error). Wall clock, not monotonic:
        # the reader is a different process, and window filtering happens here
        # precisely so two clocks never have to be compared.
        self._obs: deque[tuple[float, str, float, bool]] = deque(maxlen=buffer)
        self._samples: deque[LiveSample] = deque(maxlen=sample_buffer)
        self._rng = random.Random()

    # --- control plane (called by ServeCloud over the handle) ---

    def attach_green(self, green_app: str, version: str, weight: float = 0.0) -> None:
        """Point at a candidate deployed as a *separate* Serve application.

        **Why green is attached rather than bound.** Binding it into this
        application would mean adding a canary requires re-running the whole
        app graph — and measured on a live instance, that *restarts the blue
        replica* (verified: blue's construction identity changes). Serve drains
        gracefully, so no request is dropped, but cycling the stable version in
        order to observe a candidate is precisely what a canary must not do: it
        discards blue's warm state at the moment blue becomes the baseline being
        measured against.

        Attaching instead makes canary deploy, ramp and rollback pure
        control-plane calls on this replica. Blue is never touched.
        """
        from ray import serve as _serve

        self._green = _serve.get_app_handle(green_app)
        self._green_version = version
        self.set_weight(weight)

    def detach_green(self) -> None:
        """Stop routing to the candidate. The app itself is deleted by the caller."""
        self._weight = 0.0
        self._green = None
        self._green_version = None

    def set_mode(self, mode: CanaryMode) -> None:
        """Switch between shadow and split dispatch without redeploying.

        Mode is per-target configuration, and a redeploy to change it would
        restart the replica and discard the window — the same cost that made
        green a separate application in the first place.
        """
        self._mode = CanaryMode(mode)

    def set_weight(self, fraction: float) -> None:
        """Route ``fraction`` of traffic to green. 0.0 = green is dark."""
        if not 0.0 <= fraction <= 1.0:
            raise ValueError(f"traffic fraction must be in 0.0..1.0, got {fraction}")
        if fraction > 0.0 and self._green is None:
            raise ValueError("cannot send traffic to green: no candidate is deployed")
        self._weight = fraction

    def status(self) -> dict[str, Any]:
        return {
            "blue_version": self._blue_version,
            "green_version": self._green_version,
            "weight": self._weight,
            "mode": self._mode.value,
            "observations": len(self._obs),
            "samples": len(self._samples),
        }

    def window(self, version: str, window_s: float) -> tuple[list[float], int]:
        """Latencies and error count for ``version`` over the last ``window_s``.

        Filtered *here*, where the observations were timestamped, so the caller
        never has to reconcile its clock with a replica's.
        """
        cutoff = time.time() - window_s
        recent = [(lat, err) for at, ver, lat, err in self._obs
                  if ver == version and at >= cutoff]
        return [lat for lat, _ in recent], sum(1 for _, err in recent if err)

    def samples(self, limit: int | None = None) -> list[LiveSample]:
        """Paired live samples for ``evaluate_canary``. Empty under SPLIT."""
        collected = list(self._samples)
        return collected[-limit:] if limit is not None else collected

    def reset(self) -> None:
        """Drop the accumulated window — used when a fresh canary starts."""
        self._obs.clear()
        self._samples.clear()

    # --- data plane ---

    def _record(self, version: str, latency: float, error: str | None) -> None:
        self._obs.append((time.time(), version, latency, error is not None))

    async def route(self, *args: Any) -> dict[str, Any]:
        """Dispatch one request and return the answer the *caller* should see."""
        shadowing = (
            self._mode is CanaryMode.SHADOW
            and self._green is not None
            and self._weight > 0.0
        )
        if shadowing:
            (b_lat, b_out, b_err), (g_lat, g_out, g_err) = await asyncio.gather(
                _timed_call(self._blue, args), _timed_call(self._green, args))
            self._record(self._blue_version, b_lat, b_err)
            self._record(str(self._green_version), g_lat, g_err)
            # Only a request both versions answered is comparable evidence. A
            # sample built from a failed call would enter evaluate_canary as a
            # "disagreement", blaming the candidate for what may be blue's fault
            # — and the error already moved error_rate, which is where a failure
            # belongs.
            if b_err is None and g_err is None:
                self._samples.append(LiveSample(
                    request=args, candidate_response=g_out, baseline_response=b_out))
            # The caller gets blue's answer even when green disagreed: shadow
            # traffic is unobservable to clients by definition.
            return self._envelope(self._blue_version, BLUE, b_out, b_err)

        to_green = self._green is not None and self._rng.random() < self._weight
        handle = self._green if to_green else self._blue
        version = str(self._green_version) if to_green else self._blue_version
        slot = GREEN if to_green else BLUE
        latency, out, err = await _timed_call(handle, args)
        self._record(version, latency, err)
        return self._envelope(version, slot, out, err)

    def _envelope(self, version: str, slot: str, out: Any, err: str | None) -> dict[str, Any]:
        if err is not None:
            return {"error": err, "version": version, "slot": slot}
        return {"result": out, "version": version, "slot": slot}

    async def invoke(self, *args: Any) -> dict[str, Any]:
        """Alias for :meth:`route`.

        ``sis.loadgen.drive_handle`` calls ``.invoke`` on whatever handle it is
        given — that is the direct path a :class:`TargetDeployment` exposes.
        Aliasing it here lets the same load generator fill a canary's live
        window (OMNI-14's bootstrap traffic, since nothing external calls the
        target yet) by driving the router exactly as a real caller would,
        rather than needing a router-aware special case in ``loadgen``.
        """
        return await self.route(*args)

    async def __call__(self, request: Request) -> dict[str, Any]:
        payload = await request.json()
        args = payload.get("args", [])
        if not isinstance(args, list):
            return {"error": "'args' must be a list of the entry function's arguments"}
        return await self.route(*args)


def app_name(contract: OptimizationContract, slot: str) -> str:
    """Serve application name for a contract's slot, e.g. ``sort-green``."""
    return f"{contract.name}-{slot}"


def route_prefix(contract: OptimizationContract, slot: str) -> str:
    """HTTP route for a slot. Blue owns the plain path (it is what clients
    call); other slots get a suffixed one so both can be up simultaneously —
    which is the entire point of a canary."""
    return f"/{contract.name}" if slot == BLUE else f"/{contract.name}-{slot}"


def build(
    contract: OptimizationContract,
    *,
    source: str | None = None,
    version: str = "live",
    slot: str = BLUE,
    num_replicas: int = 1,
) -> Any:
    """Build (don't run) a Serve application for *contract*.

    *source* defaults to the contract's committed target — the merged,
    human-reviewed code. A canary passes candidate source explicitly, which is
    the case the module docstring's sandbox note is about.
    """
    import pathlib

    code = (source if source is not None
            else pathlib.Path(contract.target_file).read_text(encoding="utf-8"))
    # `@serve.deployment` replaces the class with a Deployment at runtime, but
    # its stub still types the name as the plain class — so .options/.bind are
    # invisible to mypy. Narrow ignores rather than dropping the strict gate.
    return TargetDeployment.options(  # type: ignore[attr-defined]
        num_replicas=num_replicas, ray_actor_options=_LIGHTWEIGHT_REPLICA,
    ).bind(code, contract.entry, version, slot, sandbox_settings())


def build_canary(
    contract: OptimizationContract,
    *,
    blue_source: str,
    blue_version: str,
    mode: CanaryMode = CanaryMode.SHADOW,
) -> Any:
    """Build the public application: a router in front of blue.

    Green is deliberately *not* part of this graph — it is deployed as its own
    application and attached at runtime (:meth:`CanaryRouter.attach_green`), so
    that adding or removing a canary never re-runs this one and never restarts
    the blue replica.
    """
    blue = TargetDeployment.options(  # type: ignore[attr-defined]
        name="blue", num_replicas=1, ray_actor_options=_LIGHTWEIGHT_REPLICA,
    ).bind(blue_source, contract.entry, blue_version, BLUE, sandbox_settings())

    # Pinned to one replica: the router is the only place a paired observation
    # can be recorded, so a second replica would split the window (see the class
    # docstring).
    return CanaryRouter.options(  # type: ignore[attr-defined]
        num_replicas=1, ray_actor_options=_LIGHTWEIGHT_REPLICA,
    ).bind(blue, blue_version=blue_version, mode=mode)


def build_candidate(
    contract: OptimizationContract,
    *,
    source: str,
    version: str,
    scrub_env: bool = True,
) -> Any:
    """Build the green application — one deployment, serving candidate source.

    ``scrub_env`` blanks the replica's non-allowlisted environment variables
    (:func:`scrubbed_env_vars`), because this is where *candidate* source runs.
    Blue serves merged, human-reviewed code and is left alone. Turning it off
    exists so a test can observe the difference; a real canary should not.
    """
    ray_actor_options: dict[str, Any] = dict(_LIGHTWEIGHT_REPLICA)
    if scrub_env:
        ray_actor_options["runtime_env"] = {"env_vars": scrubbed_env_vars()}
    options: dict[str, Any] = {
        "name": "green", "num_replicas": 1, "ray_actor_options": ray_actor_options}
    return TargetDeployment.options(**options).bind(  # type: ignore[attr-defined]
        source, contract.entry, version, GREEN, sandbox_settings())


def serve_slot(
    contract: OptimizationContract,
    *,
    source: str | None = None,
    version: str = "live",
    slot: str = BLUE,
) -> Any:
    """Build *and run* one slot, returning its ``DeploymentHandle``."""
    return serve.run(
        build(contract, source=source, version=version, slot=slot),
        name=app_name(contract, slot),
        route_prefix=route_prefix(contract, slot),
    )


def _demo() -> None:
    """``python -m sis.serving`` — blue and green up at once, for poking at.

    Blue serves the committed target, green the stub's optimised candidate, so
    the two routes genuinely run different code (docs/RUNBOOK.md, Level 0b).
    """
    import logging
    import pathlib
    import time

    import ray

    from sis.contract import SORT

    ray.init(logging_level=logging.ERROR, ignore_reinit_error=True)
    serve.start(logging_config={"log_level": "ERROR"})

    serve_slot(SORT, version="blue-live", slot=BLUE)
    candidate = pathlib.Path(str(SORT.stub_candidate_path)).read_text(encoding="utf-8")
    serve_slot(SORT, source=candidate, version="green-candidate", slot=GREEN)

    blue_url = f"http://127.0.0.1:8000{route_prefix(SORT, BLUE)}"
    print(f"blue  (committed target) → {blue_url}")
    print(f"green (candidate)        → http://127.0.0.1:8000{route_prefix(SORT, GREEN)}")
    print(f"""\n  curl -s -X POST -d '{{"args": [[3,1,2]]}}' {blue_url}\n""")
    print("Ctrl-C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nshutting down")
        serve.shutdown()  # type: ignore[no-untyped-call]  # ray.serve ships no stub


if __name__ == "__main__":
    _demo()
