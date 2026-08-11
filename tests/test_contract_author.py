"""Tests for the contract-author step and the write path into specs/ (OMNI-21).

The load-bearing claim is not that drafting works — it is that drafting can
never *become* an exam without a human. So most of what follows is about the
gate, the inertness of staging, and the fact that no path around either exists.
"""

from __future__ import annotations

import pathlib

import pytest

from sis.contract_author import (
    ContractDraft,
    parse_spec,
    pending,
    promote,
    skeleton_from_spec,
    stage,
)
from sis.paths import CONTRACT_STAGING_DIR, SPECS_DIR
from sis.policy import ChangeTier, classify
from sis.ports import RequiresHumanApproval

SPEC_BODY = """\
# Supply route planner

Some prose a PM wrote that is not a criterion.

## Acceptance criteria

- A two-supplier scenario returns the known optimal plan
- An infeasible demand flags unmet demand rather than raising
- A single-route degenerate case still returns a plan

## Domain laws

- Cargo is conserved: everything shipped equals demand met
- Removing a supplier can never lower the optimal cost

## Notes

- This bullet is under a different heading and must not be collected
"""


def _draft(**overrides: object) -> ContractDraft:
    base: dict[str, object] = {
        "name": "planner",
        "spec_ref": "CONF-1487",
        "files": {"tests.py": "# drafted\n"},
    }
    return ContractDraft(**{**base, **overrides})  # type: ignore[arg-type]


# --- the approval gate ----------------------------------------------------


def test_promotion_without_approval_raises(tmp_path: pathlib.Path) -> None:
    """The only way into specs/ is through a human.

    Same ``RequiresHumanApproval`` shape the adapters use for deleting a Jira
    issue or merging to main — one mechanism, one place to audit.
    """
    staged = stage(_draft(), staging_dir=tmp_path)
    with pytest.raises(RequiresHumanApproval, match="requires human approval"):
        promote(staged)


def test_the_refusal_names_the_contract_and_its_spec(tmp_path: pathlib.Path) -> None:
    # A human being asked to approve an exam needs to know which exam and which
    # spec it claims to implement.
    staged = stage(_draft(), staging_dir=tmp_path)
    with pytest.raises(RequiresHumanApproval) as exc:
        promote(staged)
    assert "planner" in str(exc.value)
    assert "CONF-1487" in str(exc.value)


def test_there_is_no_force_argument() -> None:
    """Approval must be impossible to satisfy accidentally.

    A ``force=`` or a truthy default would make the gate a formality; pinning
    the signature keeps a future convenience flag from quietly becoming one.
    """
    import inspect

    params = inspect.signature(promote).parameters
    assert set(params) == {"staged", "approved"}
    assert params["approved"].default is False
    assert params["approved"].kind is inspect.Parameter.KEYWORD_ONLY


def test_an_approved_promotion_writes_into_specs(tmp_path: pathlib.Path) -> None:
    draft = _draft(name="_promotion_probe", files={"tests.py": "# approved\n"})
    staged = stage(draft, staging_dir=tmp_path)
    target = staged.target
    try:
        assert promote(staged, approved=True) == target
        assert (target / "tests.py").read_text(encoding="utf-8") == "# approved\n"
    finally:
        if target.exists():
            for child in target.rglob("*"):
                if child.is_file():
                    child.unlink()
            target.rmdir()


def test_promoting_something_never_staged_fails_clearly(tmp_path: pathlib.Path) -> None:
    staged = stage(_draft(), staging_dir=tmp_path)
    for child in staged.directory.iterdir():
        child.unlink()
    staged.directory.rmdir()
    with pytest.raises(FileNotFoundError, match="stage it again"):
        promote(staged, approved=True)


# --- staging is inert -----------------------------------------------------


def test_staging_lives_outside_specs() -> None:
    """Nothing under specs/ may be loop-writable, and staging must be.

    If staging were inside specs/ it would be POLICY-FORBIDDEN and the loop
    could not draft at all; if it were readable by a gate, drafting would be
    equivalent to authoring.
    """
    assert not CONTRACT_STAGING_DIR.is_relative_to(SPECS_DIR)
    assert classify(SPECS_DIR) is ChangeTier.FORBIDDEN
    assert classify(CONTRACT_STAGING_DIR) is not ChangeTier.FORBIDDEN


def test_the_approval_gate_itself_is_guardrail_code() -> None:
    # A loop able to edit this module could approve its own contract.
    assert classify("sis/contract_author.py") is ChangeTier.FORBIDDEN


def test_no_gate_reads_the_staging_directory() -> None:
    """Staging is inert by construction, and that is what makes drafting safe.

    Checked against the gauntlet's source rather than by observation: a gate
    that started copying staged files in would pass every behavioural test right
    up until it promoted an unreviewed exam.
    """
    gauntlet_src = (pathlib.Path(__file__).parent.parent / "sis/gauntlet.py").read_text(
        encoding="utf-8"
    )
    assert "CONTRACT_STAGING" not in gauntlet_src
    assert "contract_staging" not in gauntlet_src


def test_restaging_replaces_the_previous_draft(tmp_path: pathlib.Path) -> None:
    # A second attempt after review feedback must not leave the first lying
    # around to be approved by mistake.
    stage(_draft(files={"tests.py": "# first\n", "oracle.py": "# gone\n"}),
          staging_dir=tmp_path)
    staged = stage(_draft(files={"tests.py": "# second\n"}), staging_dir=tmp_path)
    assert (staged.directory / "tests.py").read_text(encoding="utf-8") == "# second\n"
    assert not (staged.directory / "oracle.py").exists()


def test_pending_lists_what_awaits_a_human(tmp_path: pathlib.Path) -> None:
    assert pending(tmp_path) == ()
    stage(_draft(name="beta"), staging_dir=tmp_path)
    stage(_draft(name="alpha"), staging_dir=tmp_path)
    assert pending(tmp_path) == ("alpha", "beta")


# --- a draft cannot name a path outside its own contract ------------------


@pytest.mark.parametrize(
    "relative",
    [
        "../escape.py",
        "../../sis/gauntlet.py",
        "nested/../../escape.py",
        "/absolute.py",
    ],
)
def test_a_draft_cannot_escape_its_own_directory(relative: str) -> None:
    # Checked on the resolved path, not by looking for ".." in the string:
    # a/../../b and a symlinked segment reduce to the same question.
    with pytest.raises(ValueError, match="escapes|absolute"):
        _draft(files={relative: "x"})


def test_a_draft_cannot_write_a_file_no_gate_would_read() -> None:
    # Not a security boundary — promotion is human-approved — but a typo'd
    # suffix that lands somewhere unread is a silent failure worth making loud.
    with pytest.raises(ValueError, match="suffix"):
        _draft(files={"tests.txt": "x"})


@pytest.mark.parametrize("name", ["", "a/b", ".hidden"])
def test_an_unusable_contract_name_is_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="single directory name"):
        _draft(name=name)


def test_a_draft_must_cite_the_spec_it_came_from() -> None:
    # Provenance roots at the spec page; a contract nobody can trace to a spec
    # cannot be reviewed against one.
    with pytest.raises(ValueError, match="no spec_ref"):
        _draft(spec_ref="")


def test_an_empty_draft_is_rejected() -> None:
    with pytest.raises(ValueError, match="drafts no files"):
        _draft(files={})


# --- reading the spec -----------------------------------------------------


def test_criteria_and_laws_are_read_from_their_own_headings() -> None:
    criteria, laws = parse_spec(SPEC_BODY)
    assert len(criteria) == 3
    assert criteria[0].startswith("A two-supplier scenario")
    assert len(laws) == 2
    assert laws[0].startswith("Cargo is conserved")


def test_bullets_under_other_headings_are_not_collected() -> None:
    # A spec is prose; sweeping up every bullet would put a PM's aside into the
    # exam.
    _, laws = parse_spec(SPEC_BODY)
    assert not any("must not be collected" in law for law in laws)


def test_the_order_a_human_wrote_them_in_is_preserved() -> None:
    # The first criterion is usually the central one; reordering loses that.
    criteria, _ = parse_spec(SPEC_BODY)
    assert "infeasible demand" in criteria[1]


# --- the skeleton ---------------------------------------------------------


def test_a_skeleton_carries_every_stated_criterion_and_law() -> None:
    draft = skeleton_from_spec(
        name="planner", spec_ref="CONF-1487", body=SPEC_BODY,
        entry="plan", public_api=("plan", "Plan"),
    )
    tests = draft.files["tests.py"]
    oracle = draft.files["oracle.py"]
    # Each human sentence arrives with somewhere to put the answer.
    assert "A two-supplier scenario returns the known optimal plan" in tests
    assert "Cargo is conserved" in oracle
    assert tests.count("def test_") == 3
    assert oracle.count("def law_") == 2


def test_every_generated_body_fails_until_a_human_writes_it() -> None:
    """The system cannot invent domain laws, and must not look as if it did.

    A skeleton that guessed at bodies would produce an exam that reads as
    authored and checks nothing — strictly worse than an obviously unfinished
    one, because an unfinished stub cannot be approved by accident.
    """
    draft = skeleton_from_spec(
        name="planner", spec_ref="CONF-1487", body=SPEC_BODY,
        entry="plan", public_api=("plan",),
    )
    for name, content in draft.files.items():
        if name.endswith(".py"):
            assert "NotImplementedError" in content
            assert "assert True" not in content


def test_a_skeleton_says_loudly_that_no_human_has_read_it() -> None:
    draft = skeleton_from_spec(
        name="planner", spec_ref="CONF-1487", body=SPEC_BODY,
        entry="plan", public_api=("plan",),
    )
    assert "NOT YET" in draft.files["tests.py"]
    assert "not yet" in draft.files["README.md"].lower()


def test_a_spec_with_no_acceptance_criteria_is_refused() -> None:
    # There would be nothing to verify against, and a contract that verifies
    # nothing passes everything.
    with pytest.raises(ValueError, match="no acceptance criteria"):
        skeleton_from_spec(
            name="x", spec_ref="CONF-1", body="# Title\n\nprose only\n",
            entry="f", public_api=("f",),
        )


def test_a_spec_stating_no_laws_still_drafts_its_acceptance_tests() -> None:
    # Laws are the ideal, criteria are the floor; a domain that has not yet
    # articulated its laws should not be blocked from having a contract.
    body = "## Acceptance criteria\n\n- It does the thing\n"
    draft = skeleton_from_spec(
        name="x", spec_ref="CONF-1", body=body, entry="f", public_api=("f",),
    )
    assert "tests.py" in draft.files
    assert "oracle.py" not in draft.files


def test_generated_identifiers_are_valid_python() -> None:
    body = (
        "## Acceptance criteria\n\n"
        "- 100% of requests: return a plan (even when infeasible!)\n"
    )
    draft = skeleton_from_spec(
        name="x", spec_ref="CONF-1", body=body, entry="f", public_api=("f",),
    )
    compile(draft.files["tests.py"], "tests.py", "exec")


def test_a_drafted_skeleton_stages_without_touching_specs(
    tmp_path: pathlib.Path,
) -> None:
    before = sorted(p.name for p in SPECS_DIR.iterdir())
    draft = skeleton_from_spec(
        name="planner", spec_ref="CONF-1487", body=SPEC_BODY,
        entry="plan", public_api=("plan",),
    )
    staged = stage(draft, staging_dir=tmp_path)
    assert staged.directory.is_dir()
    assert sorted(p.name for p in SPECS_DIR.iterdir()) == before
