"""sis.contract — what "correct" and "better" mean, per target.

The gauntlet used to answer both questions with `sum_of_divisors` baked into its
benchmark script: the reference implementation, the random-input domain, and the
fixed workload were all literals inside an f-string. That made
``SIS_TARGET_PATHS`` illusory — any other target failed the benchmark gate — and
it is [L5](../docs/KNOWN_ISSUES.md).

A contract moves those decisions out of the engine and next to the target, split
in two on purpose:

- **Declarative data** (this module): which function is under optimisation,
  where the target lives, how much faster a candidate has to be.
- **A trusted oracle module** (``specs/<name>/oracle.py``): the reference
  implementation, the benchmark inputs, and the random-input generator. These
  have to be *code*, because they run inside the sandbox alongside the untrusted
  candidate — so they are a module the gauntlet copies in, not strings the
  engine interpolates.

Everything under ``specs/`` is POLICY-FORBIDDEN (``sis.policy.GUARDRAIL_DIRS``),
so the implementer cannot edit its own exam. That separation is the anti-gaming
property, made structural rather than incidental.
"""

from __future__ import annotations

import importlib.util
import types
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from sis.backtest import Backtest
from sis.invariant import DEFAULT_INVARIANT_EXAMPLES, Invariant
from sis.paths import PROJECT_ROOT


class Determinism(str, Enum):
    """Whether a contract's entry point returns a value or a distribution.

    **An axis, not a task class.** The tempting move is to call a stochastic
    target "Class 3", after optimisation (Class 1) and feature construction
    (Class 2). That is a category error: Classes 1 and 2 differ by *what the
    task is*, while determinism differs by *what the output is*, and it crosses
    both. A slow Monte Carlo simulation someone wants sped up is unambiguously a
    Class-1 optimisation and unambiguously stochastic — a linear 1→2→3 ladder
    has nowhere to put it. See docs/OMNITRACK_VISION.md E2.

    Consequences, and the reason this is one field rather than a hierarchy:

    - **The default is DETERMINISTIC and a contract that says nothing stays
      that way, permanently.** There is no ladder to climb and nothing to opt
      out of.
    - **The gate profile does not branch on this.** Only the *comparator*
      inside a gate does (``Backtest.compare``): a deterministic contract names
      ``within_tolerance`` and compares values, a stochastic one names a proper
      scoring rule and compares distributions. Same gates, same fixtures.
    - The one structural difference is the seed requirement below, enforced by
      the interface gate.
    """

    DETERMINISTIC = "deterministic"
    STOCHASTIC = "stochastic"


class GateName(str, Enum):
    """The gates a contract can ask for, in the order they are cheapest to run.

    The *contract* selects the profile; :mod:`sis.gauntlet` owns every
    implementation. That split is deliberate: gate code is guardrail code and
    stays in one FORBIDDEN file, while which gates apply is a property of the
    target and belongs next to it.
    """

    AST = "ast"
    NOOP = "noop"
    MYPY = "mypy"
    INTERFACE = "interface"
    ACCEPTANCE = "acceptance"
    INVARIANT = "invariant"
    BACKTEST = "backtest"
    DIFFERENTIAL_BENCHMARK = "differential_benchmark"


class Contract(Protocol):
    """What the gauntlet needs from a contract, regardless of its class.

    Not ``runtime_checkable``: it carries data members, and an ``isinstance``
    check against those is both unsupported and beside the point — this exists
    so ``validate()`` can be written once against a shape, not so anything can
    interrogate a contract at runtime.
    """

    # Read-only properties, not bare attributes: a Protocol member declared as
    # a plain attribute is a *settable* variable, which a frozen dataclass can
    # never satisfy. Every contract is frozen, so every member here is a
    # property — mypy is right to insist, and the alternative (unfreezing the
    # contracts) would make the exam mutable at runtime.
    @property
    def name(self) -> str:
        """Identifier used in reject reasons and the episodic log."""
        ...

    @property
    def determinism(self) -> Determinism:
        """Whether the entry point returns a value or a distribution."""
        ...

    @property
    def backtests(self) -> tuple[Backtest, ...]:
        """Recorded episodes the candidate must reproduce."""
        ...

    @property
    def invariants(self) -> tuple[Invariant, ...]:
        """Domain laws the candidate must obey on generated inputs."""
        ...

    @property
    def invariant_examples(self) -> int:
        """How many generated examples per invariant."""
        ...

    def gate_profile(self) -> tuple[GateName, ...]:
        """Which gates run, cheapest first."""
        ...

    @property
    def entry(self) -> str:
        """The primary callable a candidate must export."""
        ...

    @property
    def public_api(self) -> tuple[str, ...]:
        """Every symbol a candidate must export, including :attr:`entry`."""
        ...

    @property
    def target_file(self) -> str:
        """Absolute path to the module the implementer writes."""
        ...

    @property
    def oracle_path(self) -> str | None:
        """Repo-relative trusted oracle module, or None if the contract has no reference.

        Class 1 always has one — it *is* the definition of correct. Class 2 has
        none by definition, though it may still ship a module here to supply
        contract-local backtest comparators.
        """
        ...

    @property
    def tests_path(self) -> str:
        """Repo-relative path to the trusted acceptance tests."""
        ...

    @property
    def tests_file(self) -> str:
        """Absolute path to the trusted acceptance tests."""
        ...

# A candidate must run in at most this fraction of the baseline's time (i.e. be
# at least 10% faster) — "beat the baseline by a defined margin". Per-contract,
# because what counts as a meaningful win is a property of the target, not of
# the engine. (docs/CLASS2_CONTRACT.md calls this ``min_speedup``; it is named
# for what it actually is — 0.90 is a latency *ratio*, not a 0.9x speedup — and
# matches ``max_latency_ratio`` in sis.canary.)
DEFAULT_MAX_LATENCY_RATIO = 0.90

# How many randomised differential-correctness trials to run. The point is
# inputs the candidate cannot predict, so more is strictly safer; 300 keeps the
# gate well under a second for a microsecond-scale target.
DEFAULT_DIFF_TRIALS = 300


@dataclass(frozen=True)
class OptimizationContract:
    """Class-1 (optimization) contract: a target that already works, made faster.

    Correctness is *agreement with a known-correct reference* on inputs the
    candidate cannot predict; "better" is *faster by a margin*. Class 2
    (feature construction, no pre-existing correct version) replaces the
    reference with invariants — see docs/CLASS2_CONTRACT.md.
    """

    name: str
    entry: str          # the function under optimisation, e.g. "sum_of_divisors"
    target_path: str    # repo-relative; SOFT — the only thing the SWE may write
    oracle_path: str    # repo-relative; FORBIDDEN — reference + inputs + generator
    tests_path: str     # repo-relative; FORBIDDEN — acceptance tests, run in-sandbox
    max_latency_ratio: float = DEFAULT_MAX_LATENCY_RATIO
    diff_trials: int = DEFAULT_DIFF_TRIALS
    # repo-relative; the stub proposer's canned answer for this contract
    # (SIS_PROPOSER=stub, the offline/zero-cost/CI default). None means the stub
    # has nothing to offer here — SIS_PROPOSER=claude is required for this
    # contract, and propose() says so rather than reading the wrong file.
    stub_candidate_path: str | None = None
    # Recorded episodes the candidate must reproduce (sis.backtest). Empty for
    # both shipped targets, and that is not an oversight: `sum_of_divisors` and
    # `sort` are pure functions with no history to reproduce, and inventing some
    # would make the gate look exercised while testing nothing. The mechanism
    # ships now because omnitrack's acceptance evidence is exactly this shape
    # (docs/OMNITRACK_VISION.md, Phases A/B); its first real fixtures arrive
    # with the first modelled target.
    backtests: tuple[Backtest, ...] = ()
    # Almost always DETERMINISTIC. A stochastic *optimisation* is a real cell in
    # the grid, though — "this Monte Carlo simulation is too slow" — which is
    # precisely why determinism is a field here rather than a third contract
    # class. See Determinism.
    determinism: Determinism = Determinism.DETERMINISTIC
    invariants: tuple[Invariant, ...] = ()
    # Mirrors ``diff_trials``' role: statistical power is a property of the
    # target, not of the engine, so it is a contract field rather than a constant.
    invariant_examples: int = DEFAULT_INVARIANT_EXAMPLES

    def gate_profile(self) -> tuple[GateName, ...]:
        """The Class-1 profile: agreement with a reference, and faster."""
        return (
            GateName.AST,
            GateName.NOOP,
            GateName.MYPY,
            GateName.INTERFACE,
            GateName.ACCEPTANCE,
            GateName.INVARIANT,
            GateName.BACKTEST,
            GateName.DIFFERENTIAL_BENCHMARK,
        )

    @property
    def public_api(self) -> tuple[str, ...]:
        """An optimisation replaces one function, so the API is that function.

        A Class-1 candidate is free to add helpers; it just may not drop the
        entry point. ``FeatureContract`` is where a multi-symbol API matters.
        """
        return (self.entry,)

    def __post_init__(self) -> None:
        if not 0.0 < self.max_latency_ratio <= 1.0:
            raise ValueError(
                f"max_latency_ratio must be in (0.0, 1.0], got {self.max_latency_ratio} "
                "(1.0 = 'no slower'; 0.90 = 'at least 10% faster')"
            )
        if self.diff_trials < 1:
            raise ValueError(f"diff_trials must be >= 1, got {self.diff_trials}")
        names = [b.name for b in self.backtests]
        if len(names) != len(set(names)):
            raise ValueError(
                f"contract {self.name!r} has duplicate backtest names: "
                f"{sorted({n for n in names if names.count(n) > 1})} — names identify a "
                "fixture in the episodic log, so they have to be unique to be useful"
            )

    @property
    def target_file(self) -> str:
        return str(PROJECT_ROOT / self.target_path)

    @property
    def oracle_file(self) -> str:
        return str(PROJECT_ROOT / self.oracle_path)

    @property
    def tests_file(self) -> str:
        return str(PROJECT_ROOT / self.tests_path)

    def load_oracle(self) -> types.ModuleType:
        """Import and return the oracle module.

        The oracle is trusted (human/PM-authored, stdlib-only, no side effects
        — never generated candidate code), so importing it in the main process
        is safe; this is a different act from the gauntlet copying its *text*
        into the sandbox for the candidate to be judged against.

        Used by the proposer to build a contract-specific prompt — the entry
        function's required signature and its trusted-reference source — without
        a second, separate place declaring what "correct" means. The oracle
        already is that single source of truth.
        """
        oracle_spec = importlib.util.spec_from_file_location(
            f"oracle_{self.name}", self.oracle_file)
        if oracle_spec is None or oracle_spec.loader is None:
            raise RuntimeError(f"cannot load oracle module at {self.oracle_path}")
        module = importlib.util.module_from_spec(oracle_spec)
        oracle_spec.loader.exec_module(module)
        return module


@dataclass(frozen=True)
class FeatureContract:
    """Class-2 (feature construction) contract: build what a spec describes.

    There is **no pre-existing correct version to differ against**, and "10%
    faster" is not what makes it right. So correctness comes from the spec's
    acceptance tests and (once OMNI-18 lands) the domain's invariants, and the
    reference oracle is replaced by recorded history via ``backtests``.

    Two gates from the Class-1 profile are deliberately absent:

    - **no-op** — there is no baseline to be identical *to*. The first
      implementation of a feature has nothing to be a no-op against, and a
      later one that happens to match the previous version is a legitimate
      "nothing to change here", not a gaming attempt.
    - **differential + benchmark** — both presuppose a reference implementation
      that can be evaluated on demand. Keeping the benchmark would also quietly
      re-import the wrong success criterion: a feature that is correct and slow
      has passed, and a latency budget belongs in a ``DomainSLO`` (OMNI-24),
      which is explicitly not a correctness gate.

    ``spec_ref`` is the Confluence page the feature was specified in — the
    provenance root, so ``spec → contract → branch/PR → verdict → outcome``
    reconstructs from artifacts rather than from memory.
    """

    name: str
    spec_ref: str                 # Confluence page id — the provenance root
    entry: str                    # the primary callable
    entry_module: str             # repo-relative; SOFT — where the SWE writes
    acceptance_tests: str         # repo-relative; FORBIDDEN — the trusted exam
    # Every symbol the feature must export. Required rather than defaulted to
    # ``(entry,)``: for a feature the API *is* part of the specification, and a
    # contract that shrugs about its own surface has given the implementer one
    # less thing it must get right.
    public_api: tuple[str, ...]
    # The name of a typing.Protocol the implementation should satisfy.
    # **Declared, not yet enforced** — say so plainly rather than implying a
    # check that does not run. Enforcing it means resolving the Protocol inside
    # the sandbox and asserting the candidate module against it under mypy, and
    # no shipped contract needs that yet. The interface gate meanwhile answers
    # the cheaper question: do the symbols exist and is the entry callable.
    protocol: str | None = None
    # Optional here, unlike Class 1: a feature has no reference implementation
    # by definition. A contract may still point at a module to supply its own
    # backtest comparators, which take precedence over the shared library.
    oracle_path: str | None = None
    backtests: tuple[Backtest, ...] = ()
    determinism: Determinism = Determinism.DETERMINISTIC
    invariants: tuple[Invariant, ...] = ()
    # Mirrors ``diff_trials``' role: statistical power is a property of the
    # target, not of the engine, so it is a contract field rather than a constant.
    invariant_examples: int = DEFAULT_INVARIANT_EXAMPLES

    def __post_init__(self) -> None:
        if self.entry not in self.public_api:
            raise ValueError(
                f"contract {self.name!r}: entry {self.entry!r} is not in public_api "
                f"{self.public_api} — the entry point is part of the API by definition, "
                "and the interface gate only checks what public_api lists"
            )
        names = [b.name for b in self.backtests]
        if len(names) != len(set(names)):
            raise ValueError(
                f"contract {self.name!r} has duplicate backtest names: "
                f"{sorted({n for n in names if names.count(n) > 1})}"
            )

    def gate_profile(self) -> tuple[GateName, ...]:
        """The Class-2 profile: the right shape, the spec's cases, and history."""
        return (
            GateName.AST,
            GateName.MYPY,
            GateName.INTERFACE,
            GateName.ACCEPTANCE,
            GateName.INVARIANT,
            GateName.BACKTEST,
        )

    @property
    def target_file(self) -> str:
        return str(PROJECT_ROOT / self.entry_module)

    @property
    def tests_path(self) -> str:
        return self.acceptance_tests

    @property
    def tests_file(self) -> str:
        return str(PROJECT_ROOT / self.acceptance_tests)


# The bootstrap target. Registered in the SelfModel at bootstrap, and used as
# the fallback by callers that don't name a contract (see gauntlet.validate).
SUM_OF_DIVISORS = OptimizationContract(
    name="sum_of_divisors",
    entry="sum_of_divisors",
    target_path="runtime/target.py",
    oracle_path="specs/sum_of_divisors/oracle.py",
    tests_path="specs/sum_of_divisors/tests.py",
    stub_candidate_path="runtime/candidates/optimised_target.py",
)

# The second target. Its whole reason for existing is to prove the contract
# abstraction generalises — nothing in sis/ knows either target by name, so a
# third one is a new specs/ directory and an entry here, not an engine change.
# Deliberately different in shape from SUM_OF_DIVISORS: a collection-valued
# entry point, freely scalable input size (so per-request work can dominate Ray
# Serve's dispatch overhead when it becomes the served canary target, OMNI-11),
# and no ``benchmark()`` in its required API.
SORT = OptimizationContract(
    name="sort",
    entry="sort_numbers",
    target_path="runtime/sort_target.py",
    oracle_path="specs/sort/oracle.py",
    tests_path="specs/sort/tests.py",
    stub_candidate_path="runtime/candidates/optimised_sort.py",
)

DEFAULT_CONTRACTS: tuple[OptimizationContract, ...] = (SUM_OF_DIVISORS, SORT)

# The first Class-2 target: build roman-numeral conversion from a spec, with no
# pre-existing implementation to differ against. Chosen from the shortlist in
# docs/CLASS2_CONTRACT.md — a total function over a tiny, closed, unambiguous
# domain, and one whose round-trip property gives OMNI-18's invariant gate an
# obvious first customer.
#
# Two exports rather than one on purpose: it is the smallest honest example of
# an API that is *specified* rather than inherited from an existing function,
# which is the distinction Class 2 turns on.
ROMAN = FeatureContract(
    name="roman",
    spec_ref="CONF-ROMAN",  # placeholder until the intake page exists
    entry="to_roman",
    entry_module="runtime/roman.py",
    acceptance_tests="specs/roman/tests.py",
    public_api=("to_roman", "from_roman"),
    # Strategies and domain laws only — no reference implementation. See the
    # module docstring for why a Class-2 "oracle" is a different thing.
    oracle_path="specs/roman/oracle.py",
    invariants=(
        # The law that makes this a good first Class-2 target: checkable without
        # knowing what the right numeral is, so it catches a candidate that is
        # self-consistently wrong on inputs nobody enumerated.
        Invariant(name="round_trip", strategy="in_range_values", check="round_trip"),
        # Separate from round-trip on purpose: a candidate can round-trip
        # perfectly with a non-canonical encoding (IIII parses back to 4), and
        # the spec asks for the canonical numeral, not merely a reversible one.
        Invariant(name="canonical", strategy="in_range_values", check="canonical_form"),
    ),
)

# Kept separate from DEFAULT_CONTRACTS, which the SelfModel registers and types
# as optimisation contracts. Merging the two would push a Class-2 shape through
# the contract registry, the ``--contract`` selector and the proposer prompt —
# all of which assume a reference oracle and a benchmark. Wiring feature
# contracts through the loop is its own change; this ticket delivers the
# gauntlet profile they run under.
FEATURE_CONTRACTS: tuple[FeatureContract, ...] = (ROMAN,)


def default_contract() -> OptimizationContract:
    """The contract assumed when a caller doesn't name one (back-compat)."""
    return SUM_OF_DIVISORS
