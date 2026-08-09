"""sis.backtest — "does the candidate reproduce recorded reality?"

The Class-1 benchmark asks *is it faster than the baseline?* and the differential
gate asks *does it agree with a reference oracle?* Both presuppose an oracle that
can be evaluated on demand. A model of something in the world has neither: there
is no reference implementation of how a junction, a supplier or a regulator
behaves. What there *is* is history — states that occurred and outcomes that
followed. The backtest gate makes that the reference (docs/CLASS2_CONTRACT.md).

Three design choices are load-bearing rather than incidental.

**Input and expected outcome are separate files.** It reads like redundancy, and
it is the reason the holdout can exist at all. A fixture's *inputs* are usually
fine to show a proposer — they describe a situation. The recorded *outcome* is
the exam answer, and a proposer that has seen it can reproduce it without
modelling anything. Keeping them in separate artifacts means "show the inputs,
never the answers" is a file-level rule rather than a discipline someone has to
remember while writing a prompt.

**Comparison is named by the contract, not hardcoded.** ``Backtest.compare``
resolves to a function *inside the sandbox*, the same trick
``OptimizationContract`` already uses for its oracle: contract data is trusted,
the candidate is not, so the comparison must not be something the candidate can
reach. This is also the single seam where non-determinism will enter
(docs/OMNITRACK_VISION.md E2) — a deterministic contract names
``within_tolerance`` and compares values; a stochastic one names a proper scoring
rule and compares distributions. Same gate, same fixtures, different comparator.
Nothing about determinism is a property of *this* module, and a contract that
says nothing stays deterministic forever.

**Fixtures carry event time from the first version.** Today nothing reads it.
The alternative is discovering later that every recorded fixture is unreplayable
— and unlike code, history cannot be re-recorded once the window has passed. See
OMNI-23 for the ``Clock`` port that will populate and consume it.

This module is deliberately pure: dataclasses, validation, and a script builder.
Sandbox execution lives in :mod:`sis.gauntlet`, which already owns that
machinery — keeping it there avoids a circular import and keeps every "how do we
run untrusted code" decision in one file.
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

# Fixture schema version. Bumped only for a breaking layout change; the gate
# refuses a version it does not know rather than guessing at the shape and
# silently comparing the wrong field.
FIXTURE_SCHEMA = 1

# The default comparison: numbers within a relative tolerance, structures
# elementwise. Named (not inlined) because the contract must be able to say
# something else without the engine changing.
DEFAULT_COMPARATOR = "within_tolerance"

# 5% — matches the worked example in docs/CLASS2_CONTRACT.md ("land within 5% of
# realized cost"). A per-backtest field because how close counts as reproducing
# history is a property of the domain, never of the engine.
DEFAULT_TOLERANCE = 0.05

# Exit codes the in-sandbox script uses to distinguish *why* it stopped. Distinct
# values because "the candidate got a different answer" and "the harness could
# not run" must never collapse into one verdict — the episodic dataset separates
# a candidate's fault from ours (``harness`` vs a real gate, see sis.episodic).
EXIT_MISMATCH = 3       # candidate did not reproduce the fixture
EXIT_NO_ENTRY = 4       # candidate does not export the contract's entry point
EXIT_NO_COMPARATOR = 5  # the named comparator does not exist (harness fault)
EXIT_BAD_FIXTURE = 6    # fixture/expect file malformed or wrong schema


class Split(str, Enum):
    """Which partition a fixture belongs to.

    ``TRAIN`` fixtures may inform a proposal; ``HOLDOUT`` fixtures may only judge
    one. The distinction is worth encoding even though nothing enforces it yet,
    because the enforcement point is a prompt — and a prompt is built from
    whatever fields happen to be at hand. A fixture that never carried a split
    cannot be excluded from one later without re-labelling every fixture by hand.

    The deeper problem this is the hook for: every accept/reject decision leaks
    roughly one bit about the holdout, so a loop run long enough overfits data it
    was never shown (the adaptive-data-analysis problem — cf. Dwork et al.'s
    reusable holdout). Rotation and an evaluation budget are the mitigations;
    both need this field to exist first. See docs/OMNITRACK_VISION.md D5.
    """

    TRAIN = "train"
    HOLDOUT = "holdout"


@dataclass(frozen=True)
class Backtest:
    """One recorded episode a candidate must reproduce.

    *fixture* and *expect* are repo-relative paths into ``specs/`` — POLICY-
    FORBIDDEN, so the implementer cannot edit the history it is judged against,
    the same guarantee that protects the reference oracle.
    """

    name: str
    fixture: str                      # repo-relative JSON: the recorded input state
    expect: str                       # repo-relative JSON: the recorded outcome
    split: Split = Split.HOLDOUT      # default to the strict side, not the lenient one
    compare: str = DEFAULT_COMPARATOR
    tolerance: float = DEFAULT_TOLERANCE

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("backtest name must not be empty")
        if self.tolerance < 0.0:
            raise ValueError(f"tolerance must be >= 0, got {self.tolerance}")
        if not self.compare:
            raise ValueError(f"backtest {self.name!r} names no comparator")


@dataclass(frozen=True)
class Fixture:
    """A parsed fixture file: the recorded input state of one episode."""

    args: list[Any]
    # ISO-8601 of when this episode occurred. Optional *today* — no adapter
    # records it yet — and deliberately part of the schema anyway, because a
    # fixture written without it can never be replayed in event time and history
    # does not come round again. OMNI-23 populates it.
    event_time: str | None = None


def parse_fixture(raw: str, *, where: str) -> Fixture:
    """Parse a fixture document, or raise ValueError naming *where* it came from.

    Strict about the schema version on purpose: an unknown version means the
    layout is not what this code expects, and comparing the wrong field is worse
    than refusing to compare at all.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{where}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{where}: expected a JSON object, got {type(data).__name__}")

    schema = data.get("schema")
    if schema != FIXTURE_SCHEMA:
        raise ValueError(
            f"{where}: unsupported fixture schema {schema!r} "
            f"(this build understands {FIXTURE_SCHEMA})"
        )

    args = data.get("args")
    if not isinstance(args, list):
        raise ValueError(f"{where}: 'args' must be a list of entry-point arguments")

    event_time = data.get("event_time")
    if event_time is not None and not isinstance(event_time, str):
        raise ValueError(f"{where}: 'event_time' must be an ISO-8601 string or absent")

    return Fixture(args=args, event_time=event_time)


def parse_expectation(raw: str, *, where: str) -> Any:
    """Parse an expectation document and return its recorded outcome value."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{where}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{where}: expected a JSON object, got {type(data).__name__}")

    schema = data.get("schema")
    if schema != FIXTURE_SCHEMA:
        raise ValueError(
            f"{where}: unsupported fixture schema {schema!r} "
            f"(this build understands {FIXTURE_SCHEMA})"
        )
    if "value" not in data:
        raise ValueError(f"{where}: missing 'value' (the recorded outcome)")
    return data["value"]


def holdout(backtests: tuple[Backtest, ...]) -> tuple[Backtest, ...]:
    """The holdout subset — the fixtures whose outcomes must never reach a prompt."""
    return tuple(b for b in backtests if b.split is Split.HOLDOUT)


def build_script(
    *,
    candidate_path: str,
    comparators_path: str,
    oracle_path: str | None,
    entry: str,
    plan: list[dict[str, Any]],
) -> str:
    """Build the in-sandbox backtest script.

    *plan* is one dict per backtest with sandbox-local ``fixture``/``expect``
    paths plus ``name``/``compare``/``tolerance``. Pure string building, so the
    interesting part — what the script checks and in what order — is testable
    without standing up a sandbox.

    Comparator resolution prefers the contract's own oracle module over the
    shared library, so a domain can supply a comparison the shared set has no
    business knowing about, without the engine learning the domain's name.
    """
    return textwrap.dedent(
        f"""\
        import sys, json, importlib.util

        def _load(path, name):
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        cand = _load({candidate_path!r}, "candidate")
        shared = _load({comparators_path!r}, "comparators")
        oracle_path = {oracle_path!r}
        oracle = _load(oracle_path, "oracle") if oracle_path else None

        entry_fn = getattr(cand, {entry!r}, None)
        if not callable(entry_fn):
            print("NOENTRY", {entry!r})
            sys.exit({EXIT_NO_ENTRY})

        for bt in {plan!r}:
            try:
                with open(bt["fixture"], encoding="utf-8") as fh:
                    fixture = json.load(fh)
                with open(bt["expect"], encoding="utf-8") as fh:
                    expected = json.load(fh)["value"]
                args = fixture["args"]
            except Exception as exc:
                print("BADFIXTURE", bt["name"], exc)
                sys.exit({EXIT_BAD_FIXTURE})

            # Contract-local comparator wins over the shared library.
            compare = getattr(oracle, bt["compare"], None) if oracle else None
            if compare is None:
                compare = getattr(shared, bt["compare"], None)
            if not callable(compare):
                print("NOCOMPARATOR", bt["name"], bt["compare"])
                sys.exit({EXIT_NO_COMPARATOR})

            actual = entry_fn(*args)
            ok, detail = compare(actual, expected, bt["tolerance"])
            if not ok:
                print("MISMATCH", json.dumps({{
                    "backtest": bt["name"],
                    "split": bt["split"],
                    "comparator": bt["compare"],
                    "detail": str(detail),
                }}))
                sys.exit({EXIT_MISMATCH})

        print("OK")
        """
    )


def plan_entry(backtest: Backtest, *, fixture_path: Path, expect_path: Path) -> dict[str, Any]:
    """One :func:`build_script` plan entry for *backtest*, with sandbox-local paths."""
    return {
        "name": backtest.name,
        "split": backtest.split.value,
        "compare": backtest.compare,
        "tolerance": backtest.tolerance,
        "fixture": str(fixture_path),
        "expect": str(expect_path),
    }
