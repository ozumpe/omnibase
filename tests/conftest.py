"""Pytest fixtures / environment for the main test suite.

The target's own acceptance tests are no longer collected here: they live in
``specs/<contract>/tests.py`` and run **inside the gauntlet sandbox**, against
whichever candidate is being validated, where ``import target`` resolves to the
candidate. Running them in this suite would only ever re-test the local
``runtime/target.py`` — a different question from the one the gauntlet asks, and
one that needed a ``sys.path`` hack to answer.
"""

import os
from typing import Any

import pytest

# Default the suite to the no-op episodic store so cycle tests neither write to
# the real runtime/episodic.jsonl nor persist CEO state across tests (which would
# leak a tripped breaker from one test into another). Tests that exercise a real
# backend construct it directly on a tmp path or set SIS_EPISODIC_STORE themselves.
os.environ.setdefault("SIS_EPISODIC_STORE", "none")


# Modules whose every test needs a live Ray Serve deployment.
SERVE_MODULES = frozenset({
    "test_live_canary", "test_serve_cloud", "test_loadgen", "test_loop_serve",
})

# Root fixtures that start Serve. Used for modules that are *deliberately split*
# — test_serving.py tests its pure helpers without Serve at all and only a
# handful of tests pay for a deployment, so marking that module wholesale would
# quietly drop its fast half from the default run.
#
# Note what is absent: `handles`. Six modules define a fixture by that name and
# only two of them are Serve tests; the rest are plain Ray actors. Matching on it
# would pull most of the suite into the slow half.
SERVE_FIXTURES = frozenset({"slots", "ray_serve", "served"})


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply the ``serve`` marker by module or fixture closure.

    Done here rather than with ``pytestmark`` in each module so there is one
    place to read the rule, and so a module can be partly fast and partly slow
    without a decorator on every function. pytest resolves the whole fixture
    closure into ``fixturenames``, so a test reaching Serve through two layers
    of intermediate fixture is still caught.
    """
    for item in items:
        module = item.module.__name__.rsplit(".", 1)[-1] if item.module else ""
        if module in SERVE_MODULES or (SERVE_FIXTURES & set(item.fixturenames)):
            item.add_marker(pytest.mark.serve)


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: Any) -> None:
    """Say out loud when the default run has skipped the Ray Serve tests.

    The default ``-m 'not serve'`` makes the inner loop fast, and it also makes a
    bare ``pytest`` mean something narrower than "the suite passes". Printing
    that on every run is the cheap half of not repeating OMNI-16, where CI's
    trigger filtered on the PR *base* and a stacked branch merged green having
    verified nothing at all. A green tick should never quietly mean less than it
    appears to; the expensive half is CI, which runs both halves explicitly and
    is pinned by tests/test_test_layout.py.
    """
    del exitstatus
    # Match the *exclusion*, not the word "serve" — the default expression is
    # "not serve", so a naive `"serve" in markexpr` check silences the very
    # notice it was written to print. (It did.)
    markexpr = str(config.getoption("markexpr", "") or "")
    if "not serve" not in markexpr:
        return
    terminalreporter.write_sep(
        "-",
        "Ray Serve integration tests were NOT run (-m 'not serve'). "
        "Run `poetry run pytest -m serve -n 0` before pushing, or let CI do it.",
        yellow=True,
    )
