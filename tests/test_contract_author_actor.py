"""The ContractAuthor role actor (OMNI-21). Own module for its own Ray cluster.

Thin by design — the drafting logic and the approval gate are pure and tested in
tests/test_contract_author.py — so this asks only the questions that need a real
actor: does it read a spec through the Workspace, stage the result, record
provenance, and stop.
"""

import pathlib

import pytest

ray = pytest.importorskip("ray")

from sis import contract_author, org  # noqa: E402
from sis.paths import SPECS_DIR  # noqa: E402
from sis.roles import ContractAuthor  # noqa: E402

SPEC_BODY = """\
# Supply route planner

## Acceptance criteria

- A two-supplier scenario returns the known optimal plan
- An infeasible demand flags unmet demand rather than raising

## Domain laws

- Cargo is conserved: everything shipped equals demand met
"""


@pytest.fixture(scope="module")
def handles():  # type: ignore[no-untyped-def]
    h = org.bootstrap()
    yield h
    ray.shutdown()


@pytest.fixture
def author(handles):  # type: ignore[no-untyped-def]
    actor = ContractAuthor.remote()  # type: ignore[attr-defined]
    # Block until __init__ has run. Actor construction is asynchronous, so a
    # test that only *queries* the SelfModel registry (rather than calling a
    # method, which would force initialisation anyway) can otherwise look before
    # the actor has registered itself. Serial runs were slow enough to hide it;
    # under `-n auto` it fails.
    ray.get(actor.__ray_ready__.remote())
    return actor


def _page(handles, body: str = SPEC_BODY):  # type: ignore[no-untyped-def]
    workspace = handles["Workspace"]
    return ray.get(workspace.create_page.remote("SD", "Supply route planner", body))


def test_the_author_drafts_from_a_spec_and_stages_it(author, handles) -> None:  # type: ignore[no-untyped-def]
    page = _page(handles)
    result = ray.get(author.draft.remote(
        page.id, name="_actor_probe", entry="plan", public_api=("plan",),
    ))
    staged = pathlib.Path(result["staged_at"])
    try:
        assert result["contract"] == "_actor_probe"
        assert result["spec_ref"] == page.id
        assert "tests.py" in result["files"]
        # The law the spec stated arrives in the draft with somewhere to answer it.
        assert "Cargo is conserved" in (staged / "oracle.py").read_text(encoding="utf-8")
    finally:
        if staged.exists():
            for child in sorted(staged.rglob("*"), reverse=True):
                child.unlink() if child.is_file() else child.rmdir()
            staged.rmdir()


def test_the_author_never_promotes(author, handles) -> None:  # type: ignore[no-untyped-def]
    """It surfaces the decision; a human makes it.

    The same shape as DevOps.observe_merge applying a human's merge rather than
    performing one. If this actor could promote, the separation between author
    and implementer would be a naming convention rather than a guarantee.
    """
    before = sorted(p.name for p in SPECS_DIR.iterdir())
    page = _page(handles)
    result = ray.get(author.draft.remote(
        page.id, name="_actor_probe2", entry="plan", public_api=("plan",),
    ))
    staged = pathlib.Path(result["staged_at"])
    try:
        assert result["promoted"] is False
        assert sorted(p.name for p in SPECS_DIR.iterdir()) == before
        assert not hasattr(ContractAuthor, "promote")
    finally:
        if staged.exists():
            for child in sorted(staged.rglob("*"), reverse=True):
                child.unlink() if child.is_file() else child.rmdir()
            staged.rmdir()


def test_drafting_is_recorded_in_the_provenance_graph(author, handles) -> None:  # type: ignore[no-untyped-def]
    # spec → contract → ... is the chain a reviewer walks backwards from a
    # rejected candidate, so the drafting step has to appear in it.
    page = _page(handles)
    result = ray.get(author.draft.remote(
        page.id, name="_actor_probe3", entry="plan", public_api=("plan",),
    ))
    staged = pathlib.Path(result["staged_at"])
    try:
        provenance = ray.get(handles["SelfModel"].provenance.remote())
        drafted = [e for e in provenance if e["kind"] == "contract_drafted"]
        assert drafted, "drafting a contract left no provenance"
        assert drafted[-1]["ref"] == page.id
        assert drafted[-1]["detail"]["awaiting"] == "human approval"
    finally:
        if staged.exists():
            for child in sorted(staged.rglob("*"), reverse=True):
                child.unlink() if child.is_file() else child.rmdir()
            staged.rmdir()


def test_the_author_is_registered_as_its_own_role(author, handles) -> None:  # type: ignore[no-untyped-def]
    # A different actor from the SWE that has to pass the exam. That separation
    # *is* the anti-gaming property, so it should be visible in the org chart.
    registry = ray.get(handles["SelfModel"].registry.remote())
    entry = next((a for a in registry if a["name"] == "ContractAuthor"), None)
    assert entry is not None
    assert entry["role"] == "ContractAuthor"


def test_the_pure_gate_still_refuses_what_the_actor_staged(author, handles) -> None:  # type: ignore[no-untyped-def]
    """The actor's output is not a special case: promoting it needs approval too."""
    page = _page(handles)
    result = ray.get(author.draft.remote(
        page.id, name="_actor_probe4", entry="plan", public_api=("plan",),
    ))
    staged_dir = pathlib.Path(result["staged_at"])
    try:
        staged = contract_author.StagedContract(
            name=result["contract"], spec_ref=result["spec_ref"],
            directory=staged_dir, files=tuple(result["files"]),
        )
        with pytest.raises(Exception, match="requires human approval"):
            contract_author.promote(staged)
    finally:
        if staged_dir.exists():
            for child in sorted(staged_dir.rglob("*"), reverse=True):
                child.unlink() if child.is_file() else child.rmdir()
            staged_dir.rmdir()


def test_the_discriminates_field_is_a_bool_not_a_sentence(author, handles) -> None:  # type: ignore[no-untyped-def]
    """Regression: it used to carry summary()'s prose.

    A caller writing the natural `if result["discriminates"]:` took the success
    branch for "DOES NOT REJECT A NULL IMPLEMENTATION", because a non-empty
    string is truthy — so the one fact the field exists to surface was the one a
    truthiness test could not see. None means "not checked", a third state.
    """
    page = _page(handles)
    result = ray.get(author.draft.remote(
        page.id, name="_actor_probe5", entry="plan", public_api=("plan",),
    ))
    staged = pathlib.Path(result["staged_at"])
    try:
        assert result["discriminates"] in (True, False, None)
        # The prose is still available, under its own key.
        assert isinstance(result["discrimination_detail"], str)
    finally:
        if staged.exists():
            for child in sorted(staged.rglob("*"), reverse=True):
                child.unlink() if child.is_file() else child.rmdir()
            staged.rmdir()
