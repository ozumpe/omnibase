"""sis.paths — single source of truth for filesystem locations.

All paths are absolute and derived from the project root, so the engine
behaves identically regardless of the current working directory. This is
important because the loop rewrites files at runtime and spawns
subprocesses with their own CWD.
"""

import pathlib

# project root = parent of the sis/ package directory
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Runtime-mutable state (the loop reads and writes these).
RUNTIME_DIR = PROJECT_ROOT / "runtime"
TARGET_PATH = RUNTIME_DIR / "target.py"            # the live target module
# Episodic store (sis/episodic.py) — backend chosen by SIS_EPISODIC_STORE.
EPISODIC_JSONL = RUNTIME_DIR / "episodic.jsonl"
EPISODIC_DUCKDB = RUNTIME_DIR / "episodic.duckdb"

# Generated/candidate code.
CANDIDATES_DIR = RUNTIME_DIR / "candidates"
OPTIMISED_CANDIDATE_PATH = CANDIDATES_DIR / "optimised_target.py"

TESTS_DIR = PROJECT_ROOT / "tests"

# Contracts: the reference oracle + acceptance tests per target. POLICY-FORBIDDEN
# (sis.policy.GUARDRAIL_DIRS) — the implementer must not edit its own exam. The
# gauntlet copies a contract's oracle and tests into its sandbox; the concrete
# paths live on the contract, not here, since there is one set per target.
SPECS_DIR = PROJECT_ROOT / "specs"
# Shared comparators for the backtest gate — the marking scheme, one set for
# every contract, so it sits at the root of specs/ rather than inside any one
# contract's directory. A contract may still override a comparator by name in
# its own oracle (see sis.backtest.build_script).
COMPARATORS_PATH = SPECS_DIR / "comparators.py"
# Shared invariant predicates + strategies — the domain-agnostic laws, next to
# the comparators for the same reason: one set for every contract, overridable
# per contract from its own oracle.
INVARIANTS_PATH = SPECS_DIR / "invariants.py"
