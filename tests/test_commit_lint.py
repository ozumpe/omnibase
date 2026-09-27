"""OMNI-125: the commit-lint workflow, run for real against a throwaway repository.

The step's shell script is lifted out of `.github/workflows/commit-lint.yml` and
executed as-is, so a test here cannot pass against a copy that has drifted from
what CI runs. The bug it pins was invisible to reading: under `pipefail`,
`echo "$msg" | grep -q` reports *no match* for a message larger than the pipe
buffer, because grep exits at the first match and `echo` dies of SIGPIPE. The
v0.2.0 release squash (165 KB, 138 OMNI keys) was rejected as keyless.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from sis.paths import PROJECT_ROOT

WORKFLOW = PROJECT_ROOT / ".github/workflows/commit-lint.yml"

# Well past Linux's 64 KiB pipe buffer, the size that turned the bug on.
_PADDING = ("filler line of a long squash message, the kind a release PR makes\n" * 5000)


def _lint_script() -> str:
    """The body of the workflow's one `run: |` block, dedented."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "run: |"]
    assert len(starts) == 1, "commit-lint.yml is expected to have exactly one run block"
    indent = len(lines[starts[0]]) - len(lines[starts[0]].lstrip())
    body: list[str] = []
    for line in lines[starts[0] + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


def _lint(tmp_path: Path, message: str) -> subprocess.CompletedProcess[str]:
    """Commit *message* on top of a base commit and run commit-lint on the pair."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        # The user's global config may sign commits or install hooks.
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "develop")
    git("commit", "-q", "--allow-empty", "-m", "OMNI-1: base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "msg").write_text(message, encoding="utf-8")
    git("commit", "-q", "--allow-empty", "--cleanup=verbatim", "-F", str(tmp_path / "msg"))
    head = git("rev-parse", "HEAD")
    return subprocess.run(
        ["bash", "-c", _lint_script()], cwd=repo, capture_output=True, text=True,
        env={**env, "BASE_SHA": base, "HEAD_SHA": head, "BASE_REF": "develop",
             "HEAD_REF": "feature/x", "HEAD_REPO": "ozumpe/omnibase",
             "THIS_REPO": "ozumpe/omnibase"},
    )


@pytest.mark.parametrize(
    "message",
    [
        pytest.param("OMNI-125: key in the subject\n\n" + _PADDING, id="key-in-subject"),
        pytest.param("Release v9.9.9 (#1)\n\n" + _PADDING + "* OMNI-125: squashed\n" + _PADDING,
                     id="key-deep-in-the-body"),
        pytest.param("Ad-hoc fix\n\nNo-Ticket: trivial\n\n" + _PADDING, id="no-ticket-line"),
        pytest.param("OMNI-125: a short message\n", id="short"),
    ],
)
def test_a_commit_with_a_key_passes_whatever_its_size(tmp_path: Path, message: str) -> None:
    result = _lint(tmp_path, message)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "message",
    [pytest.param("no key here\n", id="short"),
     pytest.param("no key here\n\n" + _PADDING, id="large")],
)
def test_a_commit_without_a_key_still_fails(tmp_path: Path, message: str) -> None:
    # The harness must be able to see a failure, or the passing cases prove nothing.
    result = _lint(tmp_path, message)
    assert result.returncode == 1
    assert "has no Jira key" in result.stdout
