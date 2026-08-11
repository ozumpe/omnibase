"""sis.contract_author — the one way anything gets written into ``specs/``.

``specs/`` is the exam: reference oracles, acceptance tests, domain laws,
backtest fixtures. It is POLICY-FORBIDDEN with no override, because an
implementer able to edit its own exam makes every gate downstream theatre. But
an exam nothing can ever write is an exam nobody can author either — and the
whole point of the contract-author step is to move human effort from *writing
checking code* to *stating domain laws*.

The resolution is a one-way path with a human in it:

    draft (in-process)  →  staging (loop-writable, inert)  →  specs/ (human-approved)

**Staging is inert by construction.** Nothing ever loads, imports or copies a
staged file into a sandbox; only ``specs/`` is. So the loop drafting a plausible
acceptance test costs nothing until a human reads it, which is what makes it
safe to let the loop draft at all.

**Promotion raises without approval**, the same ``RequiresHumanApproval`` shape
the adapters already use for deleting a Jira issue or merging to ``main``. One
mechanism, one place to audit, and a caller cannot get past it by being clever
about arguments — there is no ``force``.

## Why this is general rather than acceptance-test-shaped

A second writer is already known to be coming. ``docs/OMNITRACK_VISION.md`` D12
raises it: held-out backtest splits and scenario libraries also live in
``specs/``, the traces that fill them are produced by the running system, and
D5's rotation implies *ongoing* writes — yet the loop must still never write its
own exam. If this module only knew how to write acceptance tests, that pipeline
would grow a second, parallel approval mechanism with its own semantics to get
wrong. So a draft is *files*, not fields.

## What stays irreducibly human

Stating the domain laws — "cargo is conserved", "removing a supplier cannot
lower the optimal cost". The system cannot invent these, and this module does not
pretend otherwise: it drafts structure, and a human supplies and approves
meaning. The separation between author and implementer is not a workflow
nicety, it *is* the anti-gaming property, which is why the approval gate lives
here in guardrail code rather than in a role actor's method.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from sis.paths import CONTRACT_STAGING_DIR, SPECS_DIR
from sis.ports import RequiresHumanApproval

# Only these may be written into a contract directory. Not a security boundary —
# promotion is human-approved, and a human can put anything anywhere — but a
# typo'd filename that lands somewhere nothing reads is a silent, confusing
# failure, and this turns it into a loud one at draft time.
ALLOWED_SUFFIXES = frozenset({".py", ".json", ".md"})


@dataclass(frozen=True)
class ContractDraft:
    """A proposed contract, as files, before any human has seen it.

    *files* is keyed by path **relative to the contract's own directory**
    (``tests.py``, ``oracle.py``, ``backtests/q1_fixture.json``), so a draft
    cannot name a path outside the contract it claims to be for.
    """

    name: str
    spec_ref: str                       # the Confluence page this came from
    files: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or "/" in self.name or self.name.startswith("."):
            raise ValueError(
                f"contract name {self.name!r} must be a single directory name — it "
                "becomes specs/<name>/, and a name with a separator would place the "
                "exam somewhere the gates do not look"
            )
        if not self.spec_ref:
            raise ValueError(
                f"contract {self.name!r} has no spec_ref: provenance roots at the spec "
                "page, and a contract nobody can trace to a spec cannot be reviewed "
                "against one"
            )
        if not self.files:
            raise ValueError(f"contract {self.name!r} drafts no files")
        for relative in self.files:
            _reject_unsafe_path(relative, contract=self.name)


def _reject_unsafe_path(relative: str, *, contract: str) -> None:
    """Refuse anything that could escape the contract's own directory.

    Traversal is checked on the *resolved* path rather than by looking for
    ``..`` in the string: ``a/../../b`` and ``a/%2e%2e`` and a symlinked segment
    all reduce to the same question, which is whether the result is still inside
    the directory. Same reasoning as ``sis.policy._rel``, which had to resolve
    for the same reason (L4/L10).
    """
    if not relative or relative.endswith("/"):
        raise ValueError(f"contract {contract!r}: {relative!r} is not a file path")
    if Path(relative).is_absolute():
        raise ValueError(
            f"contract {contract!r}: {relative!r} is absolute; draft paths are relative "
            "to the contract's own directory"
        )
    suffix = Path(relative).suffix
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError(
            f"contract {contract!r}: {relative!r} has suffix {suffix!r}; the gates read "
            f"only {sorted(ALLOWED_SUFFIXES)} and anything else would sit there unread"
        )
    root = (SPECS_DIR / contract).resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(
            f"contract {contract!r}: {relative!r} escapes specs/{contract}/ — a draft "
            "may only write inside the contract it claims to be for"
        )


@dataclass(frozen=True)
class StagedContract:
    """A draft written to the staging area, waiting for a human."""

    name: str
    spec_ref: str
    directory: Path
    files: tuple[str, ...]

    @property
    def target(self) -> Path:
        """Where promotion would put it. Not written until approved."""
        return SPECS_DIR / self.name


def stage(draft: ContractDraft, *, staging_dir: Path | None = None) -> StagedContract:
    """Write *draft* to the staging area and return what a human has to approve.

    The loop may call this freely. Staged files are inert: no gate reads them,
    nothing imports them, and the sandbox is only ever handed ``specs/``.
    Re-staging the same contract replaces the previous draft, so a second attempt
    after review feedback does not leave the first one lying around to be
    approved by mistake.
    """
    root = (staging_dir or CONTRACT_STAGING_DIR) / draft.name
    if root.exists():
        shutil.rmtree(root)
    for relative, content in draft.files.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    return StagedContract(
        name=draft.name,
        spec_ref=draft.spec_ref,
        directory=root,
        files=tuple(sorted(draft.files)),
    )


def promote(staged: StagedContract, *, approved: bool = False) -> Path:
    """Copy a staged contract into ``specs/``. Requires human approval.

    Deliberately a keyword with no default-true path and no ``force``: this is
    the only way anything reaches the exam, so the check must be impossible to
    satisfy accidentally. The agent's job is to *surface* the decision; a human
    makes it.

    Returns the contract directory under ``specs/``.
    """
    if not approved:
        raise RequiresHumanApproval(
            f"promoting contract {staged.name!r} into specs/ requires human approval — "
            f"it is the exam the implementer is judged against (spec {staged.spec_ref})"
        )
    if not staged.directory.is_dir():
        raise FileNotFoundError(
            f"staged contract {staged.name!r} is not at {staged.directory}; stage it "
            "again before promoting"
        )
    target = staged.target
    target.mkdir(parents=True, exist_ok=True)
    for relative in staged.files:
        source = staged.directory / relative
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return target


ACCEPTANCE_HEADING = "acceptance criteria"
LAWS_HEADING = "domain laws"


def parse_spec(body: str) -> tuple[list[str], list[str]]:
    """Pull the stated acceptance criteria and domain laws out of a spec page.

    Bullet lines under a heading containing "acceptance criteria" and one
    containing "domain laws". Deliberately dumb: a spec is prose written by a
    human for humans, and the more this tries to *understand* it the more
    confidently wrong it gets. Structure is all that is extracted; meaning stays
    where it was written.

    Returns ``(criteria, laws)``, each in the order stated, because the order a
    human wrote them in is information — the first criterion is usually the
    central one.
    """
    criteria: list[str] = []
    laws: list[str] = []
    bucket: list[str] | None = None
    for raw in body.splitlines():
        line = raw.strip()
        lowered = line.lstrip("#").strip().lower()
        if line.startswith("#") or (line and line.rstrip(":").lower() in
                                    (ACCEPTANCE_HEADING, LAWS_HEADING)):
            if ACCEPTANCE_HEADING in lowered:
                bucket = criteria
            elif LAWS_HEADING in lowered:
                bucket = laws
            else:
                bucket = None      # some other heading ends the current section
            continue
        if bucket is not None and line.startswith(("-", "*")):
            text = line[1:].strip()
            if text:
                bucket.append(text)
    return criteria, laws


def _identifier(text: str, *, prefix: str, index: int) -> str:
    """A stable, readable Python identifier for a criterion or law."""
    words = [w for w in "".join(c if c.isalnum() else " " for c in text).split()][:8]
    slug = "_".join(w.lower() for w in words) or "unnamed"
    return f"{prefix}_{index}_{slug}"[:80]


def skeleton_from_spec(
    *, name: str, spec_ref: str, body: str, entry: str, public_api: tuple[str, ...]
) -> ContractDraft:
    """Draft a contract *skeleton* from a spec page.

    One failing acceptance test per stated criterion and one predicate stub per
    stated law, each carrying the human's own sentence as its docstring.

    **Every generated body raises ``NotImplementedError``**, and that is the
    design rather than laziness. The system cannot invent domain laws — that is
    the irreducibly human input this whole step exists to concentrate — so a
    skeleton that guessed at bodies would produce an exam that looks authored and
    checks nothing, which is strictly worse than an obviously unfinished one. A
    stub that fails loudly cannot be approved by accident; a plausible-looking
    wrong assertion can.

    What it does buy: the *shape* is mechanical and error-prone by hand, and
    nothing is silently dropped — every sentence the spec states arrives in the
    draft with somewhere to put the answer.
    """
    criteria, laws = parse_spec(body)
    if not criteria:
        raise ValueError(
            f"spec {spec_ref} states no acceptance criteria under a "
            f"{ACCEPTANCE_HEADING!r} heading, so there is nothing to verify against"
        )

    imports = ", ".join(sorted(public_api))
    tests = [
        f'"""Acceptance tests for the `{name}` contract — drafted from {spec_ref}.\n\n'
        "AUTHORED BY THE CONTRACT-AUTHOR STEP, NOT YET BY A HUMAN. Every case below\n"
        "fails until someone writes it. That is deliberate: an unfinished exam is\n"
        "obvious, a plausible wrong one is not.\n\n"
        "Runs inside the gauntlet sandbox: standard library only, no sis imports.\n"
        '"""\n\n'
        "import pytest\n"
        f"from target import {imports}\n"
    ]
    for index, criterion in enumerate(criteria, start=1):
        tests.append(
            f"\n\ndef {_identifier(criterion, prefix='test', index=index)}() -> None:\n"
            f'    """{criterion}"""\n'
            f'    raise NotImplementedError("write this case from the spec")\n'
        )

    files = {"tests.py": "".join(tests)}

    if laws:
        oracle = [
            f'"""Domain laws for the `{name}` contract — drafted from {spec_ref}.\n\n'
            "AUTHORED BY THE CONTRACT-AUTHOR STEP, NOT YET BY A HUMAN.\n\n"
            "Two predicate shapes (see sis.invariant):\n"
            "    check(args, output)        -> bool   # also usable on live traffic\n"
            "    check(args, output, impl)  -> bool   # offline only\n\n"
            "Runs inside the gauntlet sandbox: standard library plus Hypothesis only.\n"
            '"""\n\n'
            "from typing import Any\n\n"
            "from hypothesis import strategies as st\n\n\n"
            "def valid_inputs() -> Any:\n"
            '    """One valid input for this target, as an args tuple."""\n'
            '    raise NotImplementedError("describe a valid input for this domain")\n'
        ]
        for index, law in enumerate(laws, start=1):
            oracle.append(
                f"\n\ndef {_identifier(law, prefix='law', index=index)}"
                "(args: tuple[Any, ...], output: Any) -> bool:\n"
                f'    """{law}"""\n'
                '    raise NotImplementedError("state this law as a predicate")\n'
            )
        files["oracle.py"] = "".join(oracle)

    files["README.md"] = (
        f"# `{name}` contract\n\n"
        f"Drafted from **{spec_ref}** by the contract-author step, and **not yet "
        "reviewed by a human**.\n\n"
        f"- {len(criteria)} acceptance criteria → `tests.py`\n"
        f"- {len(laws)} domain laws → `oracle.py`\n\n"
        f"Entry point: `{entry}`. Public API: {', '.join(public_api)}.\n\n"
        "Every generated body raises `NotImplementedError`. Fill them in, then "
        "approve promotion into `specs/` — nothing here is read by any gate until "
        "that happens.\n"
    )
    return ContractDraft(name=name, spec_ref=spec_ref, files=files)


def pending(staging_dir: Path | None = None) -> tuple[str, ...]:
    """Contract names currently staged and awaiting a human. Sorted, for review."""
    root = staging_dir or CONTRACT_STAGING_DIR
    if not root.is_dir():
        return ()
    return tuple(sorted(p.name for p in root.iterdir() if p.is_dir()))
