"""sis.self_model — the SelfModel / Registry digital twin.

A named, detached Ray actor that keeps a live, queryable model of the
system itself and its substrate — the *first piece of the world the server
models*. It tracks:

- the actor registry (role, state, parent/child, restarts),
- deployment state (blue/green slots, the live version),
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

    def deployment(self) -> dict[str, Any]:
        return {"slots": dict(self._slots), "live_version": self._live_version}

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
    return datetime.datetime.now(datetime.UTC).isoformat()


def get_self_model() -> Any:
    """Return the named SelfModel handle (a Ray ActorHandle), creating it if necessary.

    Shares the ``sis`` namespace and uses atomic get-or-create so a persistent
    cluster reuses the one SelfModel across runs instead of duplicating it (M2).
    """
    return SelfModel.options(  # type: ignore[attr-defined]
        name=SELF_MODEL_NAME, namespace="sis", lifetime="detached", get_if_exists=True
    ).remote()
