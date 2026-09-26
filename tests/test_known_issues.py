"""Every entry in ``docs/KNOWN_ISSUES.md`` carries its Jira ticket.

KNOWN_ISSUES is the canonical defect list; Jira is where work gets planned and
seen. An entry without a ticket is a defect the board cannot see — which is how
24 open Lows, a won't-fix Medium and every pre-Jira resolution ended up
invisible there until they were backfilled on 2026-09-26. So the rule is pinned
rather than remembered.

An entry is a top-level bullet in one of the issue sections. It passes when its
first line starts with a ticket reference (``- [OMNI-45] **H2 — ...``), or when
it is a batch heading (``**L10–L14**``, ``**Minor batch**``) whose nested
bullets each start with one. References are Markdown shortcut links, so each
also needs its link target at the bottom of the file — without one it renders
as literal ``[OMNI-45]`` text, a link that silently goes nowhere.
"""

from __future__ import annotations

import re

from sis.paths import PROJECT_ROOT

KNOWN_ISSUES = PROJECT_ROOT / "docs" / "KNOWN_ISSUES.md"

ISSUE_SECTIONS = frozenset(
    {"High", "Medium", "Low", "Resolved (Low)", "Won't fix", "Resolved"}
)

_SECTION = re.compile(r"^## (.+?)\s*$")
_ENTRY = re.compile(r"^- ")
_NESTED = re.compile(r"^ {2}- ")
_LEADING_TICKET = re.compile(r"^\s*- \[OMNI-\d+\]")
_REFERENCE = re.compile(r"\[(OMNI-\d+)\](?![(:])")
_TARGET = re.compile(r"^\[(OMNI-\d+)\]: https://olafzumpe\.atlassian\.net/browse/(OMNI-\d+)$")


def _entries(text: str) -> list[tuple[str, str, list[str]]]:
    """(section, first line, nested bullet first lines) per issue entry."""
    entries: list[tuple[str, str, list[str]]] = []
    section = ""
    for line in text.splitlines():
        heading = _SECTION.match(line)
        if heading:
            section = heading.group(1)
            continue
        if section not in ISSUE_SECTIONS:
            continue
        if _ENTRY.match(line):
            entries.append((section, line, []))
        elif _NESTED.match(line) and entries and entries[-1][0] == section:
            entries[-1][2].append(line)
    return entries


def entries_without_ticket(text: str) -> list[str]:
    """First line of every entry that neither starts with, nor groups, tickets."""
    missing = []
    for _, first, nested in _entries(text):
        if _LEADING_TICKET.match(first):
            continue
        if nested and all(_LEADING_TICKET.match(n) for n in nested):
            continue  # a batch heading: each fix in it carries its own ticket
        missing.append(first)
    return missing


def unresolved_references(text: str) -> list[str]:
    """Ticket references with no (or a mismatched) link target."""
    targets = {}
    for line in text.splitlines():
        target = _TARGET.match(line)
        if target:
            targets[target.group(1)] = target.group(2)
    bad = [k for k, v in targets.items() if k != v]  # [OMNI-45]: .../OMNI-54
    used = set(_REFERENCE.findall(text))
    return sorted(bad + [key for key in used if key not in targets])


def test_every_known_issue_has_a_jira_ticket() -> None:
    text = KNOWN_ISSUES.read_text(encoding="utf-8")
    assert entries_without_ticket(text) == []


def test_every_ticket_reference_resolves_to_its_own_issue() -> None:
    text = KNOWN_ISSUES.read_text(encoding="utf-8")
    assert unresolved_references(text) == []


def test_the_issue_sections_are_all_still_there() -> None:
    # Renaming "## Low" to "## Low severity" would otherwise exempt every
    # entry under it from the check above, and the check would still pass.
    text = KNOWN_ISSUES.read_text(encoding="utf-8")
    found = {m.group(1) for m in map(_SECTION.match, text.splitlines()) if m}
    assert ISSUE_SECTIONS <= found
    sections_with_entries = {section for section, _, _ in _entries(text)}
    assert sections_with_entries == ISSUE_SECTIONS


def test_the_checks_catch_what_they_exist_to_catch() -> None:
    # A check that cannot fail asserts nothing — the contract-author
    # discrimination check was once exactly that. Feed each one a document it
    # must reject.
    doc = "\n".join(
        [
            "## Low",
            "- [OMNI-1] **L1** — ticketed.",
            "- **L2** — no ticket.",
            "## Resolved",
            "- **A batch** of two fixes:",
            "  - [OMNI-2] **one** — ticketed.",
            "  - **two** — not.",
            "- **Grouped** and fully ticketed:",
            "  - [OMNI-3] **three**",
            "## Sequencing",
            "- **Not an issue section**, so no ticket needed.",
            "",
            "[OMNI-1]: https://olafzumpe.atlassian.net/browse/OMNI-1",
            "[OMNI-2]: https://olafzumpe.atlassian.net/browse/OMNI-20",
        ]
    )
    assert entries_without_ticket(doc) == ["- **L2** — no ticket.", "- **A batch** of two fixes:"]
    assert unresolved_references(doc) == ["OMNI-2", "OMNI-3"]
