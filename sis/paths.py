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
TARGET_BACKUP_PATH = RUNTIME_DIR / "target.py.bak"  # backup taken before promotion
# Episodic store (sis/episodic.py) — backend chosen by SIS_EPISODIC_STORE.
EPISODIC_JSONL = RUNTIME_DIR / "episodic.jsonl"
EPISODIC_DUCKDB = RUNTIME_DIR / "episodic.duckdb"

# Generated/candidate code.
CANDIDATES_DIR = RUNTIME_DIR / "candidates"
OPTIMISED_CANDIDATE_PATH = CANDIDATES_DIR / "optimised_target.py"

# Tests (the gauntlet copies the target test into its sandbox).
TESTS_DIR = PROJECT_ROOT / "tests"
TARGET_TEST_PATH = TESTS_DIR / "test_target.py"
