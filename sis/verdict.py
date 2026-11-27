"""sis.verdict — what a rejection *is*, as fields, not as wording (OMNI-208).

Every component that refuses a candidate says three things: which gate
refused it, what kind of refusal it is, and a sentence for people. Code reads
the first two; only people and prompts read the third. Before this module the
gate and the kind were recovered from the sentence's wording, so whether a
cycle counted toward the circuit breaker depended on what a candidate's own
error text happened to say (L27, L64).

A :class:`Verdict` travels with its sentence: on ``gauntlet.Result`` as
``verdict``, and in the plain dicts the roles pass around under
:data:`KEY`. :func:`read` takes such a dict and returns its verdict. A dict
written before OMNI-208 (an old episodic row, a stored feature rejection)
has no verdict, and only then is the sentence parsed (:func:`legacy`). No
live producer relies on that path; a test pins it.

Pure: no I/O, nothing from ``sis`` imported, so the gauntlet, the episodic
store and the roles can all use it without an import cycle.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

# The key a verdict is carried under in a result dict, next to its "reason".
KEY = "gate_verdict"

Kind = Literal["rejected", "neutral", "harness", "error"]
KINDS: frozenset[str] = frozenset(get_args(Kind))

# Gates whose refusal is benign, and the cycle status each is recorded under:
# no bug filed, no breaker count, spend still recorded. "noop": nothing to
# improve (M3). "benchmark_inconclusive": the measurement could not separate
# the candidate from the margin (OMNI-41). "canary_inconclusive": the live
# canary's paired interval could not tell (OMNI-50, M20). "review_requested":
# the feature's own review asked for changes (OMNI-131), which the next cycle
# revises.
NEUTRAL_OUTCOMES: dict[str, str] = {
    "noop": "no_change",
    "benchmark_inconclusive": "inconclusive",
    "canary_inconclusive": "inconclusive",
    "review_requested": "review_requested",
}


def kind_of(gate: str) -> Kind:
    """The kind a gate's refusal always has. Pure.

    The kind follows from the gate: one table, so a producer cannot call an
    inconclusive benchmark a rejection or a sandbox fault the candidate's.
    """
    if gate == "harness":
        return "harness"
    if gate == "error":
        return "error"
    return "neutral" if gate in NEUTRAL_OUTCOMES else "rejected"


@dataclass(frozen=True)
class Verdict:
    """Why a candidate was not accepted: the gate, the kind, and a sentence.

    ``gate`` uses the episodic log's ``reject_gate`` names (``ast``,
    ``timeout``, ``canary_regression``, ...): wider than ``GateName``, since
    the proposer, the review and the live canary refuse candidates too.
    ``detail`` is for people and prompts. Nothing reads it to decide.
    """

    gate: str
    kind: Kind
    detail: str

    def __post_init__(self) -> None:
        if self.kind != kind_of(self.gate):
            raise ValueError(f"gate {self.gate!r} is {kind_of(self.gate)}, not {self.kind}")

    @classmethod
    def of(cls, gate: str, detail: str) -> Verdict:
        """The verdict for *gate*, with the kind that gate always has."""
        return cls(gate=gate, kind=kind_of(gate), detail=detail)

    @property
    def neutral(self) -> str | None:
        """The neutral cycle status (``no_change``, ``inconclusive``, ...), or None."""
        return NEUTRAL_OUTCOMES.get(self.gate) if self.kind == "neutral" else None

    def to_dict(self) -> dict[str, str]:
        return {"gate": self.gate, "kind": self.kind, "detail": self.detail}


def from_dict(raw: Any) -> Verdict | None:
    """A verdict from its dict form, or None for anything that is not one. Pure."""
    if not isinstance(raw, Mapping):
        return None
    gate, kind, detail = raw.get("gate"), raw.get("kind"), raw.get("detail")
    if not (isinstance(gate, str) and gate and isinstance(detail, str) and kind in KINDS):
        return None
    try:
        return Verdict(gate=gate, kind=kind, detail=detail)
    except ValueError:
        return None


def carried(reason: str, gate: str) -> dict[str, Any]:
    """A result dict's ``reason`` and its verdict, for ``{**carried(...), ...}``. Pure."""
    return {"reason": reason, KEY: Verdict.of(gate, reason).to_dict()}


def read(result: Mapping[str, Any] | None) -> Verdict | None:
    """The verdict a result dict carries, or None if it refused nothing. Pure.

    A dict without one is from before OMNI-208 (an old episodic row, a stored
    rejection): only then is its reason parsed (:func:`legacy`).
    """
    if not result:
        return None
    typed = from_dict(result.get(KEY))
    if typed is not None:
        return typed
    reason = result.get("reason")
    return legacy(reason) if isinstance(reason, str) else None


def of_result(result: Any) -> Verdict | None:
    """The verdict on a ``gauntlet.Result``-shaped object, or None if it passed. Pure.

    ``validate`` types every failure. A result built without a verdict (a
    test double, say) is read as a record would be, and a reason no record
    rule knows is a plain rejection: never neutral, never the harness's.
    """
    if result.passed:
        return None
    typed = getattr(result, "verdict", None)
    if isinstance(typed, Verdict):
        return typed
    reason = str(result.reason)
    return legacy(reason) or Verdict.of("unknown", reason)


def failure_of(result: Any) -> Verdict:
    """The verdict on a failed ``gauntlet.Result``-shaped object. Pure."""
    judged = of_result(result)
    if judged is None:
        raise ValueError("a passed result has no verdict")
    return judged


def legacy(reason: str | None) -> Verdict | None:
    """The verdict an old record's reason sentence implies, or None. Pure."""
    gate = legacy_gate(reason)
    return None if gate is None or reason is None else Verdict.of(gate, reason)


# What the gauntlet said when a gate ran out of time: "<gate> gate timed out"
# or "gauntlet sandbox timed out after Ns". Only at the start: further in, the
# words are what the candidate raised or returned.
_HARNESS_TIMEOUT = re.compile(r"(?:[\w-]+ gate|gauntlet sandbox) timed out\b")


def legacy_gate(reason: str | None) -> str | None:
    """Which gate an old record's reason sentence names. Pure; records only.

    The pre-OMNI-208 parser, kept so episodic rows and stored rejections
    written before typed verdicts stay readable. Live code never calls it:
    every producer attaches a :class:`Verdict`.
    """
    if not reason:
        return None
    r = reason.lower()
    prefixes = (
        ("harness:", "harness"),
        ("proposer:", "proposer"),
        ("review requested:", "review_requested"),
        ("review:", "review"),
        ("already built", "built"),
        ("tampered:", "tampering"),
        ("interface:", "interface"),
        ("backtest failed", "backtest"),
        ("slo exceeded", "slo"),
        ("slo workload raised", "slo_error"),
        ("invariant violated in sandbox", "invariant"),
        ("insufficient evidence", "canary_evidence"),
        ("invariant violated", "canary_invariant"),
        ("response disagreement", "canary_disagreement"),
        ("live error rate", "canary_errors"),
        ("live latency inconclusive", "canary_inconclusive"),
        ("live ", "canary_regression"),
    )
    # An exception raised inside the cycle (OMNI-55): first, since its
    # message can hold any word the checks below look for.
    if r.startswith("error:"):
        return "error"
    # The harness's own timeout names the gate ("mypy gate timed out"), so it
    # wins over the gate-name checks (L12), and only at the start (L27).
    if _HARNESS_TIMEOUT.match(r) is not None:
        return "timeout"
    for prefix, gate in prefixes:
        if r.startswith(prefix):
            return gate
    contains = (
        ("syntaxerror", "ast"),
        ("no change", "noop"),
        ("mypy", "mypy"),
    )
    for needle, gate in contains:
        if needle in r:
            return gate
    if r.startswith("acceptance"):
        return "acceptance"
    if "pytest" in r:
        return "pytest"
    if "correctness mismatch" in r:
        return "correctness"
    for prefix, gate in (("benchmark inconclusive", "benchmark_inconclusive"),
                         ("benchmark unmeasurable", "benchmark_unmeasurable"),
                         ("benchmark output malformed", "benchmark_malformed")):
        if r.startswith(prefix):
            return gate
    if "no improvement" in r:
        return "benchmark"
    if "policy" in r:
        return "policy"
    return None


def is_harness_timeout(reason: str) -> bool:
    """True if an old record's reason is the harness's own timeout. Pure; records only."""
    return _HARNESS_TIMEOUT.match(reason.lower()) is not None
