"""sis.worker — Ray actor that runs the target and reports latency."""

import importlib.util
import time
import types

import ray

from sis.paths import TARGET_PATH


@ray.remote
class WorkerActor:
    """Runs the current target module and exposes benchmark results."""

    def __init__(self) -> None:
        self._module: types.ModuleType = self._load_target()

    # ------------------------------------------------------------------
    # Public interface (called by supervisor)
    # ------------------------------------------------------------------

    def benchmark(self, n: int = 10_000, repetitions: int = 5) -> float:
        """Return mean wall-clock time (s) for sum_of_divisors(n)."""
        fn = self._module.sum_of_divisors
        times: list[float] = []
        for _ in range(repetitions):
            start = time.perf_counter()
            fn(n)
            times.append(time.perf_counter() - start)
        return sum(times) / len(times)

    def reload(self) -> None:
        """Hot-reload the target module from disk (called after a promotion)."""
        self._module = self._load_target()

    def get_source(self) -> str:
        """Return the current target source for inclusion in prompts."""
        return TARGET_PATH.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_target() -> types.ModuleType:
        spec = importlib.util.spec_from_file_location("target", TARGET_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot locate target at {TARGET_PATH}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
