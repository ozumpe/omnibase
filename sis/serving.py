"""sis.serving — a contract's target behind Ray Serve.

The first thing in this codebase to actually *serve traffic*. Until now the loop
optimised a function nobody called; a canary needs somewhere for real requests
to go, and two versions to send them to. This module provides the deployment;
splitting traffic between a blue and a green one is ``ServeCloud`` (OMNI-13).

**Stateless by construction.** A replica holds the entry function and nothing
else — no cache, no counters, no cross-request state. That is a deliberate
constraint, not an oversight: it lets the canary mechanics be proven without
also solving state hand-off between blue and green. Stateful targets (an LRU
cache, a rate limiter) are explicitly deferred until the mechanics work.

## Where the sandbox boundary is — read before extending this

A replica **executes the source it is handed** (``_load``). When that source is
a merged, human-reviewed target, it is ordinary trusted code. When it is a
*candidate*, this is LLM-generated code running in a Ray worker — and a Serve
replica is **not** the gauntlet sandbox: no scrubbed environment, no egress
block, no per-call timeout.

That is the intended shape of a canary (DESIGN.md §4: deploy the new version
alongside the old and shift traffic to it), and the candidate has already passed
every offline gate. But the difference from ``sis.gauntlet`` is real and worth
stating plainly, because the guarantee is *procedural* here rather than
kernel-enforced: the gauntlet's sandbox stops a malicious diff from reaching the
network, whereas a green replica could. Anything that deploys candidate source
through here — i.e. OMNI-13 — should decide deliberately whether the replica
needs a scrubbed ``runtime_env``, and not inherit this note by accident.
"""

from __future__ import annotations

import types
from typing import TYPE_CHECKING, Any

from ray import serve

from sis.contract import OptimizationContract

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.requests import Request

# Slot names match SelfModel._slots and DeployRecord.slot, so a served
# deployment, a deploy record and the digital twin all say "green" about the
# same thing. Divergent vocabularies here would make the provenance graph and
# the live topology impossible to line up during an incident.
BLUE = "blue"
GREEN = "green"


def _load(source: str, entry: str, label: str) -> Any:
    """Execute *source* and return its *entry* callable.

    See the module docstring on what executing candidate source means here.
    ``label`` only names the synthetic module so tracebacks from a served
    version are attributable to it rather than to ``<string>``.
    """
    module = types.ModuleType(f"served_{label}")
    exec(compile(source, f"<served:{label}>", "exec"), module.__dict__)  # noqa: S102
    fn = getattr(module, entry, None)
    if not callable(fn):
        raise ValueError(f"served source does not export a callable {entry!r}")
    return fn


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

    def __init__(self, source: str, entry: str, version: str, slot: str) -> None:
        self._fn = _load(source, entry, f"{slot}_{version}")
        self._entry = entry
        self._version = version
        self._slot = slot

    def invoke(self, *args: Any) -> Any:
        """Call the served entry function. No state is kept between calls."""
        return self._fn(*args)

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
            result = self.invoke(*args)
        except Exception as exc:  # noqa: BLE001 - a served version must not take the node down
            # Surfaced as an error *response*, not a crashed replica: the canary
            # reads the error rate off live traffic, and a replica that dies on
            # a bad request would look like an infrastructure fault instead of
            # the candidate defect it is.
            return {"error": f"{type(exc).__name__}: {exc}", "version": self._version}
        return {"result": result, "version": self._version, "slot": self._slot}


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
        num_replicas=num_replicas).bind(code, contract.entry, version, slot)


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
