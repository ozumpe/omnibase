"""sis.self_model — the SelfModel / Registry digital twin.

A named, detached Ray actor that keeps a live, queryable model of the
system itself and its substrate — the *first piece of the world the server
models*. It tracks:

- the actor registry (role, state, parent/child, restarts),
- deployment state (blue/green slots, the live version),
- the contract registry (what "correct" and "better" mean, per target),
- the provenance graph (spec → epic/story → branch/PR → deploy → outcome),
- the runtime substrate (Ray cluster resources, platform).

It is load-bearing for error collection, blue/green handling, reasoning
about the org chart, and supervision/placement.
"""

from __future__ import annotations

import datetime
import platform
from dataclasses import asdict, dataclass, field
from typing import Any

import ray

from sis.contract import OptimizationContract

SELF_MODEL_NAME = "SelfModel"


@dataclass
class ActorInfo:
    name: str
    role: str
    parent: str | None = None
    state: str = "starting"
    restarts: int = 0


@dataclass
class ProvenanceEvent:
    timestamp: str
    kind: str  # spec | epic | story | branch | pr | canary | outcome
    ref: str
    detail: dict[str, Any] = field(default_factory=dict)


@ray.remote
class SelfModel:
    """Live model of the running system and its substrate."""

    def __init__(self) -> None:
        self._registry: dict[str, ActorInfo] = {}
        self._provenance: list[ProvenanceEvent] = []
        self._slots: dict[str, str | None] = {"blue": None, "green": None}  # slot → version
        self._live_version: str | None = None
        self._contracts: dict[str, OptimizationContract] = {}  # target_path → contract
        self._pr_contracts: dict[str, str] = {}  # pr_id → contract name
        # The PR whose merge would release the current canary. Tracked here
        # rather than parsed back out of the green version string: the version
        # is f"{branch}@{pr_id}" and a branch name may itself contain "@", so
        # recovering the id by splitting is guesswork. The merge watcher needs
        # an exact id, and this is already the actor that knows what is deployed.
        self._pending_pr: str | None = None

    # --- actor registry ---
    def register(self, name: str, role: str, parent: str | None = None) -> None:
        self._registry[name] = ActorInfo(name=name, role=role, parent=parent, state="alive")

    def set_state(self, name: str, state: str) -> None:
        if name in self._registry:
            self._registry[name].state = state

    def note_restart(self, name: str) -> None:
        if name in self._registry:
            self._registry[name].restarts += 1

    def registry(self) -> list[dict[str, Any]]:
        return [asdict(info) for info in self._registry.values()]

    # --- deployment state ---
    def set_slot(self, slot: str, version: str | None) -> None:
        self._slots[slot] = version

    def set_live_version(self, version: str) -> None:
        self._live_version = version
        self._slots["blue"] = version

    def set_pending_pr(self, pr_id: str | None) -> None:
        """Record (or clear) the PR whose merge would release the canary."""
        self._pending_pr = pr_id

    def deployment(self) -> dict[str, Any]:
        return {"slots": dict(self._slots), "live_version": self._live_version,
                "pending_pr": self._pending_pr}

    # --- contract registry ---
    # The SelfModel already knows what is deployed where; knowing what each
    # target is *judged by* belongs with it rather than in an env var, and it is
    # the same lookup the canary needs to fetch a PR's contract later
    # (docs/SERVE_CANARY.md step 10).
    def register_contract(self, contract: OptimizationContract) -> None:
        self._contracts[contract.target_path] = contract

    def contract_for(self, target_path: str) -> OptimizationContract | None:
        """The contract governing *target_path*, or None if it has none."""
        return self._contracts.get(target_path)

    def contract_by_name(self, name: str) -> OptimizationContract | None:
        """The contract called *name*, or None. How a cycle selects which
        target to optimise (``SIS_CONTRACT``) when there is more than one."""
        return next((c for c in self._contracts.values() if c.name == name), None)

    def contracts(self) -> list[OptimizationContract]:
        return list(self._contracts.values())

    def set_pr_contract(self, pr_id: str, contract_name: str) -> None:
        """Remember which contract a PR's candidate was implemented against.

        The engine is multi-target (``sum_of_divisors``, ``sort``, ...), so by
        the time a canary needs the contract — its oracle, entry point, margin,
        route — the PR id is all it has to go on. Set once by ``SWE.implement()``
        right after it resolves the contract and opens the PR; read by
        ``DevOps.canary()``. Adapter-agnostic: this lives here rather than on
        the ``PullRequest`` dataclass or a VCS-specific field, so it works
        identically for every ``VersionControl`` adapter without teaching the
        real GitHub adapter to round-trip custom metadata through a PR.
        """
        self._pr_contracts[pr_id] = contract_name

    def contract_for_pr(self, pr_id: str) -> OptimizationContract | None:
        """The contract governing *pr_id*'s candidate, or None if unknown."""
        name = self._pr_contracts.get(pr_id)
        return self.contract_by_name(name) if name else None

    # --- provenance graph ---
    def record(self, kind: str, ref: str, **detail: Any) -> None:
        self._provenance.append(
            ProvenanceEvent(timestamp=_now(), kind=kind, ref=ref, detail=detail)
        )

    def provenance(self) -> list[dict[str, Any]]:
        return [asdict(e) for e in self._provenance]

    # --- substrate (hardware / runtime) ---
    def substrate(self) -> dict[str, Any]:
        try:
            resources = ray.cluster_resources()  # type: ignore[no-untyped-call]
        except Exception:  # pragma: no cover - Ray not initialised
            resources = {}
        return {
            "ray_resources": resources,
            "python": platform.python_version(),
            "platform": platform.platform(),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "registry": self.registry(),
            "deployment": self.deployment(),
            "provenance": self.provenance(),
            "substrate": self.substrate(),
        }


def _now() -> str:
    """Wall clock, deliberately — provenance is an audit trail, not event time.

    ``sis.clock`` makes event time injectable, and this is the place it should
    *not* be used. A provenance entry records when the **engine** did something;
    an audit trail that can be repositioned is worth less than one that cannot.

    There is a second, mechanical reason a clock would not work here anyway: the
    SelfModel is a named, detached actor created with ``get_if_exists``, so a
    clock passed at construction would be fixed by whichever process created it
    first and silently ignored by every later cycle — the same shape as the
    env-var trap that sis.clock's docstring warns about.
    """
    return datetime.datetime.now(datetime.UTC).isoformat()


def get_self_model() -> Any:
    """Return the named SelfModel handle (a Ray ActorHandle), creating it if necessary.

    Shares the ``sis`` namespace and uses atomic get-or-create so a persistent
    cluster reuses the one SelfModel across runs instead of duplicating it (M2).
    """
    return SelfModel.options(  # type: ignore[attr-defined]
        name=SELF_MODEL_NAME, namespace="sis", lifetime="detached", get_if_exists=True
    ).remote()
