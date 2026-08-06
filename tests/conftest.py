"""Pytest fixtures / environment for the main test suite.

The target's own acceptance tests are no longer collected here: they live in
``specs/<contract>/tests.py`` and run **inside the gauntlet sandbox**, against
whichever candidate is being validated, where ``import target`` resolves to the
candidate. Running them in this suite would only ever re-test the local
``runtime/target.py`` — a different question from the one the gauntlet asks, and
one that needed a ``sys.path`` hack to answer.
"""

import os

# Default the suite to the no-op episodic store so cycle tests neither write to
# the real runtime/episodic.jsonl nor persist CEO state across tests (which would
# leak a tripped breaker from one test into another). Tests that exercise a real
# backend construct it directly on a tmp path or set SIS_EPISODIC_STORE themselves.
os.environ.setdefault("SIS_EPISODIC_STORE", "none")
