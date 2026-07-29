"""Pytest fixtures / path setup for the main test suite.

test_target.py does a bare ``import target``. The live target lives in
runtime/, so put that directory on sys.path here. (Inside the gauntlet's
sandbox, target.py sits next to the test and this conftest is absent — the
import resolves there via the sandbox's own directory.)
"""

import os
import sys

from sis.paths import RUNTIME_DIR

sys.path.insert(0, str(RUNTIME_DIR))

# Default the suite to the no-op episodic store so cycle tests neither write to
# the real runtime/episodic.jsonl nor persist CEO state across tests (which would
# leak a tripped breaker from one test into another). Tests that exercise a real
# backend construct it directly on a tmp path or set SIS_EPISODIC_STORE themselves.
os.environ.setdefault("SIS_EPISODIC_STORE", "none")
