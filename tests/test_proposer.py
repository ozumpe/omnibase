"""Tests for the proposer (stub default + Claude dispatch, no network)."""

from sis import proposer
from sis.paths import OPTIMISED_CANDIDATE_PATH


def test_defaults_to_stub(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("SIS_PROPOSER", raising=False)
    result = proposer.propose("def sum_of_divisors(n): ...", 0.001)
    assert result == OPTIMISED_CANDIDATE_PATH.read_text(encoding="utf-8")


def test_claude_mode_dispatches(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # SIS_PROPOSER=claude routes to _claude_proposal — stub it so no API call.
    monkeypatch.setenv("SIS_PROPOSER", "claude")
    captured: dict[str, object] = {}

    def fake(source: str, baseline: float) -> str:
        captured["source"] = source
        captured["baseline"] = baseline
        return "def sum_of_divisors(n: int) -> int: return n\n"

    monkeypatch.setattr(proposer, "_claude_proposal", fake)
    out = proposer.propose("CURRENT", 0.5)
    assert out.startswith("def sum_of_divisors")
    assert captured == {"source": "CURRENT", "baseline": 0.5}


def test_extract_code_strips_fences() -> None:
    fenced = "Here is the code:\n```python\ndef f(x: int) -> int:\n    return x\n```\nDone."
    assert proposer._extract_code(fenced) == "def f(x: int) -> int:\n    return x\n"


def test_extract_code_without_fences() -> None:
    plain = "def f(x: int) -> int:\n    return x"
    assert proposer._extract_code(plain) == "def f(x: int) -> int:\n    return x\n"
