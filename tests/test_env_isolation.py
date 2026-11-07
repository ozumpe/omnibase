"""The suite does not inherit a real run's settings from the shell (OMNI-59, M23)."""

import os
import pathlib
import subprocess
import sys

import pytest

from tests.conftest import SCRUBBED_NAMES, scrub_environment

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Set only by the outer test, for the inner run. Not SIS_-prefixed, so the
# scrub leaves it alone.
_INNER = "OMNI59_INNER_RUN"


def test_scrub_removes_sis_settings_and_credentials_and_nothing_else() -> None:
    env = {"SIS_ADAPTERS": "real", "SIS_ATLASSIAN_API_TOKEN": "t",
           "ANTHROPIC_API_KEY": "sk", "AWS_SECRET_ACCESS_KEY": "s",
           "PATH": "/bin", "HOME": "/h", "XSIS_ADAPTERS": "x"}
    removed = scrub_environment(env)
    assert env == {"PATH": "/bin", "HOME": "/h", "XSIS_ADAPTERS": "x"}
    assert removed == {"SIS_ADAPTERS": "real", "SIS_ATLASSIAN_API_TOKEN": "t",
                       "ANTHROPIC_API_KEY": "sk", "AWS_SECRET_ACCESS_KEY": "s"}
    assert "ANTHROPIC_API_KEY" in SCRUBBED_NAMES


@pytest.mark.skipif(os.environ.get(_INNER) != "1", reason="run by the test below")
def test_inner_a_default_test_sees_the_in_memory_defaults() -> None:
    from sis import config

    assert "SIS_ADAPTERS" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert config.get("adapters.mode") == "memory"
    assert config.get("proposer.backend") == "stub"


@pytest.mark.skipif(os.environ.get(_INNER) != "1", reason="run by the test below")
def test_inner_real_env_puts_the_shell_back(real_env: dict[str, str]) -> None:
    assert real_env["SIS_ADAPTERS"] == "real"
    assert os.environ["SIS_ADAPTERS"] == "real"


def test_a_shell_set_up_for_a_real_run_does_not_reach_the_suite() -> None:
    # The acceptance criterion, end to end: a pytest started from a shell that
    # exported a real run's settings still runs on the in-memory defaults.
    env = dict(os.environ, SIS_ADAPTERS="real", SIS_PROPOSER="claude",
               ANTHROPIC_API_KEY="sk-must-not-be-seen", **{_INNER: "1"})
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "-p", "no:cacheprovider",
         "-rA", f"{pathlib.Path(__file__).relative_to(ROOT)}", "-k", "inner"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "2 passed" in run.stdout
    # The header names what was ignored, and never shows a value.
    assert "ignored from your shell" in run.stdout and "SIS_ADAPTERS" in run.stdout
    assert "sk-must-not-be-seen" not in run.stdout + run.stderr
