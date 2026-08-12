"""sis.contract_author — the one way anything gets written into ``specs/``.

``specs/`` is the exam: reference oracles, acceptance tests, domain laws,
backtest fixtures. It is POLICY-FORBIDDEN with no override, because an
implementer able to edit its own exam makes every gate downstream theatre. But
an exam nothing can ever write is an exam nobody can author either — and the
whole point of the contract-author step is to move human effort from *writing
checking code* to *stating domain laws*.

The resolution is a one-way path with a human in it:

    draft (in-process)  →  staging (loop-writable)  →  specs/ (human-approved)

**No gate reads staging.** Nothing under ``runtime/contract_staging/`` is ever
copied into a validation sandbox or imported by the loop; only ``specs/`` is. So
a plausible drafted acceptance test still costs nothing until a human reads it.

That is *narrower* than the "staging is inert" claim this module used to make,
and the narrowing is deliberate rather than cosmetic: :func:`stage` now executes
the drafted tests once, in the gauntlet's sandbox, to find out whether the exam
discriminates. Drafted tests are generated code, so that execution takes the
same preconditions every other generated-code path takes
(``gauntlet.ensure_sandbox_ready``) — and the docstring says so, because a
safety property a reader relies on must not be quietly false.

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
import builtins
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from sis import gauntlet
from sis.paths import CONTRACT_STAGING_DIR, SPECS_DIR
from sis.ports import RequiresHumanApproval

# Only these may be written into a contract directory. Not a security boundary —
# promotion is human-approved, and a human can put anything anywhere — but a
# typo'd filename that lands somewhere nothing reads is a silent, confusing
# failure, and this turns it into a loud one at draft time.
ALLOWED_SUFFIXES = frozenset({".py", ".json", ".md"})

# The discrimination check runs inside a single-threaded Ray actor, so it does
# not get the full per-gate SIS_GAUNTLET_TIMEOUT: a drafted test with an
# infinite loop would otherwise pin ContractAuthor for two minutes and queue
# every other draft behind it. Generous for a handful of transcribed assertions,
# short enough that a runaway draft is an inconvenience rather than an outage.
DISCRIMINATION_TIMEOUT_SECONDS = 30.0


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

    gauntlet.ensure_sandbox_ready()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "target.py").write_text(null_implementation(public_api), encoding="utf-8")
        (tmp / "sitecustomize.py").write_text(gauntlet._NETWORK_GUARD, encoding="utf-8")
        tests_dir = tmp / "tests"
        tests_dir.mkdir()
        (tests_dir / "__init__.py").write_text("", encoding="utf-8")
        (tests_dir / "test_target.py").write_text(tests_source, encoding="utf-8")
        report = tmp / "report.xml"
        env = gauntlet._sandbox_env(home=tmpdir, pythonpath=tmpdir)
        result = gauntlet._run(
            [gauntlet._PY, "-m", "pytest", str(tests_dir), "-q", "--tb=no",
             f"--junit-xml={report}"],
            tmpdir, env, timeout=DISCRIMINATION_TIMEOUT_SECONDS,
        )
        outcomes = _parse_junit(report)

    if result.returncode == 124:
        return Discrimination(
            checked=False, discriminates=False,
            detail="the drafted tests timed out against a null implementation",
        )
    if outcomes is None:
        return Discrimination(
            checked=False, discriminates=False,
            detail="the drafted tests produced no report — they could not be collected "
                   f"(pytest exit {result.returncode}); the draft is probably not "
                   "importable",
        )
    if not outcomes:
        return Discrimination(
            checked=False, discriminates=False,
            detail="the drafted tests defined no cases to run",
        )

    errored = [o for o in outcomes if o.errored]
    if errored:
        return Discrimination(
            checked=False, discriminates=False,
            detail=f"{len(errored)} case(s) errored rather than ran ({errored[0].detail})",
        )
    substantive = [o for o in outcomes if o.failed and not o.unwritten]
    unwritten = [o for o in outcomes if o.unwritten]
    if substantive:
        return Discrimination(
            checked=True, discriminates=True,
            detail=f"{len(substantive)} case(s) reject a null implementation"
                   + (f"; {len(unwritten)} still unwritten" if unwritten else ""),
        )
    if unwritten and len(unwritten) == len(outcomes):
        return Discrimination(
            checked=True, discriminates=False,
            detail="no case asserts anything yet — every body is still a stub",
        )
    return Discrimination(
        checked=True, discriminates=False,
        detail=f"{len(outcomes) - len(unwritten)} case(s) passed against a module that "
               "returns None for everything",
    )


@dataclass(frozen=True)
class _Outcome:
    """One test's result, from pytest's own machine-readable report."""

    name: str
    failed: bool
    errored: bool
    detail: str

    @property
    def unwritten(self) -> bool:
        """Did it fail only because nobody has written it yet?

        The distinction the whole check turns on. A skeleton's stubs raise
        ``NotImplementedError``, which makes pytest exit non-zero — so treating
        *any* non-zero exit as "rejects a null implementation" made the verdict
        unconditionally positive on the one path that actually runs, and the
        guard against a vacuous exam issued its strongest pass for exams that
        asserted nothing at all.
        """
        return self.failed and "NotImplementedError" in self.detail


def _parse_junit(report: Path) -> list[_Outcome] | None:
    """Per-test outcomes from pytest's JUnit XML, or None if it wrote none.

    Structured output rather than an exit code, because the exit code cannot
    distinguish "a real assertion failed" from "an unwritten stub raised" from
    "the module did not import" — and those are three different verdicts, only
    one of which is evidence the exam discriminates. Parsing the human summary
    would be the same guess with more string handling.
    """
    if not report.is_file():
        return None
    try:
        root = ElementTree.parse(report).getroot()
    except ElementTree.ParseError:
        return None
    outcomes: list[_Outcome] = []
    for case in root.iter("testcase"):
        failure = case.find("failure")
        error = case.find("error")
        node = failure if failure is not None else error
        detail = "" if node is None else f"{node.get('message', '')} {node.text or ''}"
        outcomes.append(_Outcome(
            name=str(case.get("name", "?")),
            failed=failure is not None,
            errored=error is not None,
            detail=detail.strip(),
        ))
    return outcomes


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

    The loop may call this freely. No gate reads a staged file and the validation
    sandbox is only ever handed ``specs/``. Re-staging the same contract replaces
    the previous draft, so a second attempt after review feedback does not leave
    the first lying around to be approved by mistake.

    **This is not a pure write.** When *public_api* is given, the drafted tests
    are executed once against a null implementation
    (:func:`check_discrimination`) so the verdict reaches a human *before* they
    decide. That runs generated code, so it takes the gauntlet's sandbox
    preconditions and a shorter timeout than a gate would.
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
    # "README.md" is added explicitly: stage() may have just created it, and a
    # file that exists on disk but not in `files` is one promote() silently
    # leaves behind — which would drop the vacuous-exam warning on the way into
    # specs/, exactly when it matters most.
    written = set(draft.files) | ({"README.md"} if discrimination is not None else set())
    return StagedContract(
        name=draft.name,
        spec_ref=draft.spec_ref,
        directory=root,
        files=tuple(sorted(written)),
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

# Ordered: a heading naming more than one section belongs to the *first* one it
# names, never to both. Two independent substring tests would put every bullet
# under "Acceptance criteria and domain laws" into both lists, drafting a test
# stub and a law predicate for each.
_HEADINGS = (ACCEPTANCE_HEADING, LAWS_HEADING, EXAMPLES_HEADING)

# The call half of a worked example. The *value* half is not matched by regex —
# see _split_example for why a greedy pattern gets `f(1) -> "a) -> b"` wrong.
_CALL = re.compile(r"^(?P<entry>[A-Za-z_]\w*)\s*\((?P<args>.*)\)$", re.DOTALL)
# `raises ValueError`, `raises ValueError("nope")`, `raises errs.RangeError`.
# Only the final name is captured; whether it is usable is decided later.
_RAISES = re.compile(
    r"^raises?\s+(?:[A-Za-z_]\w*\.)*(?P<exc>[A-Za-z_]\w*)\s*(?:\(.*\))?$", re.IGNORECASE
)
_ARROWS = ("->", "→", "=")


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
    return [e for e, _ in _transcribe(body) if e is not None]


def untranscribed_examples(body: str) -> list[str]:
    """Worked-example lines that could not be transcribed, with the reason.

    Returned rather than discarded because "the draft contains no rejection
    tests" and "the spec stated four rejection cases and none of them could be
    read" look identical from the outside, and only one of them is fine. The
    README reports these so a reviewer sees what the draft is missing.
    """
    out: list[str] = []
    for example, reason in _transcribe(body):
        if example is None:
            out.append(reason)
        elif not _is_emittable(example):
            why = ("names a non-builtin exception" if example.is_rejection
                   else "value has no literal repr")
            out.append(f"{example.entry}({example.args!r}...) ({why})")
    return out


def _transcribe(body: str) -> list[tuple[WorkedExample | None, str]]:
    """Every worked-example line, as either an example or a reason it is not one."""
    out: list[tuple[WorkedExample | None, str]] = []
    for raw in _sections(body).get(EXAMPLES_HEADING, []):
        # Backticks are stripped *everywhere*, not just at the ends: the common
        # markdown form quotes the call and the value separately (`f(1)` -> `2`),
        # which leaves an interior backtick that no amount of end-stripping helps.
        line = raw.replace("`", "").strip()
        split = _split_example(line)
        if split is None:
            out.append((None, f"{raw} (not `entry(args) -> value`)"))
            continue
        entry, args, expected_text = split

        rejection = _RAISES.match(expected_text)
        if rejection is not None:
            out.append((WorkedExample(
                entry=entry, args=args, raises=rejection.group("exc")), ""))
            continue
        try:
            expected = ast.literal_eval(expected_text)
        except (ValueError, SyntaxError):
            out.append((None, f"{raw} (value is not a literal)"))
            continue
        out.append((WorkedExample(entry=entry, args=args, expected=expected), ""))
    return out


def _split_example(line: str) -> tuple[str, tuple[object, ...], str] | None:
    """Split ``entry(args) -> value`` at the right arrow.

    Tries every arrow position rather than matching one greedily, because the
    value may legitimately contain an arrow *and* a closing paren — a greedy
    ``\\((?P<args>.*)\\)`` swallows the paren inside ``f(1) -> "a) -> b"`` and the
    whole example is then silently dropped. Regexes do not backtrack to "and the
    left side must also be a valid literal tuple", so the check is done here.
    """
    for index in range(len(line)):
        for arrow in _ARROWS:
            if not line.startswith(arrow, index):
                continue
            call = _CALL.match(line[:index].strip())
            expected = line[index + len(arrow):].strip()
            if call is None or not expected:
                continue
            raw_args = call.group("args").strip()
            try:
                # The trailing comma makes a single argument parse as a 1-tuple,
                # so `f(4)` and `f(4, 5)` come back in the same shape.
                args = ast.literal_eval(f"({raw_args},)") if raw_args else ()
            except (ValueError, SyntaxError):
                continue
            if isinstance(args, tuple):
                return call.group("entry"), args, expected
    return None


def _sections(body: str) -> dict[str, list[str]]:
    """Bullet lines per section, in stated order, each bullet in exactly one.

    One pass with mutually exclusive assignment. A heading is classified by the
    *first* section phrase it names, so "Acceptance criteria and domain laws"
    is criteria — arguably arbitrary, but unambiguously one thing, which is what
    matters. Any other heading closes the current section, so a bullet under
    "Notes" belongs to nothing.
    """
    collected: dict[str, list[str]] = {heading: [] for heading in _HEADINGS}
    current: list[str] | None = None
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.lstrip("#").strip().rstrip(":").lower()
        is_heading = line.startswith("#") or lowered in _HEADINGS
        if is_heading:
            current = next(
                (collected[h] for h in _HEADINGS if h in lowered), None
            )
            continue
        if current is not None and line.startswith(("-", "*")):
            text = line[1:].strip()
            if text:
                current.append(text)
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
    sections = _sections(body)
    return sections[ACCEPTANCE_HEADING], sections[LAWS_HEADING]


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
    usable = [e for e in examples if _is_emittable(e)]
    if not usable:
        return ""
    out: list[str] = [
        "\n\n# --- worked examples, transcribed verbatim from the spec ---------------\n"
        "# Values here were written by a human; only the assertion shape is generated.\n"
    ]
    # Grouped **per entry point**, not per spec. A single test asserting
    # `first_entry(*args) == expected` over every example makes a claim no human
    # wrote the moment a spec mentions two functions: `from_roman("IV") -> 4`
    # would be emitted as `to_roman("IV") == 4`, which then permanently rejects
    # correct code once promoted.
    for entry in sorted({e.entry for e in usable}):
        returns = [e for e in usable if e.entry == entry and not e.is_rejection]
        rejects = [e for e in usable if e.entry == entry and e.is_rejection]
        suffix = _identifier(entry, prefix="", index=0).strip("_0") or entry
        if returns:
            cases = ",\n".join(f"    ({e.args!r}, {e.expected!r})" for e in returns)
            out.append(
                f'\n\n@pytest.mark.parametrize(("args", "expected"), [\n{cases},\n])\n'
                f"def test_worked_examples_{suffix}(args: tuple, expected: object) -> None:\n"
                f'    """Each case is stated in the spec."""\n'
                f"    assert {entry}(*args) == expected\n"
            )
        if rejects:
            cases = ",\n".join(f"    ({e.args!r}, {e.raises})" for e in rejects)
            out.append(
                f'\n\n@pytest.mark.parametrize(("args", "expected_error"), [\n{cases},\n])\n'
                f"def test_worked_rejections_{suffix}("
                "args: tuple, expected_error: type) -> None:\n"
                f'    """Each rejection is stated in the spec."""\n'
                f"    with pytest.raises(expected_error):\n"
                f"        {entry}(*args)\n"
            )
    return "".join(out)


def _is_emittable(example: WorkedExample) -> bool:
    """Can this example become generated source that actually imports?

    Two ways a transcribed value cannot, both of which produce a ``tests.py``
    that ``compile()`` accepts and that then dies at *collection*:

    - **A repr that is not a literal.** ``1e400`` evaluates to ``inf``, whose
      repr is the bare name ``inf`` — undefined in the generated module.
    - **A non-builtin exception name.** ``raises MyDomainError`` names something
      that lives in an implementation nobody has written yet, so there is
      nothing to import it from at draft time.

    Both are reported by :func:`untranscribed_examples` rather than dropped in
    silence, because a rejection case that vanishes leaves an exam that looks
    complete and tests less than the human asked for.
    """
    if example.is_rejection:
        exc = getattr(builtins, str(example.raises), None)
        return isinstance(exc, type) and issubclass(exc, BaseException)
    return _round_trips(example.args) and _round_trips(example.expected)


def _round_trips(value: object) -> bool:
    """Does ``repr(value)`` parse back to ``value`` as a literal?"""
    try:
        return bool(ast.literal_eval(repr(value)) == value)
    except (ValueError, SyntaxError):
        return False


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
            # repr, not raw interpolation: the criterion is prose from a document
            # store, and stage() now *executes* the drafted tests. A bullet
            # containing a closing triple-quote would otherwise end the docstring
            # and run whatever followed it. repr escapes quotes and backslashes,
            # so the worst a hostile bullet achieves is an ugly docstring.
            f"    {criterion!r}\n"
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
                f"    {law!r}\n"          # repr for the same reason as criteria above
                '    raise NotImplementedError("state this law as a predicate")\n'
            )
        files["oracle.py"] = "".join(oracle)

    examples = parse_worked_examples(body)
    emitted = [e for e in examples if _is_emittable(e)]
    dropped = untranscribed_examples(body)
    files["README.md"] = (
        f"# `{name}` contract\n\n"
        f"Drafted from **{spec_ref}** by the contract-author step, and **not yet "
        "reviewed by a human**.\n\n"
        f"- {len(criteria)} acceptance criteria → `tests.py` (stubs)\n"
        f"- {len(laws)} domain laws → `oracle.py` (stubs)\n"
        f"- {len(emitted)} worked examples → `tests.py` (**real assertions**)\n"
        + (f"- {len(dropped)} worked example(s) could NOT be transcribed:\n"
           + "".join(f"  - {d}\n" for d in dropped) if dropped else "")
        + f"\nEntry point: `{entry}`. Public API: {', '.join(public_api)}.\n\n"
        "**The criteria and law bodies raise `NotImplementedError`** — the system "
        "cannot invent domain laws, so it drafts the shape and leaves the meaning "
        "to you. **The worked examples are real assertions**, transcribed verbatim "
        "from values you wrote; read them, because they are the part that can be "
        "wrong without looking wrong.\n\n"
        "Nothing here is read by any gate until you approve promotion into "
        "`specs/`.\n"
    )
    return ContractDraft(name=name, spec_ref=spec_ref, files=files)


def pending(staging_dir: Path | None = None) -> tuple[str, ...]:
    """Contract names currently staged and awaiting a human. Sorted, for review."""
    root = staging_dir or CONTRACT_STAGING_DIR
    if not root.is_dir():
        return ()
    return tuple(sorted(p.name for p in root.iterdir() if p.is_dir()))
