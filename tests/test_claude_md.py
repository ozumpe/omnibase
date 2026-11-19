"""OMNI-211: ``CLAUDE.md`` stays short.

Every session (each routine run and each thread) loads ``CLAUDE.md``, and its
``@`` includes with it. It had grown to 733 lines plus two included files,
because each fix added a paragraph of history to it. The detail lives in
``docs/ENGINE_RULES.md``; history lives in ``CHANGELOG.md`` and Jira.
"""

from __future__ import annotations

import re

import pytest

from sis.paths import PROJECT_ROOT

# Reads CLAUDE.md, so a docs-only PR still runs it (OMNI-60).
pytestmark = pytest.mark.docs

CLAUDE_MD = PROJECT_ROOT / "CLAUDE.md"
MAX_LINES = 200

# An `@path` at the start of a line or after whitespace includes that file in
# every session; an e-mail address or a decorator in a code span does not.
_INCLUDE = re.compile(r"(?:^|\s)@[\w./-]+\.md\b")


def test_claude_md_stays_under_its_line_cap() -> None:
    lines = CLAUDE_MD.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= MAX_LINES, (
        f"CLAUDE.md has {len(lines)} lines (cap {MAX_LINES}). Move detail to "
        "docs/ENGINE_RULES.md and history to CHANGELOG.md or the Jira ticket.")


def test_claude_md_includes_no_other_file() -> None:
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert _INCLUDE.findall(text) == [], "link to the file instead of @-including it"


def test_the_detail_it_points_to_exists() -> None:
    text = CLAUDE_MD.read_text(encoding="utf-8")
    for path in sorted(set(re.findall(r"`((?:docs/)?[A-Z][\w_]+\.md)`", text))):
        assert (PROJECT_ROOT / path).is_file(), f"CLAUDE.md names a missing {path}"


def test_the_include_check_catches_an_include() -> None:
    assert _INCLUDE.findall("Detailed brief: @DESIGN.md — read it")
    assert not _INCLUDE.findall("mail me at someone@example.md")
