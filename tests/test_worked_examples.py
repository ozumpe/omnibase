"""Worked-example transcription and the discrimination check (OMNI-26).

Two claims to hold down. Transcription must never *infer* — every value in a
generated assertion has to be one a human wrote — and the discrimination check
must catch the failure a reviewer is least likely to notice, which is an exam
that reads as authored and asserts nothing.
"""

from __future__ import annotations

import pathlib

import pytest

from sis.contract_author import (
    ContractDraft,
    check_discrimination,
    null_implementation,
    parse_spec,
    parse_worked_examples,
    skeleton_from_spec,
    stage,
    untranscribed_examples,
)

SPEC = """\
# Roman numerals

## Acceptance criteria

- Converts a value in range to its canonical numeral

## Worked examples

- `to_roman(4) -> "IV"`
- to_roman(1987) -> "MCMLXXXVII"
- to_roman(0) -> raises ValueError
- to_roman(4000) -> raises ValueError

## Domain laws

- from_roman inverts to_roman across the defined range
"""

ROMAN_OK = '''\
"""Roman numerals."""

_VALUES = [
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
    (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
]


def to_roman(value: int) -> str:
    """Canonical numeral for 1..3999."""
    if not 1 <= value <= 3999:
        raise ValueError(str(value))
    out: list[str] = []
    remaining = value
    for amount, symbol in _VALUES:
        while remaining >= amount:
            out.append(symbol)
            remaining -= amount
    return "".join(out)
'''


# --- transcription --------------------------------------------------------


def test_worked_examples_are_read_verbatim() -> None:
    examples = parse_worked_examples(SPEC)
    returns = [e for e in examples if not e.is_rejection]
    assert [(e.args, e.expected) for e in returns] == [
        ((4,), "IV"),
        ((1987,), "MCMLXXXVII"),
    ]


def test_rejection_cases_are_transcribed_too() -> None:
    rejects = [e for e in parse_worked_examples(SPEC) if e.is_rejection]
    assert [(e.args, e.raises) for e in rejects] == [((0,), "ValueError"), ((4000,), "ValueError")]


def test_backticks_and_arrow_variants_are_tolerated() -> None:
    # A spec is prose a human formats however they like; the parser should not
    # make them learn a syntax to be understood.
    body = (
        "## Worked examples\n\n"
        "- `f(1) -> 2`\n"
        "- f(2) → 4\n"
        "- f(3) = 6\n"
    )
    assert [e.expected for e in parse_worked_examples(body)] == [2, 4, 6]


def test_multi_argument_and_zero_argument_calls_parse() -> None:
    body = "## Worked examples\n\n- add(1, 2) -> 3\n- now() -> 0\n"
    examples = parse_worked_examples(body)
    assert examples[0].args == (1, 2)
    assert examples[1].args == ()


def test_values_are_literals_not_evaluated_code() -> None:
    """A spec page is prose from a document store, so it must never be executed.

    In a real deployment the intake channel carries prose an outside author can
    influence. `eval` there would make the page an arbitrary code path into the
    process that decides what "correct" means — so anything not a literal is
    skipped, not run.
    """
    body = (
        "## Worked examples\n\n"
        "- f(1) -> __import__('os').system('echo pwned')\n"
        "- f(2) -> open('/etc/passwd').read()\n"
        "- f(3) -> 9\n"
    )
    examples = parse_worked_examples(body)
    assert [(e.args, e.expected) for e in examples] == [((3,), 9)]


def test_a_malformed_example_is_skipped_not_fatal() -> None:
    # One mistyped bullet in a long spec should not make the page undraftable.
    # What it must not do is silently weaken an assertion, and skipping cannot.
    body = "## Worked examples\n\n- this is not an example at all\n- f(1) -> 2\n"
    assert [e.expected for e in parse_worked_examples(body)] == [2]


def test_examples_under_other_headings_are_ignored() -> None:
    body = "## Notes\n\n- f(1) -> 2\n\n## Worked examples\n\n- g(3) -> 4\n"
    assert [e.entry for e in parse_worked_examples(body)] == ["g"]


# --- what gets generated --------------------------------------------------


def _draft() -> ContractDraft:
    return skeleton_from_spec(
        name="roman_probe", spec_ref="CONF-1", body=SPEC,
        entry="to_roman", public_api=("to_roman",),
    )


def test_transcribed_examples_become_real_assertions() -> None:
    tests = _draft().files["tests.py"]
    assert "assert to_roman(*args) == expected" in tests
    assert "pytest.raises(expected_error)" in tests
    # The human's own values, verbatim.
    assert "'IV'" in tests or '"IV"' in tests
    assert "MCMLXXXVII" in tests


def test_the_generated_test_module_is_valid_python() -> None:
    compile(_draft().files["tests.py"], "tests.py", "exec")


def test_criteria_without_examples_are_still_stubs() -> None:
    # Only transcription earns a real body. A criterion stated in prose has no
    # values to transcribe, so it stays a stub rather than becoming a guess.
    tests = _draft().files["tests.py"]
    assert "NotImplementedError" in tests


# --- the discrimination check ---------------------------------------------


def test_a_null_implementation_exports_the_names_and_promises_nothing() -> None:
    source = null_implementation(("to_roman", "from_roman"))
    assert "def to_roman(" in source and "def from_roman(" in source
    compile(source, "null.py", "exec")


def test_transcribed_examples_reject_a_null_implementation() -> None:
    """The point of the check: real assertions fail against a module doing nothing."""
    verdict = check_discrimination(_draft(), public_api=("to_roman",))
    assert verdict.checked
    assert verdict.discriminates, verdict.detail


def test_an_exam_of_only_stubs_is_reported_as_asserting_nothing() -> None:
    """A stub suite "fails" against a null impl too — for the wrong reason.

    Counting that as discrimination would make the check pass exactly when it is
    least informative, so a draft with no assertions is called out instead.
    """
    body = "## Acceptance criteria\n\n- It does the thing\n"
    draft = skeleton_from_spec(
        name="stubs_only", spec_ref="CONF-1", body=body,
        entry="f", public_api=("f",),
    )
    verdict = check_discrimination(draft, public_api=("f",))
    assert not verdict.discriminates
    assert "stub" in verdict.detail


def test_a_vacuous_exam_is_caught() -> None:
    """The failure a reviewer skimming plausible-looking code will not notice."""
    vacuous = ContractDraft(
        name="vacuous", spec_ref="CONF-1",
        files={"tests.py": "def test_it_works() -> None:\n    assert True\n"},
    )
    verdict = check_discrimination(vacuous, public_api=("f",))
    assert verdict.checked
    assert not verdict.discriminates
    assert "returns\nNone" in verdict.detail or "None for everything" in verdict.detail


def test_a_draft_without_tests_reports_that_it_could_not_be_checked() -> None:
    # "Not checked" and "checked and vacuous" are different facts, and a reviewer
    # needs to be able to tell them apart.
    draft = ContractDraft(name="x", spec_ref="CONF-1", files={"README.md": "# x\n"})
    verdict = check_discrimination(draft, public_api=("f",))
    assert not verdict.checked
    assert "no tests.py" in verdict.detail


# --- staging surfaces the verdict -----------------------------------------


def test_staging_records_the_verdict(tmp_path: pathlib.Path) -> None:
    staged = stage(_draft(), staging_dir=tmp_path, public_api=("to_roman",))
    assert staged.discrimination is not None
    assert staged.discrimination.discriminates


def test_the_verdict_is_written_where_a_reviewer_will_see_it(
    tmp_path: pathlib.Path,
) -> None:
    staged = stage(_draft(), staging_dir=tmp_path, public_api=("to_roman",))
    readme = (staged.directory / "README.md").read_text(encoding="utf-8")
    assert "discriminate" in readme.lower()
    assert "null implementation" in readme.lower()


def test_a_vacuous_draft_says_so_loudly_in_its_readme(tmp_path: pathlib.Path) -> None:
    vacuous = ContractDraft(
        name="vacuous", spec_ref="CONF-1",
        files={
            "tests.py": "def test_it_works() -> None:\n    assert True\n",
            "README.md": "# vacuous\n",
        },
    )
    staged = stage(vacuous, staging_dir=tmp_path, public_api=("f",))
    readme = (staged.directory / "README.md").read_text(encoding="utf-8")
    assert "DOES NOT REJECT A NULL IMPLEMENTATION" in readme


def test_staging_without_a_public_api_skips_the_check(tmp_path: pathlib.Path) -> None:
    # Nothing to build a null implementation from; better to report nothing than
    # to report a verdict derived from an empty module.
    staged = stage(_draft(), staging_dir=tmp_path)
    assert staged.discrimination is None


# --- the transcribed exam is a real exam ----------------------------------


def test_the_generated_exam_passes_against_a_correct_implementation(
    tmp_path: pathlib.Path,
) -> None:
    """The other half of discrimination: it must not reject correct code either.

    A test suite that fails against everything discriminates in the letter and
    is useless in fact.
    """
    from sis import gauntlet

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    # Only the transcribed half: the prose criteria are still stubs by design.
    source = _draft().files["tests.py"]
    transcribed = source[source.index("# --- worked examples"):]
    header = "import pytest\nfrom target import to_roman\n"
    (tests_dir / "test_target.py").write_text(header + transcribed, encoding="utf-8")
    (tmp_path / "target.py").write_text(ROMAN_OK, encoding="utf-8")
    (tmp_path / "sitecustomize.py").write_text(gauntlet._NETWORK_GUARD, encoding="utf-8")

    env = gauntlet._sandbox_env(home=str(tmp_path), pythonpath=str(tmp_path))
    result = gauntlet._run(
        [gauntlet._PY, "-m", "pytest", str(tests_dir), "-q"], str(tmp_path), env
    )
    assert result.returncode == 0, result.stdout


# --- regressions from the OMNI-26 review ----------------------------------
#
# Every finding gets a test that fails against the original implementation.
# The through-line worth remembering: the first version decided "the exam
# discriminates" from pytest's *exit code*, which cannot tell a real assertion
# failing from an unwritten stub raising from the module not importing. So the
# one check that existed to catch a vacuous exam issued its strongest pass for
# exams that asserted nothing at all.


def test_a_stub_plus_a_null_passing_example_does_not_count_as_discriminating() -> None:
    """The original defect, in its smallest form.

    `f(1) -> None` passes against a null implementation, so this exam asserts
    nothing about behaviour — but the criterion stub raises NotImplementedError,
    which made pytest exit non-zero and the old check report "discriminates".
    """
    body = "## Acceptance criteria\n\n- crit one\n\n## Worked examples\n\n- f(1) -> None\n"
    draft = skeleton_from_spec(
        name="p", spec_ref="C-1", body=body, entry="f", public_api=("f",))
    verdict = check_discrimination(draft, public_api=("f",))
    assert verdict.checked
    assert not verdict.discriminates
    assert "passed against a module" in verdict.detail


def test_prose_containing_the_word_assert_does_not_fake_an_assertion() -> None:
    # The old vacuity guard was `tests_source.count("assert ")`, a raw substring
    # scan over the whole file including the copied criterion docstring.
    body = "## Acceptance criteria\n\n- The parser must assert the header is present\n"
    draft = skeleton_from_spec(
        name="p", spec_ref="C-1", body=body, entry="f", public_api=("f",))
    verdict = check_discrimination(draft, public_api=("f",))
    assert not verdict.discriminates
    assert "still a stub" in verdict.detail


def test_a_draft_that_cannot_be_collected_is_reported_as_unchecked() -> None:
    """"Could not run" is not "rejects a null implementation".

    A module that fails to import exits non-zero, which the old check read as
    the strongest possible pass.
    """
    broken = ContractDraft(
        name="broken", spec_ref="C-1",
        files={"tests.py": "import nonexistent_module_xyz\n\ndef test_a() -> None:\n    pass\n"},
    )
    verdict = check_discrimination(broken, public_api=("f",))
    assert not verdict.checked
    assert not verdict.discriminates


def test_a_multi_function_spec_never_cross_asserts() -> None:
    """`from_roman("IV") -> 4` must not become `to_roman("IV") == 4`.

    The old generator hard-coded `returns[0].entry` for every case, so a spec
    naming two functions produced an assertion no human wrote — one that would
    permanently reject correct code once promoted, while also failing against
    the null implementation and so certifying itself as discriminating.
    """
    body = (
        "## Acceptance criteria\n\n- c\n\n## Worked examples\n\n"
        '- to_roman(4) -> "IV"\n- from_roman("IV") -> 4\n'
    )
    tests = skeleton_from_spec(
        name="p", spec_ref="C-1", body=body, entry="to_roman",
        public_api=("to_roman", "from_roman"),
    ).files["tests.py"]
    assert "def test_worked_examples_to_roman" in tests
    assert "def test_worked_examples_from_roman" in tests
    assert "assert to_roman(*args) == expected" in tests
    assert "assert from_roman(*args) == expected" in tests


def test_a_non_builtin_exception_is_not_emitted_and_is_reported() -> None:
    # `raises MyDomainError` names something living in an implementation nobody
    # has written yet, so the generated module cannot import it. Emitting it
    # made every test in the file error at collection.
    body = "## Worked examples\n\n- f(1) -> raises MyDomainError\n"
    assert "MyDomainError" not in skeleton_from_spec(
        name="p", spec_ref="C-1", body="## Acceptance criteria\n\n- c\n" + body,
        entry="f", public_api=("f",)).files["tests.py"]
    assert any("non-builtin exception" in d for d in untranscribed_examples(body))


def test_a_value_with_no_literal_repr_is_not_emitted_and_is_reported() -> None:
    # 1e400 evaluates to inf, whose repr is the bare name `inf` — undefined in
    # the generated module, so it compiled and then died at collection.
    body = "## Worked examples\n\n- f(1) -> 1e400\n"
    assert untranscribed_examples(body)
    assert "no literal repr" in untranscribed_examples(body)[0]


def test_generated_tests_are_importable_not_merely_compilable() -> None:
    """compile() accepted every one of the collection-time failures above.

    Running them is the only check that catches an undefined name in a
    parametrize list, which is where two of these defects lived.
    """
    body = (
        "## Acceptance criteria\n\n- c\n\n## Worked examples\n\n"
        '- f(1) -> 2\n- f(0) -> raises ValueError\n'
    )
    draft = skeleton_from_spec(
        name="p", spec_ref="C-1", body=body, entry="f", public_api=("f",))
    verdict = check_discrimination(draft, public_api=("f",))
    assert verdict.checked, verdict.detail


def test_spec_prose_cannot_execute_at_stage_time(tmp_path: pathlib.Path) -> None:
    """A criterion bullet must not be able to close its docstring and run code.

    stage() executes the drafted tests, and the criterion used to be
    interpolated raw into a triple-quoted docstring — so a spec page, which is
    prose an outside author can influence, was an arbitrary code path into the
    process. The `literal_eval, never eval` guarantee covered the *values* and
    was bypassed entirely through the docstring.
    """
    marker = tmp_path / "pwned"
    body = (
        '## Acceptance criteria\n\n'
        f'- ok"""; __import__("pathlib").Path({str(marker)!r}).write_text("x"); """\n'
    )
    draft = skeleton_from_spec(
        name="evil", spec_ref="C-1", body=body, entry="f", public_api=("f",))
    check_discrimination(draft, public_api=("f",))
    assert not marker.exists()
    # The prose survives as data, just not as code.
    assert "ok" in draft.files["tests.py"]


def test_running_a_draft_takes_the_gauntlets_sandbox_preconditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1: a real proposer's untrusted code requires the docker sandbox.

    check_discrimination executes generated code through the same `_run` the
    gates use, but originally skipped `ensure_sandbox_allows_proposer` — so a
    `SIS_PROPOSER=claude` run executed spec-derived code in the soft sandbox,
    where the host filesystem stays readable. A CLAUDE.md hard rule.
    """
    monkeypatch.setenv("SIS_PROPOSER", "claude")
    monkeypatch.delenv("SIS_SANDBOX", raising=False)
    draft = ContractDraft(
        name="p", spec_ref="C-1", files={"tests.py": "def test_a() -> None:\n    assert 1\n"})
    with pytest.raises(RuntimeError, match="docker"):
        check_discrimination(draft, public_api=("f",))


def test_a_heading_naming_two_sections_populates_exactly_one() -> None:
    # Two independent substring tests put every bullet under "Acceptance
    # criteria and domain laws" into both lists, drafting a test stub *and* a
    # law predicate for each and doubling the README's counts.
    criteria, laws = parse_spec(
        "## Acceptance criteria and domain laws\n\n- Returns a plan\n- Cargo is conserved\n"
    )
    assert len(criteria) == 2
    assert laws == []


def test_individually_quoted_examples_parse() -> None:
    # The common markdown form leaves an interior backtick that end-stripping
    # cannot reach, and the example vanished with no diagnostic.
    assert [e.expected for e in parse_worked_examples(
        '## Worked examples\n\n- `to_roman(4)` -> `"IV"`\n')] == ["IV"]


def test_a_value_containing_an_arrow_and_a_paren_parses() -> None:
    # A greedy `\\((?P<args>.*)\\)` swallowed the paren inside the string.
    assert [e.expected for e in parse_worked_examples(
        '## Worked examples\n\n- f(1) -> "a) -> b"\n')] == ["a) -> b"]


def test_a_rejection_with_a_message_or_a_dotted_name_parses() -> None:
    examples = parse_worked_examples(
        '## Worked examples\n\n- g(0) -> raises ValueError("nope")\n'
        "- h(0) -> raises errs.ValueError\n"
    )
    assert [e.raises for e in examples] == ["ValueError", "ValueError"]


def test_the_readme_describes_what_the_draft_actually_contains(
    tmp_path: pathlib.Path,
) -> None:
    """It used to say "every generated body raises NotImplementedError".

    With transcription that is false, and a reviewer trusting it would approve
    real machine-shaped assertions believing nothing asserted — the exact
    "looks authored, is not" failure this module exists to prevent.
    """
    body = (
        "## Acceptance criteria\n\n- c\n\n## Worked examples\n\n"
        '- f(1) -> 2\n- f(9) -> raises MyDomainError\n'
    )
    draft = skeleton_from_spec(
        name="p", spec_ref="C-1", body=body, entry="f", public_api=("f",))
    readme = draft.files["README.md"]
    assert "real assertions" in readme
    assert "1 worked examples" in readme
    # And it names what it could not transcribe, rather than quietly omitting it.
    assert "could NOT be transcribed" in readme
    assert "non-builtin exception" in readme


def test_the_verdict_survives_promotion(tmp_path: pathlib.Path) -> None:
    # README.md was written by stage() but absent from staged.files, so
    # promote() copied everything except the vacuous-exam warning.
    vacuous = ContractDraft(
        name="_verdict_probe", spec_ref="C-1",
        files={"tests.py": "def test_a() -> None:\n    assert True\n"},
    )
    staged = stage(vacuous, staging_dir=tmp_path, public_api=("f",))
    assert "README.md" in staged.files
    target = staged.target
    try:
        from sis.contract_author import promote

        promote(staged, approved=True)
        assert "DOES NOT REJECT" in (target / "README.md").read_text(encoding="utf-8")
    finally:
        if target.exists():
            for child in sorted(target.rglob("*"), reverse=True):
                child.unlink() if child.is_file() else child.rmdir()
            target.rmdir()
