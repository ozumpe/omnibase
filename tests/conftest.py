"""Pytest fixtures / path setup for the main test suite.

test_target.py does a bare ``import target``. The live target lives in
runtime/, so put that directory on sys.path here. (Inside the gauntlet's
sandbox, target.py sits next to the test and this conftest is absent — the
import resolves there via the sandbox's own directory.)
"""

import sys

from sis.paths import RUNTIME_DIR

sys.path.insert(0, str(RUNTIME_DIR))
