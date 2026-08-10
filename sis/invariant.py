"""sis.invariant — domain laws over generated inputs, the Class-2 anti-gaming layer.

The differential gate asks *does the candidate agree with a reference?* and a
Class-2 feature has no reference to agree with. What it has instead are **laws**:
cargo is conserved, capacity is never exceeded, converting a number to a numeral
and back returns the number. Those hold for inputs nobody enumerated, which is
exactly the property a hand-written acceptance list cannot have — and exactly
what makes them expensive to game. A candidate can special-case eight spec
examples; it cannot special-case an input distribution it never sees.

## Why Hypothesis rather than seeded `random`

The differential gate already generates random inputs with plain
``random.Random()``, so this could have too. The reason not to is **shrinking**:
when an invariant fails, Hypothesis reports a *minimal* counterexample rather
than whichever 1200-element list happened to break it. That matters more here
than it would elsewhere, because the episodic log is a training set — "this
candidate broke round-tripping at n=4" is a usable signal, and "this candidate
broke round-tripping on [some 1200 integers]" is noise with a stack trace.
``sis/loadgen.py`` anticipated this trade and deferred it to exactly this gate.

## Seeding is not optional

Every run passes an explicit seed and reports it on failure. Hypothesis's
example database is disabled in the sandbox (``database=None``), so the seed is
the *only* thing needed to reproduce a rejection — which is what makes a
recorded failure worth recording. It is also the groundwork for
``Determinism.STOCHASTIC`` (OMNI-17): a distributional gate over unseeded
generation is measuring noise it cannot distinguish from a regression.

## Two predicate shapes, and why the second cannot run on live traffic

A predicate is resolved by name inside the sandbox and called as::

    check(args, output)        -> bool     # canary-compatible
    check(args, output, impl)  -> bool     # offline only

The two-argument form matches :class:`sis.canary.BoundInvariant` exactly, so the
same predicate a contract declares here is the one the Ray Serve canary applies
to live traffic (OMNI-8 already consumes that shape; this module owns the
resolution step from string-valued contract data).

The three-argument form receives an :class:`Impl` — the candidate's module *and*
its bound entry function. Both, because the two kinds of law that need one need
different halves: a shared, domain-agnostic ``deterministic`` wants to call the
entry again and cannot know its name, while a contract-local ``round_trip``
wants a *different* export (``from_roman(to_roman(n)) == n``) and does know its
name. Handing over only the module would make every generic predicate
impossible; only the entry would make round-trip impossible.

Either way the three-argument form is unavailable online by construction:
re-invoking the candidate on a production response would change what production
does. So a round-trip invariant is a genuinely offline-only law rather than an
oversight, and :func:`is_canary_compatible` says which is which instead of
leaving the canary to fail one sample into a live rollout.
"""

from __future__ import annotations

import inspect
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

# How many generated examples per invariant. Mirrors ``diff_trials``' role on
# OptimizationContract — statistical power is a property of the target, so it is
# a contract field rather than an engine constant. Higher than Hypothesis's
# default 100 because these targets are microsecond-scale and the gate is the
# anti-gaming layer, where more unpredictable inputs is strictly safer.
DEFAULT_INVARIANT_EXAMPLES = 200

# Fixed default so a run is reproducible without the caller thinking about it;
# the loop passes a fresh one per cycle so a candidate cannot tune to the seed.
DEFAULT_SEED = 0

EXIT_VIOLATED = 3        # an invariant does not hold
EXIT_NO_ENTRY = 4        # candidate does not export the entry point
EXIT_UNRESOLVED = 5      # a named strategy or predicate does not exist
EXIT_STRATEGY_ERROR = 6  # the strategy itself blew up — harness fault, not the candidate


@dataclass(frozen=True)
class Invariant:
    """One domain law, as *data*.

    ``strategy`` and ``check`` are names, not callables, because the offline gate
    resolves them inside the sandbox alongside untrusted candidate code — the
    same reason ``Backtest.compare`` is a name. Contract modules are trusted; the
    candidate is not, and a predicate the candidate could reach is not a check.

    Resolution order for both names: the contract's own oracle module first, then
    the shared library in ``specs/invariants.py``. A domain can therefore state a
    law the shared set has no business knowing about, without the engine learning
    the domain's vocabulary.
    """

    name: str
    strategy: str   # factory returning a Hypothesis strategy of *args tuples*
    check: str      # predicate: (args, output[, module]) -> bool

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("invariant name must not be empty")
        if not self.strategy:
            raise ValueError(f"invariant {self.name!r} names no strategy")
        if not self.check:
            raise ValueError(f"invariant {self.name!r} names no predicate")


class BoundInvariant(Protocol):
    """The resolved form :mod:`sis.canary` consumes. See that module's docstring."""

    @property
    def name(self) -> str: ...

    def check(self, request: Any, response: Any) -> bool: ...


@dataclass(frozen=True)
class _Bound:
    name: str
    _predicate: Callable[..., bool]

    def check(self, request: Any, response: Any) -> bool:
        return bool(self._predicate(request, response))


@dataclass(frozen=True)
class Impl:
    """What a three-argument predicate is handed: the candidate, two ways.

    Constructed inside the sandbox. Mirrored here so the shared predicate library
    has a documented shape to be written against, and so tests can exercise a
    predicate without a sandbox.
    """

    module: Any                    # the candidate module, for laws naming a sibling export
    entry: Callable[..., Any]      # the bound entry point, for laws that just re-invoke


def needs_impl(predicate: Callable[..., bool]) -> bool:
    """Does *predicate* want an :class:`Impl` as a third argument?

    Signature inspection rather than a flag on :class:`Invariant`, so the fact
    lives with the predicate that determines it. A contract author who writes a
    round-trip law should not also have to remember to declare that it is one.
    """
    try:
        params = inspect.signature(predicate).parameters
    except (TypeError, ValueError):  # builtins and C callables have no signature
        return False
    return len(params) >= 3


def is_canary_compatible(predicate: Callable[..., bool]) -> bool:
    """Can this law also be applied to live traffic?

    Only the two-argument form. A predicate needing an :class:`Impl` would have
    to re-invoke the candidate on a production response, which changes what
    production does — so it is offline-only by construction.
    """
    return not needs_impl(predicate)


def bind(invariant: Invariant, module: Any) -> BoundInvariant:
    """Resolve *invariant*'s predicate against a trusted *module*, for canary use.

    Main-process resolution, deliberately separate from the in-sandbox path: the
    canary judges responses that have already crossed the network, using
    predicates from the POLICY-FORBIDDEN contract module. Raises if the predicate
    is missing or is offline-only, rather than letting the canary discover either
    one sample into a live rollout.
    """
    predicate = getattr(module, invariant.check, None)
    if not callable(predicate):
        raise ValueError(
            f"invariant {invariant.name!r}: no predicate {invariant.check!r} in "
            f"{getattr(module, '__name__', module)!r}"
        )
    if not is_canary_compatible(predicate):
        raise ValueError(
            f"invariant {invariant.name!r} needs to re-invoke the candidate, so it "
            "cannot run against live traffic — doing so would change what production "
            "does. Offline gate only."
        )
    return _Bound(name=invariant.name, _predicate=predicate)


def build_script(
    *,
    candidate_path: str,
    shared_path: str,
    oracle_path: str | None,
    entry: str,
    plan: list[dict[str, Any]],
    examples: int,
    seed: int,
) -> str:
    """Build the in-sandbox invariant script.

    Pure string building, so what the script checks — and in what order — is
    testable without standing up a sandbox.
    """
    return textwrap.dedent(
        f"""\
        import sys, json, inspect, importlib.util
        from dataclasses import dataclass

        from hypothesis import given, seed as _seed, settings, HealthCheck

        @dataclass(frozen=True)
        class Impl:
            module: object
            entry: object

        def _load(path, name):
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        cand = _load({candidate_path!r}, "candidate")
        shared = _load({shared_path!r}, "invariants")
        oracle_path = {oracle_path!r}
        oracle = _load(oracle_path, "oracle") if oracle_path else None

        entry_fn = getattr(cand, {entry!r}, None)
        if not callable(entry_fn):
            print("NOENTRY", {entry!r})
            sys.exit({EXIT_NO_ENTRY})

        def _resolve(name):
            # Contract-local wins over the shared library.
            found = getattr(oracle, name, None) if oracle else None
            return found if found is not None else getattr(shared, name, None)

        for inv in {plan!r}:
            factory = _resolve(inv["strategy"])
            predicate = _resolve(inv["check"])
            if not callable(factory) or not callable(predicate):
                missing = inv["strategy"] if not callable(factory) else inv["check"]
                print("UNRESOLVED", inv["name"], missing)
                sys.exit({EXIT_UNRESOLVED})

            try:
                strategy = factory()
            except Exception as exc:
                print("STRATEGY", inv["name"], exc)
                sys.exit({EXIT_STRATEGY_ERROR})

            wants_impl = len(inspect.signature(predicate).parameters) >= 3
            impl = Impl(module=cand, entry=entry_fn)

            @_seed({seed!r})
            @settings(
                max_examples={examples!r},
                database=None,          # the seed is the only reproduction handle
                deadline=None,          # a slow candidate is the benchmark's problem
                suppress_health_check=list(HealthCheck),
            )
            @given(strategy)
            # Closure capture rather than default arguments: Hypothesis refuses
            # to apply @given to a function with defaults, and the usual
            # late-binding hazard does not apply because prop() is defined and
            # called within one iteration.
            def prop(args):
                output = entry_fn(*args)
                held = predicate(args, output, impl) if wants_impl else predicate(args, output)
                assert held, f"{{inv['name']}} does not hold for args={{args!r}}"

            try:
                prop()
            except AssertionError as exc:
                # Hypothesis has already shrunk to a minimal counterexample by
                # the time it re-raises, and the assertion message carries it.
                #
                # Only that first line is reported. Hypothesis also attaches an
                # "Explanation:" note derived from coverage, which varies between
                # runs of the *same* seed — including it would make the recorded
                # reason unstable and quietly break the reproducibility the seed
                # exists to provide.
                message = str(exc).strip().splitlines()
                print("VIOLATED", json.dumps({{
                    "invariant": inv["name"],
                    "counterexample": message[0] if message else "",
                }}))
                sys.exit({EXIT_VIOLATED})

        print("OK")
        """
    )


def plan_entry(invariant: Invariant) -> dict[str, Any]:
    """One :func:`build_script` plan entry for *invariant*."""
    return {
        "name": invariant.name,
        "strategy": invariant.strategy,
        "check": invariant.check,
    }
