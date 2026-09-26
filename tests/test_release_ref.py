"""OMNI-63: the AWS box runs a release tag, and a run names the code it ran.

The Terraform checks are textual (there is no HCL parser in the dev deps) and
pin intent, not syntax — `tofu validate` is what checks syntax.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from sis.paths import PROJECT_ROOT
from sis.version import code_version

_INFRA = PROJECT_ROOT / "infra" / "aws"
_TAG = re.compile(r"^v\d+\.\d+\.\d+$")


def _default(variable: str) -> str:
    text = (_INFRA / "variables.tf").read_text(encoding="utf-8")
    block = text[text.index(f'variable "{variable}"'):]
    block = block[:block.index("\n}\n")]
    match = re.search(r"^\s*default\s*=\s*(.+)$", block, re.M)
    assert match, f"{variable} has no default"
    return match.group(1).strip().strip('"')


def test_the_box_defaults_to_a_release_tag_matching_the_package_version() -> None:
    ref = _default("repo_ref")
    assert _TAG.match(ref), f"repo_ref defaults to {ref!r}, not a vX.Y.Z tag"
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "(.+)"$', pyproject, re.M)
    assert version and ref == f"v{version.group(1)}", (
        "the box's default tag and pyproject's version disagree — bump both when "
        "cutting a release (CLAUDE.md)")


def test_running_a_branch_needs_an_explicit_opt_in() -> None:
    assert _default("allow_branch_ref") == "false"
    main = (_INFRA / "main.tf").read_text(encoding="utf-8")
    assert "precondition" in main
    assert "var.allow_branch_ref" in main


def test_the_bootstrap_logs_the_code_it_runs() -> None:
    script = (PROJECT_ROOT / "scripts" / "aws_bootstrap.sh").read_text(encoding="utf-8")
    assert "rev-parse HEAD" in script and "describe --tags" in script


def test_code_version_names_this_checkout() -> None:
    version = code_version()
    head = subprocess.run(["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert version["sha"] == head
    assert version["describe"] not in ("", "unknown")


def test_code_version_outside_a_repository_says_unknown(tmp_path: Path) -> None:
    assert code_version(tmp_path) == {"sha": "unknown", "describe": "unknown"}
