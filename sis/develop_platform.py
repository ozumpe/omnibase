"""sis.develop_platform — ``develop`` as a running platform: blue/green per merge (OMNI-133).

Staged delivery (``docs/STAGED_DELIVERY.md``, epic OMNI-128) lets a human merge
one finished feature into ``develop``. This module puts each merge into service:

- **blue** is the deployed head of ``develop``, served from a sandbox worker
  (:class:`sis.sandbox_worker.HotSlot`, OMNI-129);
- the merged version is started as **green** in a second worker, beside blue;
- the host sends both the same shadow traffic, inputs from the contract's
  oracle, and judges green with :func:`sis.canary.evaluate_canary`
  (``SHADOW``): agreement with the reference and with blue first, then latency;
- green **passes**: blue and green switch. The switch is automatic, because a
  human already approved the merge;
- green **fails**: it is retired and blue keeps serving. The caller pages a
  human and opens a revert PR (``org.apply_develop_judgment``); nothing is
  reverted automatically.

**Blue never serves a version that failed its judgment.** A green that fails
to load, raises, times out, answers wrongly or is slower is closed before it
has answered a single call outside the judgment, and blue's worker is never
touched. A fault on *blue's* side (it errs or dies under the window) is the
harness's, not green's: nothing switches, nothing is reverted, and the caller
pages a human (OMNI-37's rule, a broken sandbox is never blamed on the
candidate).

Decision logic here is pure where it can be (:func:`judge_window`); the
workers are the only I/O. Class-1 contracts only: a Class-2 feature has no
oracle to draw traffic from (``docs/SERVE_CANARY.md``), so its merges are
recorded as not judged.

POLICY-FORBIDDEN: what blue serves is guardrail code, like the worker itself.
"""

from __future__ import annotations

import copy
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sis.canary import CanaryMode, CanaryVerdict, LiveSample, evaluate_canary
from sis.contract import OptimizationContract
from sis.sandbox_worker import (
    DEFAULT_CALL_TIMEOUT_S,
    CallResult,
    HotSlot,
    Reply,
    WorkerError,
    as_wire,
)

# Exchanges in one judgment, each a batch of fresh inputs sent to both sides.
# evaluate_canary needs at least DEFAULT_MIN_CANARY_SAMPLES (100) latencies on
# each side, so this leaves a margin over that floor.
DEFAULT_EXCHANGES = 120
# Calls per exchange. One call per exchange would time the pipe, not the
# target: a round trip is ~20 µs in the subprocess sandbox and ~350 µs in
# docker on a Mac, against a few µs for `sort` (STAGED_DELIVERY.md, phase 0).
DEFAULT_BATCH = 16
# Green may be this much slower than blue at p95 before it fails: a merged
# version already beat its baseline offline by the contract's margin, so here
# latency only has to catch a real regression, not prove a gain. Without
# slack, two equal versions fail about half the time on noise.
DEFAULT_LATENCY_SLACK = 0.25

REFERENCE_LAW = "agrees_with_reference"


class Slot(Protocol):
    """What the platform needs from a served version: :class:`HotSlot`'s shape."""

    @property
    def version(self) -> str | None: ...

    def deploy(self, version: str, source: str) -> None: ...

    def call(self, calls: Sequence[Sequence[Any]], *, timeout_s: float | None = None
             ) -> Reply: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class Judgment:
    """What happened to one merge.

    ``switched``: green now serves as blue. ``harness``: the judgment could not
    be made (blue's side failed), so nothing changed and green is not to blame.
    Neither: green failed and was retired; blue still serves ``previous``.
    """

    version: str
    previous: str
    switched: bool
    harness: bool
    reason: str
    verdict: CanaryVerdict | None = None

    @property
    def outcome(self) -> str:
        """The episodic outcome this judgment is recorded under."""
        if self.switched:
            return "develop_switched"
        return "develop_unjudged" if self.harness else "develop_rolled_back"


@dataclass(frozen=True)
class _ReferenceLaw:
    """Green's answer equals the trusted reference's, as plain values (H4)."""

    reference: Callable[..., Any]
    name: str = REFERENCE_LAW

    def check(self, request: Any, response: Any) -> bool:
        expected = as_wire(self.reference(*copy.deepcopy(list(request))))
        return bool(expected == response)


def _failure(result: CallResult) -> str:
    kind = result.error_types[0] if result.error_types else "error"
    return f"{kind}: {' '.join(str(result.error or '').split())[:160]}"


def judge_window(
    replies: Sequence[tuple[Sequence[Sequence[Any]], Reply, Reply]],
    *,
    version: str,
    reference: Callable[..., Any],
    latency_slack: float = DEFAULT_LATENCY_SLACK,
) -> tuple[CanaryVerdict | None, str | None, bool]:
    """Judge a window of ``(batch, blue_reply, green_reply)`` exchanges. Pure.

    Returns ``(verdict, reason, harness)``. A failed call on green is green's
    failure (``live error rate``); a failed call on blue is a harness fault,
    and no verdict is reached. Otherwise :func:`evaluate_canary` decides, in
    ``SHADOW`` mode, with the reference as its law and each exchange's host
    time as one latency observation per side.
    """
    samples: list[LiveSample] = []
    blue_latencies: list[float] = []
    green_latencies: list[float] = []
    green_errors = 0
    first_green_error: str | None = None
    calls = 0
    for batch, blue, green in replies:
        if len(blue.results) != len(batch) or len(green.results) != len(batch):
            return None, "harness: a reply did not answer every call in its batch", True
        for args, b, g in zip(batch, blue.results, green.results, strict=True):
            calls += 1
            if not b.ok:
                return None, (f"harness: blue failed under the judgment window "
                              f"({_failure(b)}); green is not to blame"), True
            if not g.ok:
                green_errors += 1
                first_green_error = first_green_error or _failure(g)
                continue
            samples.append(LiveSample(request=list(args), candidate_response=g.value,
                                      baseline_response=b.value))
        blue_latencies.append(blue.elapsed_s)
        green_latencies.append(green.elapsed_s)
    if green_errors:
        return None, (f"live error rate: green failed {green_errors} of {calls} calls "
                      f"(first: {first_green_error})"), False
    verdict = evaluate_canary(
        [_ReferenceLaw(reference)], samples, blue_latencies, green_latencies,
        version=version, mode=CanaryMode.SHADOW,
        max_latency_ratio=1.0 + latency_slack,
    )
    return verdict, verdict.reason, False


class DevelopPlatform:
    """Blue (the head of ``develop``) and the green of each merge, for one contract.

    Build it from the source ``develop`` serves before any merge is judged,
    then call :meth:`start`. :meth:`judge` deploys a merge as green, judges
    it, and switches or retires it. Not thread-safe: one merge at a time,
    which is how the loop observes them.
    """

    def __init__(
        self,
        spec: OptimizationContract,
        *,
        blue_source: str,
        blue_version: str = "develop",
        exchanges: int = DEFAULT_EXCHANGES,
        batch: int = DEFAULT_BATCH,
        latency_slack: float = DEFAULT_LATENCY_SLACK,
        seed: int | None = None,
        call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
        slot_factory: Callable[[str], Slot] | None = None,
    ) -> None:
        if exchanges < 1 or batch < 1:
            raise ValueError("exchanges and batch must be at least 1")
        if latency_slack < 0:
            raise ValueError(f"latency_slack must be >= 0, got {latency_slack}")
        self.spec = spec
        self._oracle = spec.load_oracle()
        self._exchanges = exchanges
        self._batch = batch
        self._slack = latency_slack
        self._rng = random.Random(seed)
        self._new_slot: Callable[[str], Slot] = slot_factory or (
            lambda entry: HotSlot(entry, call_timeout_s=call_timeout_s))
        self._blue: Slot = self._new_slot(spec.entry)
        self._blue_version = blue_version
        self._blue_source = blue_source

    @property
    def blue_version(self) -> str:
        return self._blue_version

    @property
    def blue_source(self) -> str:
        """What ``develop`` serves now: the content a revert PR puts back."""
        return self._blue_source

    def start(self) -> None:
        """Serve blue. Raises if it does not load: there is nothing to judge against."""
        self._blue.deploy(self._blue_version, self._blue_source)

    def close(self) -> None:
        self._blue.close()

    def _inputs(self) -> list[list[Any]]:
        return [list(self._oracle.random_input(self._rng)) for _ in range(self._batch)]

    def judge(self, version: str, source: str) -> Judgment:
        """Deploy *source* as green, judge it against blue, then switch or retire it."""
        previous = self._blue_version
        if source == self._blue_source:
            return Judgment(version, previous, switched=False, harness=True,
                            reason="already serving: the merge is what blue serves")
        green = self._new_slot(self.spec.entry)
        try:
            try:
                green.deploy(version, source)
            except WorkerError as exc:
                return self._retired(green, version, previous,
                                     f"green did not start: {exc}")
            replies: list[tuple[Sequence[Sequence[Any]], Reply, Reply]] = []
            for i in range(self._exchanges):
                batch = self._inputs()
                # Alternate who goes first, so neither side always runs warm.
                try:
                    if i % 2:
                        g = green.call(batch)
                        b = self._call_blue(batch)
                    else:
                        b = self._call_blue(batch)
                        g = green.call(batch)
                except _BlueFailed as exc:
                    return self._retired(green, version, previous, str(exc), harness=True)
                except WorkerError as exc:
                    return self._retired(green, version, previous,
                                         f"live error rate: green stopped serving: {exc}")
                replies.append((batch, b, g))
            verdict, reason, harness = judge_window(
                replies, version=version, reference=self._oracle.reference,
                latency_slack=self._slack)
            if verdict is None or not verdict.passed:
                return self._retired(green, version, previous, str(reason),
                                     harness=harness, verdict=verdict)
        except BaseException:
            green.close()
            raise
        old = self._blue
        self._blue, self._blue_version, self._blue_source = green, version, source
        old.close()
        return Judgment(version, previous, switched=True, harness=False,
                        reason=verdict.reason, verdict=verdict)

    def _call_blue(self, batch: Sequence[Sequence[Any]]) -> Reply:
        try:
            return self._blue.call(batch)
        except WorkerError as exc:
            raise _BlueFailed(
                f"harness: blue stopped serving during the judgment ({exc}); "
                "green is not to blame") from exc

    @staticmethod
    def _retired(green: Slot, version: str, previous: str, reason: str, *,
                 harness: bool = False, verdict: CanaryVerdict | None = None) -> Judgment:
        green.close()
        return Judgment(version, previous, switched=False, harness=harness,
                        reason=reason, verdict=verdict)


class _BlueFailed(Exception):
    """Blue's worker failed mid-window: a harness fault, never green's."""

