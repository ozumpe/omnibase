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

from dataclasses import dataclass

from sis.paths import PROJECT_ROOT

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

    def __post_init__(self) -> None:
        if not 0.0 < self.max_latency_ratio <= 1.0:
            raise ValueError(
                f"max_latency_ratio must be in (0.0, 1.0], got {self.max_latency_ratio} "
                "(1.0 = 'no slower'; 0.90 = 'at least 10% faster')"
            )
        if self.diff_trials < 1:
            raise ValueError(f"diff_trials must be >= 1, got {self.diff_trials}")

    @property
    def target_file(self) -> str:
        return str(PROJECT_ROOT / self.target_path)

    @property
    def oracle_file(self) -> str:
        return str(PROJECT_ROOT / self.oracle_path)

    @property
    def tests_file(self) -> str:
        return str(PROJECT_ROOT / self.tests_path)


# The bootstrap target. Registered in the SelfModel at bootstrap, and used as
# the fallback by callers that don't name a contract (see gauntlet.validate).
SUM_OF_DIVISORS = OptimizationContract(
    name="sum_of_divisors",
    entry="sum_of_divisors",
    target_path="runtime/target.py",
    oracle_path="specs/sum_of_divisors/oracle.py",
    tests_path="specs/sum_of_divisors/tests.py",
)

DEFAULT_CONTRACTS: tuple[OptimizationContract, ...] = (SUM_OF_DIVISORS,)


def default_contract() -> OptimizationContract:
    """The contract assumed when a caller doesn't name one (back-compat)."""
    return SUM_OF_DIVISORS
