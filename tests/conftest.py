"""Pytest fixtures / environment for the main test suite.

The target's own acceptance tests are no longer collected here: they live in
``specs/<contract>/tests.py`` and run **inside the gauntlet sandbox**, against
whichever candidate is being validated, where ``import target`` resolves to the
candidate. Running them in this suite would only ever re-test the local
``runtime/target.py`` — a different question from the one the gauntlet asks, and
one that needed a ``sys.path`` hack to answer.
"""

import os
from collections.abc import Iterator, MutableMapping
from typing import Any

import pytest

# --- the developer's shell does not reach the suite (OMNI-59, M23) -----------
#
# A shell set up for a real run (SIS_ADAPTERS=real, SIS_PROPOSER=claude, an
# Anthropic key, AWS credentials) used to change what `pytest` exercised, with
# no sign that it had: real adapters, real spend, the real episodic store.
#
# The scrub runs here, at conftest import, not in a fixture: pytest imports
# this file before any test module, so it runs before anything starts Ray. A
# role actor inherits the driver's environment when it is *created* (the trap
# in CLAUDE.md), so a session fixture that ran after a module-level cluster
# would be too late for that cluster. Test modules that set their own SIS_*
# value at import (test_feature_cycles, test_serve_cloud) still do, after this.

# Everything the engine reads as configuration or a credential: every SIS_*
# name (config keys and EnvSecretSource's credentials alike).
SCRUBBED_PREFIXES = ("SIS_",)
# Credentials read outside that prefix: the Anthropic SDK reads its key from
# the environment (sis/llm.py), boto3 its AWS credentials and profile.
SCRUBBED_NAMES = frozenset({
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
})


def scrub_environment(environ: MutableMapping[str, str]) -> dict[str, str]:
    """Remove what :data:`SCRUBBED_PREFIXES`/:data:`SCRUBBED_NAMES` name.

    Returns what was removed, so the run can name it and a test that opts in
    (:func:`real_env`) can put it back.
    """
    removed = {name: value for name, value in environ.items()
               if name in SCRUBBED_NAMES or name.startswith(SCRUBBED_PREFIXES)}
    for name in removed:
        del environ[name]
    return removed


SCRUBBED_FROM_SHELL = scrub_environment(os.environ)


def pytest_report_header() -> str | None:
    """Name (never show) what the scrub took out of the developer's shell."""
    if not SCRUBBED_FROM_SHELL:
        return None
    return ("ignored from your shell (OMNI-59; opt in with the real_env fixture): "
            + ", ".join(sorted(SCRUBBED_FROM_SHELL)))


@pytest.fixture
def real_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Opt in: this test sees the shell's scrubbed variables again.

    For a test that means to reach a real backend. It reaches this process
    only: a Ray actor created earlier in the session kept the scrubbed
    environment, so pass per-test configuration as an argument, as everywhere
    else. Restored when the test ends.
    """
    for name, value in SCRUBBED_FROM_SHELL.items():
        monkeypatch.setenv(name, value)
    yield dict(SCRUBBED_FROM_SHELL)


# Default the suite to the no-op episodic store so cycle tests neither write to
# the real runtime/episodic.jsonl nor persist CEO state across tests (which would
# leak a tripped breaker from one test into another). Tests that exercise a real
# backend construct it directly on a tmp path or set SIS_EPISODIC_STORE themselves.
os.environ.setdefault("SIS_EPISODIC_STORE", "none")

# One accepted step per feature: a cycle that passes opens its PR at once, the
# behaviour every cycle test before OMNI-130 was written against. The
# multi-step feature flow sets its own value, before its cluster starts
# (tests/test_feature_cycles.py).
os.environ.setdefault("SIS_FEATURE_MAX_STEPS", "1")


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
