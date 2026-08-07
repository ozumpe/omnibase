"""Tests for the proposer (stub default + Claude dispatch, no network)."""

from dataclasses import replace

import pytest

from sis import proposer
from sis.contract import SUM_OF_DIVISORS, default_contract
from sis.paths import OPTIMISED_CANDIDATE_PATH


def test_defaults_to_stub(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("SIS_PROPOSER", raising=False)
    result = proposer.propose("def sum_of_divisors(n): ...", 0.001)
    assert result == OPTIMISED_CANDIDATE_PATH.read_text(encoding="utf-8")


def test_stub_reads_the_contracts_own_candidate() -> None:
    # The stub answer is per-contract, not one hardcoded file: a second target
    # needs its own canned candidate, or the stub would hand every contract
    # sum_of_divisors' answer and fail the interface gate in a confusing way.
    assert SUM_OF_DIVISORS.stub_candidate_path == "runtime/candidates/optimised_target.py"
    assert proposer.propose("x", 0.1, contract=SUM_OF_DIVISORS) == (
        OPTIMISED_CANDIDATE_PATH.read_text(encoding="utf-8"))


def test_stub_without_a_canned_candidate_fails_loudly() -> None:
    # Fail closed and say why. Silently falling back to another contract's
    # answer would surface much later as a mystifying interface-gate rejection.
    no_stub = replace(default_contract(), name="sortish", stub_candidate_path=None)
    with pytest.raises(RuntimeError, match="no canned candidate"):
        proposer.propose("x", 0.1, contract=no_stub)


def test_non_stub_mode_dispatches_to_llm(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Any non-stub SIS_PROPOSER routes to _llm_proposal — stub it so no API call.
    monkeypatch.setenv("SIS_PROPOSER", "claude")
    captured: dict[str, object] = {}

    def fake(source: str, baseline: float, spec: object) -> str:
        captured["source"] = source
        captured["baseline"] = baseline
        captured["contract"] = spec
        return "def sum_of_divisors(n: int) -> int: return n\n"

    monkeypatch.setattr(proposer, "_llm_proposal", fake)
    out = proposer.propose("CURRENT", 0.5)
    assert out.startswith("def sum_of_divisors")
    assert captured["source"] == "CURRENT"
    assert captured["baseline"] == 0.5
    assert captured["contract"] == default_contract()  # falls back, not None


# --- the contract-derived prompt (OMNI-6) ---------------------------------


def test_system_prompt_names_no_target() -> None:
    # The whole L5 bug in one assertion: the system prompt used to hardcode
    # "preserve sum_of_divisors(n: int) -> int", so pointing the loop at any
    # other target instructed the model to write the WRONG function. It must
    # also stay contract-free to remain byte-frozen and prompt-cacheable.
    assert "sum_of_divisors" not in proposer._SYSTEM_PROMPT


def test_user_prompt_carries_the_contracts_interface() -> None:
    spec = default_contract()
    prompt = proposer._user_prompt("CURRENT SOURCE", 0.25, spec)

    # The entry point and its real signature, derived from the oracle rather
    # than restated in a field that could drift from it.
    assert "sum_of_divisors(n: int) -> int" in prompt
    # The trusted reference source: ground truth for "identical results".
    assert "def reference(n: int) -> int:" in prompt
    # The acceptance tests verbatim -- this is how the model learns it must also
    # provide benchmark(), which is required by THIS contract's tests and is not
    # a universal rule the engine could state on its own.
    assert "def test_sum_of_divisors_basic()" in prompt
    assert "target.benchmark(" in prompt
    # The source to beat and the margin it must clear.
    assert "CURRENT SOURCE" in prompt
    assert "0.250000s" in prompt
    assert "90%" in prompt  # max_latency_ratio


def test_user_prompt_follows_a_different_contract() -> None:
    # Same code path, different contract -> different required interface. This
    # is the property that unblocks a second target (OMNI-7).
    spec = replace(default_contract(), name="other", entry="totally_different")
    prompt = proposer._user_prompt("SRC", 0.1, spec)
    assert "'other'" in prompt
    assert "totally_different(n: int) -> int" in prompt


def test_user_prompt_reflects_a_stricter_margin() -> None:
    spec = replace(default_contract(), max_latency_ratio=0.5)
    assert "50%" in proposer._user_prompt("SRC", 0.1, spec)


def test_extract_code_strips_fences() -> None:
    fenced = "Here is the code:\n```python\ndef f(x: int) -> int:\n    return x\n```\nDone."
    assert proposer._extract_code(fenced) == "def f(x: int) -> int:\n    return x\n"


def test_extract_code_without_fences() -> None:
    plain = "def f(x: int) -> int:\n    return x"
    assert proposer._extract_code(plain) == "def f(x: int) -> int:\n    return x\n"
