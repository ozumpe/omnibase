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

import ast
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from sis import gauntlet
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
class Discrimination:
    """Whether a drafted exam can tell a real implementation from nothing at all."""

    checked: bool          # False when the check could not run (say so, don't guess)
    discriminates: bool
    detail: str

    def summary(self) -> str:
        if not self.checked:
            return f"NOT CHECKED — {self.detail}"
        return ("rejects a null implementation" if self.discriminates
                else f"DOES NOT REJECT A NULL IMPLEMENTATION — {self.detail}")


def null_implementation(public_api: tuple[str, ...]) -> str:
    """Source for a module that exports the right names and does nothing useful.

    Every function returns ``None`` and raises nothing. A drafted exam that
    *passes* against this is asserting nothing about behaviour — which is the
    cheapest and most likely defect in any generated test suite, and invisible to
    a reviewer skimming plausible-looking code.
    """
    body = "\n\n".join(
        f"def {name}(*args: object, **kwargs: object) -> None:\n"
        f'    """Null implementation — exports the name, promises nothing."""\n'
        f"    return None"
        for name in public_api
    )
    return f'"""Null implementation, for the discrimination check."""\n\n\n{body}\n'


def check_discrimination(
    draft: ContractDraft, *, public_api: tuple[str, ...]
) -> Discrimination:
    """Run a draft's acceptance tests against a null implementation.

    Returns whether they *failed*, which is the outcome we want: an exam that
    passes against a module that does nothing is vacuous.

    Runs in the gauntlet's sandbox, for the ordinary reason — the drafted tests
    are generated code and generated code does not execute in the main process —
    and reuses that machinery rather than growing a second sandbox with its own
    subtly different guarantees.

    A draft whose bodies are all ``NotImplementedError`` stubs "fails" here too,
    of course, and that is reported honestly rather than counted as a pass:
    ``detail`` says how many cases actually assert something, because "fails
    against a stub because it is itself a stub" is not evidence of anything.
    """
    tests_source = draft.files.get("tests.py")
    if tests_source is None:
        return Discrimination(
            checked=False, discriminates=False,
            detail="the draft has no tests.py to run",
        )
    written = tests_source.count("assert ") + tests_source.count("pytest.raises")
    if written == 0:
        return Discrimination(
            checked=True, discriminates=False,
            detail="no case asserts anything yet — every body is still a stub",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "target.py").write_text(null_implementation(public_api), encoding="utf-8")
        (tmp / "sitecustomize.py").write_text(gauntlet._NETWORK_GUARD, encoding="utf-8")
        tests_dir = tmp / "tests"
        tests_dir.mkdir()
        (tests_dir / "__init__.py").write_text("", encoding="utf-8")
        (tests_dir / "test_target.py").write_text(tests_source, encoding="utf-8")
        env = gauntlet._sandbox_env(home=tmpdir, pythonpath=tmpdir)
        result = gauntlet._run(
            [gauntlet._PY, "-m", "pytest", str(tests_dir), "-q", "--tb=no"], tmpdir, env
        )

    if result.returncode == 124:
        return Discrimination(
            checked=False, discriminates=False,
            detail="the drafted tests timed out against a null implementation",
        )
    if result.returncode == 0:
        return Discrimination(
            checked=True, discriminates=False,
            detail=f"all {written} assertion(s) passed against a module that returns "
                   "None for everything",
        )
    return Discrimination(
        checked=True, discriminates=True,
        detail=f"{written} assertion(s), and they fail against a null implementation",
    )


@dataclass(frozen=True)
class StagedContract:
    """A draft written to the staging area, waiting for a human."""

    name: str
    spec_ref: str
    directory: Path
    files: tuple[str, ...]
    # Whether the drafted exam can tell an implementation from nothing at all.
    # Recorded rather than enforced: a human may still approve a vacuous exam —
    # they are sovereign over their own contract — but never unknowingly.
    discrimination: Discrimination | None = None

    @property
    def target(self) -> Path:
        """Where promotion would put it. Not written until approved."""
        return SPECS_DIR / self.name


def stage(
    draft: ContractDraft,
    *,
    staging_dir: Path | None = None,
    public_api: tuple[str, ...] = (),
) -> StagedContract:
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
    # Checked at stage time, not at promotion: the point is that a human sees
    # the verdict *before* deciding, and a check that ran during approval would
    # arrive after the judgement it is meant to inform.
    discrimination = (
        check_discrimination(draft, public_api=public_api) if public_api else None
    )
    if discrimination is not None:
        # Appended to the staged copy rather than baked into the draft: the
        # verdict is a property of *running* the draft, and a reviewer opening
        # the directory should not have to go looking for it.
        readme = root / "README.md"
        existing = readme.read_text(encoding="utf-8") if readme.exists() else ""
        readme.write_text(
            f"{existing}\n## Does this exam discriminate?\n\n"
            f"**{discrimination.summary()}**\n\n"
            "An exam that passes against a module returning `None` for everything "
            "asserts nothing about behaviour, and reads exactly like one that does.\n",
            encoding="utf-8",
        )
    return StagedContract(
        name=draft.name,
        spec_ref=draft.spec_ref,
        directory=root,
        files=tuple(sorted(draft.files)),
        discrimination=discrimination,
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
EXAMPLES_HEADING = "worked examples"

# `to_roman(4) -> "IV"` / `plan([1, 2]) -> raises ValueError`
_EXAMPLE = re.compile(
    r"^(?P<entry>[A-Za-z_]\w*)\s*\((?P<args>.*)\)\s*(?:->|→|=)\s*(?P<expected>.+)$"
)
_RAISES = re.compile(r"^raises?\s+(?P<exc>[A-Za-z_]\w*)$", re.IGNORECASE)


@dataclass(frozen=True)
class WorkedExample:
    """One concrete case a human wrote down, transcribed rather than inferred.

    ``args`` and ``expected`` are already-evaluated Python literals. Nothing here
    is a guess: the values are exactly what the spec said, which is what makes a
    generated assertion over them trustworthy in a way a generated *judgement*
    would not be.
    """

    entry: str
    args: tuple[object, ...]
    expected: object = None
    raises: str | None = None      # exception name, when the case is a rejection

    @property
    def is_rejection(self) -> bool:
        return self.raises is not None


def parse_worked_examples(body: str) -> list[WorkedExample]:
    """Transcribe the concrete cases stated under a "worked examples" heading.

    Accepts ``entry(args) -> literal`` and ``entry(args) -> raises SomeError``,
    with backticks and list markers stripped.

    **Values are read with ``ast.literal_eval``, never ``eval``.** A spec page is
    prose from a document store — in a real deployment, prose an outside author
    can influence — and executing it would make the intake channel an arbitrary
    code path into the process that decides what "correct" means. Transcription
    must be transcription all the way down.

    A malformed example is skipped rather than raised on, because one mistyped
    bullet in a long spec should not make the page undraftable; what it must not
    do is silently become a *weaker* assertion, and skipping cannot.
    """
    examples: list[WorkedExample] = []
    for line in _section(body, EXAMPLES_HEADING):
        match = _EXAMPLE.match(line.strip().strip("`").strip())
        if match is None:
            continue
        raw_args = match.group("args").strip()
        expected_text = match.group("expected").strip().strip("`").strip()
        try:
            # A trailing comma makes a single argument parse as a 1-tuple, so
            # `f(4)` and `f(4, 5)` come back in the same shape.
            args = ast.literal_eval(f"({raw_args},)") if raw_args else ()
        except (ValueError, SyntaxError):
            continue
        if not isinstance(args, tuple):
            continue

        rejection = _RAISES.match(expected_text)
        if rejection is not None:
            examples.append(WorkedExample(
                entry=match.group("entry"), args=args, raises=rejection.group("exc")))
            continue
        try:
            expected = ast.literal_eval(expected_text)
        except (ValueError, SyntaxError):
            continue
        examples.append(
            WorkedExample(entry=match.group("entry"), args=args, expected=expected))
    return examples


def _section(body: str, heading: str) -> list[str]:
    """Bullet lines under the heading containing *heading*, in stated order."""
    collected: list[str] = []
    inside = False
    for raw in body.splitlines():
        line = raw.strip()
        lowered = line.lstrip("#").strip().lower()
        if line.startswith("#") or (line and line.rstrip(":").lower() in
                                    (ACCEPTANCE_HEADING, LAWS_HEADING, EXAMPLES_HEADING)):
            inside = heading in lowered
            continue
        if inside and line.startswith(("-", "*")):
            text = line[1:].strip()
            if text:
                collected.append(text)
    return collected


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
    return _section(body, ACCEPTANCE_HEADING), _section(body, LAWS_HEADING)


def _worked_example_source(examples: list[WorkedExample]) -> str:
    """Real, parametrised assertions built from transcribed values.

    The only generated code in a draft with a body rather than a stub, and the
    reason it is safe to have one: every value in it was written by a human in
    the spec. The generator chooses the *shape* of the assertion — call the
    entry, compare, or expect a raise — and nothing about what is correct.

    Grouped into two parametrised tests rather than one per example so a spec
    with thirty cases produces a readable file a human will actually read, which
    is the whole premise of moving the burden from authoring to reviewing.
    """
    if not examples:
        return ""
    returns = [e for e in examples if not e.is_rejection]
    rejects = [e for e in examples if e.is_rejection]
    out: list[str] = [
        "\n\n# --- worked examples, transcribed verbatim from the spec ---------------\n"
        "# Values here were written by a human; only the assertion shape is generated.\n"
    ]
    if returns:
        cases = ",\n".join(f"    ({e.args!r}, {e.expected!r})" for e in returns)
        entry = returns[0].entry
        out.append(
            f'\n\n@pytest.mark.parametrize(("args", "expected"), [\n{cases},\n])\n'
            f"def test_worked_examples(args: tuple, expected: object) -> None:\n"
            f'    """Each case is stated in the spec."""\n'
            f"    assert {entry}(*args) == expected\n"
        )
    if rejects:
        cases = ",\n".join(f"    ({e.args!r}, {e.raises})" for e in rejects)
        entry = rejects[0].entry
        out.append(
            f'\n\n@pytest.mark.parametrize(("args", "expected_error"), [\n{cases},\n])\n'
            f"def test_worked_rejections(args: tuple, expected_error: type) -> None:\n"
            f'    """Each rejection is stated in the spec."""\n'
            f"    with pytest.raises(expected_error):\n"
            f"        {entry}(*args)\n"
        )
    return "".join(out)


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

    tests.append(_worked_example_source(parse_worked_examples(body)))
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
