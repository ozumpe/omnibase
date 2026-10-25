"""sis.scenarios — a generated fuel-market scenario, as a sensor reads it (OMNI-34).

The generator itself is ``specs/scenarios.py``, the **pinned** copy: it runs in
the gauntlet sandbox too, so it imports nothing from here, and it changes only
through a human. This module loads it by path and turns what it writes into
:class:`~sis.ports.Reading` and :class:`~sis.sensor_input.Absent` objects
behind a :class:`~sis.adapters.SimSensor`, so a twin is driven by a scenario
through the same ``read``, ``replay`` and ``as_of`` as by a recorded trace.

``runtime/scenarios.py`` is the improvable working copy (SOFT). Nothing here
reads it: the loop may improve a simulator, and is never judged by the copy
it improved (D7).

A scenario's readings carry the source :data:`~sis.sensor_input.SIMULATED_SOURCE`,
which a backtest refuses: simulation refutes, it never promotes (D4).

Guardrail code (POLICY-FORBIDDEN): it decides which generator drives a twin.
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from sis.adapters import SimSensor
from sis.clock import parse_event_time
from sis.paths import SCENARIOS_PATH
from sis.ports import Reading
from sis.sensor_input import SIMULATED_SOURCE, Absent

_MODULE = "_pinned_scenarios"


def pinned() -> ModuleType:
    """The pinned generator, ``specs/scenarios.py``, loaded once by path."""
    cached = sys.modules.get(_MODULE)
    if cached is not None and getattr(cached, "__file__", None) == str(SCENARIOS_PATH):
        return cached
    spec = importlib.util.spec_from_file_location(_MODULE, SCENARIOS_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"no scenario generator at {SCENARIOS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE] = module  # its dataclass looks its own module up
    spec.loader.exec_module(module)
    if module.SOURCE != SIMULATED_SOURCE:
        raise ImportError(f"{SCENARIOS_PATH} writes source {module.SOURCE!r}, not "
                          f"{SIMULATED_SOURCE!r}: a backtest could not tell it from real data")
    return module


@dataclass(frozen=True)
class Generated:
    """A scenario as the generator wrote it, and as a sensor holds it."""

    scenario: Any                   # the pinned module's Scenario
    readings: tuple[Reading, ...]
    absent: tuple[Absent, ...]

    def sensor(self) -> SimSensor:
        """A sensor that replays this scenario."""
        return SimSensor(self.readings, self.absent)

    def decision_times(self, series: str | None = None) -> list[datetime.datetime]:
        """When each week's first vintage of *series* (default California's price) was known."""
        gen = pinned()
        times: list[datetime.datetime] = gen.decision_times(
            self.scenario, series if series is not None else gen.CALIFORNIA)
        return times


def generate(kind: str, *, seed: int, **options: Any) -> Generated:
    """The pinned generator's scenario *kind* with *seed* (``specs/scenarios.generate``)."""
    scenario = pinned().generate(kind, seed=seed, **options)
    readings = tuple(Reading.from_record(record, where=f"scenario {kind}:{seed}")
                     for record in scenario.readings)
    absent = tuple(
        Absent(record["source"], record["series"],
               parse_event_time(record["event_time"], where=f"scenario {kind}:{seed}"),
               parse_event_time(record["known_at"], where=f"scenario {kind}:{seed}"),
               record["marker"])
        for record in scenario.absent)
    return Generated(scenario=scenario, readings=readings, absent=absent)


def sensor(kind: str, *, seed: int, **options: Any) -> SimSensor:
    """A sensor replaying the scenario :func:`generate` makes."""
    return generate(kind, seed=seed, **options).sensor()


__all__ = ["Generated", "generate", "pinned", "sensor"]
