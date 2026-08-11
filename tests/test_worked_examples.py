"""Worked-example transcription and the discrimination check (OMNI-26).

Two claims to hold down. Transcription must never *infer* — every value in a
generated assertion has to be one a human wrote — and the discrimination check
must catch the failure a reviewer is least likely to notice, which is an exam
that reads as authored and asserts nothing.
"""

from __future__ import annotations

import pathlib

from sis.contract_author import (
    ContractDraft,
    check_discrimination,
    null_implementation,
    parse_worked_examples,
    skeleton_from_spec,
    stage,
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
