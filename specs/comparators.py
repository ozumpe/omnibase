"""Shared comparators for the backtest gate.

**Runs inside the gauntlet sandbox**, next to untrusted candidate code, so this
is standard-library only and imports nothing from ``sis/`` — the same rule every
``oracle.py`` follows.

POLICY-FORBIDDEN (``specs/`` is in ``sis.policy.GUARDRAIL_DIRS``). That matters
more here than it looks: a comparator decides whether a candidate reproduced
history, so a candidate able to edit one could simply declare itself correct.
This is the exam's marking scheme, not a utility module.

A comparator answers *did the candidate reproduce the recorded outcome?* with a
uniform signature::

    compare(actual, expected, tolerance) -> (ok: bool, detail: str)

``detail`` is written into the episodic log on failure, so it should say what
differed and by how much — "reproduced history badly" is not a debuggable
record. Return, never raise: a comparator that throws is a harness fault and
gets blamed on the candidate.

A contract may define its own comparator of the same name in its ``oracle.py``,
which takes precedence over anything here (see ``sis.backtest.build_script``).
That is how a domain expresses a comparison the shared set has no business
knowing about — "within 5% of realised cost, but never under-predicting demand"
— without the engine learning the domain's vocabulary.

**On determinism.** Everything here compares *values*, which is what a
deterministic contract needs and all that currently ships. A stochastic
contract's comparator would score a *distribution* against recorded outcomes (a
proper scoring rule — Brier, log score, CRPS) and belongs beside these under the
same signature. The gate does not change; only the function named by the
contract does. See docs/OMNITRACK_VISION.md E2.
"""

import math
from typing import Any


def exact(actual: Any, expected: Any, tolerance: float) -> tuple[bool, str]:
    """Equality. *tolerance* is accepted and ignored, to keep one signature.

    The right default for anything discrete — a category, a decision, an
    identifier — where "close" is not a meaningful idea.
    """
    del tolerance
    if actual == expected:
        return True, "exact match"
    return False, f"expected {expected!r}, got {actual!r}"


def within_tolerance(actual: Any, expected: Any, tolerance: float) -> tuple[bool, str]:
    """Numbers within a *relative* tolerance; structures compared elementwise.

    Relative rather than absolute because fixtures across a domain rarely share a
    scale, and one absolute epsilon would be far too loose for small recorded
    values and far too tight for large ones.

    Two behaviours worth knowing before you write a fixture:

    - **A recorded outcome of exactly zero admits only exactly zero.** Relative
      tolerance around 0 is 0 (this follows ``math.isclose`` semantics). It is
      the mathematically honest reading, and it surprises people — if a genuine
      zero should tolerate small deviations, the domain wants its own comparator
      with an absolute floor, not a fudge here that silently loosens every other
      fixture.
    - **Booleans compare exactly**, never numerically, despite ``bool`` being a
      subclass of ``int``. Otherwise ``True`` would satisfy a recorded ``1``
      within any tolerance, which is a type confusion dressed up as a pass.
    """
    return _compare(actual, expected, tolerance, path="")


def _compare(actual: Any, expected: Any, tolerance: float, *, path: str) -> tuple[bool, str]:
    """Recursive worker. *path* names the failing position inside a structure."""
    at = f" at {path}" if path else ""

    # Order matters: bool before the numeric branch (bool is an int), and str
    # before the sequence branch (a str is iterable, and comparing it character
    # by character would report "length 4 vs 5" for two different words).
    if isinstance(expected, bool) or isinstance(actual, bool):
        if actual is expected:
            return True, "exact match"
        return False, f"expected {expected!r}, got {actual!r}{at}"

    if isinstance(expected, int | float) and isinstance(actual, int | float):
        if math.isclose(actual, expected, rel_tol=tolerance):
            return True, "within tolerance"
        delta = abs(actual - expected)
        relative = delta / abs(expected) if expected else float("inf")
        return False, (
            f"expected {expected!r}, got {actual!r}{at} "
            f"(off by {delta:g}, {relative:.1%} > {tolerance:.1%})"
        )

    if isinstance(expected, str) or isinstance(actual, str):
        if actual == expected:
            return True, "exact match"
        return False, f"expected {expected!r}, got {actual!r}{at}"

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False, f"expected an object{at}, got {type(actual).__name__}"
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        if missing or extra:
            return False, (
                f"key mismatch{at}: missing {missing}, unexpected {extra}"
            )
        for key in expected:
            ok, detail = _compare(
                actual[key], expected[key], tolerance, path=f"{path}[{key!r}]"
            )
            if not ok:
                return False, detail
        return True, "within tolerance"

    if isinstance(expected, list | tuple):
        if not isinstance(actual, list | tuple):
            return False, f"expected a sequence{at}, got {type(actual).__name__}"
        if len(actual) != len(expected):
            return False, (
                f"length mismatch{at}: expected {len(expected)}, got {len(actual)}"
            )
        for index, (a, e) in enumerate(zip(actual, expected, strict=True)):
            ok, detail = _compare(a, e, tolerance, path=f"{path}[{index}]")
            if not ok:
                return False, detail
        return True, "within tolerance"

    # Anything else (None, and types JSON does not produce) falls back to
    # equality rather than guessing at a notion of closeness it may not have.
    if actual == expected:
        return True, "exact match"
    return False, f"expected {expected!r}, got {actual!r}{at}"
