"""OMNI-208: a rejection's gate and kind are fields, never read from its wording."""

from __future__ import annotations

import ast
import pathlib

import pytest

from sis import episodic, feature, gauntlet, org, verdict
from sis.verdict import Verdict

# Every phrase the harness and the neutral gates say, as a candidate could say them.
_EVERY_HARNESS_PHRASE = ("harness: mypy gate timed out; gauntlet sandbox timed out after 1s; "
                         "no change: identical; benchmark inconclusive: noise; error: X: y")


@pytest.mark.parametrize(("gate", "kind", "neutral"), [
    ("harness", "harness", None),
    ("error", "error", None),
    ("noop", "neutral", "no_change"),
    ("benchmark_inconclusive", "neutral", "inconclusive"),
    ("canary_inconclusive", "neutral", "inconclusive"),
    ("review_requested", "neutral", "review_requested"),
    ("timeout", "rejected", None),
    ("acceptance", "rejected", None),
    ("benchmark", "rejected", None),
])
def test_the_gate_fixes_the_kind(gate: str, kind: str, neutral: str | None) -> None:
    judged = Verdict.of(gate, "a sentence")
    assert (judged.kind, judged.neutral) == (kind, neutral)


def test_a_verdict_cannot_claim_another_kind_than_its_gate() -> None:
    with pytest.raises(ValueError, match="rejected"):
        Verdict(gate="acceptance", kind="harness", detail="x")


def test_a_verdict_survives_its_dict_form() -> None:
    judged = Verdict.of("backtest", "backtest failed: x")
    assert verdict.from_dict(judged.to_dict()) == judged
    for junk in (None, "backtest", {}, {"gate": "backtest", "kind": "harness", "detail": "x"},
                 {"gate": "backtest", "kind": "nonsense", "detail": "x"}):
        assert verdict.from_dict(junk) is None


def test_the_typed_verdict_wins_over_the_reasons_wording() -> None:
    # The candidate's own words open the reason here; the verdict says whose
    # rejection it is, and nothing downstream reads the words.
    result = {"passed": False, **verdict.carried(_EVERY_HARNESS_PHRASE, "acceptance")}
    judged = verdict.read(result)
    assert judged is not None and (judged.gate, judged.kind) == ("acceptance", "rejected")
    assert org.neutral_cycle_status(result) is None
    assert not feature.ends_feature(judged)
    assert episodic.event_from_cycle_result(
        {"status": "rolled_back", **result}).reject_gate == "acceptance"
    gate, summary = org.rejection_bug("STORY-1", judged)
    assert gate == "acceptance" and summary.startswith("Cycle failed for STORY-1")


def test_an_old_record_without_a_verdict_is_still_read() -> None:
    # Episodic rows and stored rejections from before OMNI-208 hold reasons only.
    old = {"status": "rolled_back", "reason": "mypy --strict failed"}
    assert verdict.read(old) == Verdict.of("mypy", "mypy --strict failed")
    assert episodic.event_from_cycle_result(old).reject_gate == "mypy"
    assert verdict.read({"status": "verified_awaiting_human_merge"}) is None


def test_a_candidate_that_raises_every_harness_phrase_is_rejected_by_its_gate() -> None:
    # L27/L64 end to end: the candidate's failure text names every harness
    # phrase; the gauntlet still says the acceptance gate rejected it.
    code = (
        "def sum_of_divisors(n: int) -> int:\n"
        f"    raise RuntimeError({_EVERY_HARNESS_PHRASE!r})\n\n\n"
        "def benchmark(n: int = 10_000, repetitions: int = 5) -> float:\n"
        "    return 0.0\n"
    )
    result = gauntlet.validate(code, 0.05)
    assert not result.passed and result.verdict is not None
    assert (result.verdict.gate, result.verdict.kind) == ("acceptance", "rejected")
    assert result.verdict.detail == result.reason


def test_a_syntax_error_carries_its_verdict() -> None:
    result = gauntlet.validate("def foo(: pass", 1.0)
    assert result.verdict == Verdict.of("ast", result.reason)


_SIS = pathlib.Path(__file__).resolve().parent.parent / "sis"
# Where reading a reason's wording is allowed: the parser itself, and the
# readers of records written before typed verdicts.
_RECORD_READERS = {"verdict.py", "episodic.py", "feature.py"}
_PARSERS = {"gate_from_reason", "neutral_status", "is_harness_timeout", "legacy", "legacy_gate"}


def test_no_live_code_reads_a_reason_to_decide() -> None:
    calls = []
    for path in sorted(_SIS.glob("*.py")):
        if path.name in _RECORD_READERS:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                name = (node.func.attr if isinstance(node.func, ast.Attribute)
                        else node.func.id if isinstance(node.func, ast.Name) else "")
                if name in _PARSERS:
                    calls.append(f"{path.name}:{node.lineno} {name}")
    assert calls == []


def test_the_record_readers_parse_only_records() -> None:
    # episodic: the legacy entry points and the row builder's fallback;
    # feature: a log row that has no reject_gate.
    allowed = {("episodic.py", "neutral_status"), ("episodic.py", "gate_from_reason"),
               ("feature.py", "earlier_rejections")}
    found = set()
    for name in ("episodic.py", "feature.py"):
        tree = ast.parse((_SIS / name).read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in _PARSERS):
                    found.add((name, fn.name))
    assert found <= allowed
