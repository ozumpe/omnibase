"""OMNI-131: the agent reviews its own feature before the feature's PR opens.

Pure parts first (the rules, the reply parser, the feature's review state),
then the SWE's wiring, run in-process: the real SWE class with a fake
workspace and self-model, and the gauntlet, the proposer and the reviewer
patched. The stub proposer always returns the same candidate, so a revision
could never be reached through Ray; here every path is.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from sis import config, episodic, feature, gauntlet, llm, org, proposer, review, roles
from sis.contract import default_contract
from sis.paths import RUNTIME_DIR

BASE = ("def sum_of_divisors(n: int) -> int:\n"
        "    return sum(d for d in range(1, n + 1) if n % d == 0)\n")
HEAD = "import math\n\ndef sum_of_divisors(n: int) -> int:\n    return math.isqrt(n)\n"
REVISED = "import math\n\ndef sum_of_divisors(n: int) -> int:\n    return math.isqrt(n) + 0\n"


def _subject(head: str, base: str = BASE, **changes: Any) -> review.Subject:
    spec = dataclasses.replace(default_contract(), **changes) if changes else default_contract()
    return review.Subject(spec, base, head)


# --- the rules -------------------------------------------------------------

def test_a_clean_diff_has_no_rule_findings() -> None:
    assert review.rule_findings(_subject(HEAD)) == []


@pytest.mark.parametrize(("line", "finding"), [
    ("import socket", "imports socket"),
    ("import os.path", "imports os"),
    ("from subprocess import run", "imports subprocess"),
    ("from urllib.request import urlopen", "imports urllib"),
])
def test_a_new_io_import_is_a_finding(line: str, finding: str) -> None:
    found = review.rule_findings(_subject(f"{line}\n{HEAD}"))
    assert found == [f"no new I/O or egress: {finding}"]


@pytest.mark.parametrize("call", ["open('x')", "exec('1')", "eval('1')", "__import__('os')"])
def test_a_new_io_call_is_a_finding(call: str) -> None:
    name = call.split("(")[0]
    head = HEAD + f"\n\ndef helper() -> object:\n    return {call}\n"
    assert review.rule_findings(_subject(head)) == [f"no new I/O or egress: calls {name}()"]


@pytest.mark.parametrize("module", [
    # OMNI-203 (L75): routes the old denylist did not name. Anything not on the
    # allowlist is a finding now, so an unlisted route cannot slip through.
    "codecs", "gzip", "bz2", "lzma", "zipfile", "tarfile", "fileinput", "linecache",
    "sqlite3", "dbm", "runpy", "code", "pty", "inspect", "gc",
])
def test_an_import_not_on_the_allowlist_is_a_finding(module: str) -> None:
    found = review.rule_findings(_subject(f"import {module}\n{HEAD}"))
    assert found == [f"no new I/O or egress: imports {module}"]


@pytest.mark.parametrize("line", [
    "from __future__ import annotations", "import math", "import itertools",
    "from functools import cache", "from collections.abc import Mapping", "import heapq",
    "import bisect", "from dataclasses import dataclass", "from typing import Any",
    "import datetime", "from statistics import fmean", "import decimal", "import fractions",
    "import re", "import random", "import json", "from zoneinfo import ZoneInfo",
])
def test_an_import_on_the_allowlist_is_not_a_finding(line: str) -> None:
    assert review.rule_findings(_subject(f"{line}\n{HEAD}")) == []


@pytest.mark.parametrize(("expr", "finding"), [
    # OMNI-203 (L75): a builtin reached without calling it by name.
    ("getattr(__builtins__, 'open')", "reads __builtins__"),
    ("__builtins__['open']", "reads __builtins__"),
    ("(lambda: 0).__globals__", "reaches .__globals__"),
    ("type(0).__subclasses__()", "reaches .__subclasses__"),
    ("getattr(int, 'real' + '')", "getattr() with a computed name"),
    ("getattr(int, '__class__')", "getattr() with a dunder name"),
    ("setattr(int, 'x', 1)", "setattr()"),
])
def test_reaching_a_builtin_indirectly_is_a_finding(expr: str, finding: str) -> None:
    head = HEAD + f"\n\ndef helper() -> object:\n    return {expr}\n"
    assert f"no new I/O or egress: {finding}" in review.rule_findings(_subject(head))


@pytest.mark.parametrize("expr", [
    "getattr(int, 'real')", "hasattr(int, 'real')", "super().__init__()",
    "helper.__name__",
])
def test_plain_attribute_use_is_not_a_finding(expr: str) -> None:
    head = HEAD + f"\n\ndef helper() -> object:\n    return {expr}\n"
    assert review.rule_findings(_subject(head)) == []


@pytest.mark.parametrize("path", sorted(
    p.name for p in (RUNTIME_DIR / "candidates").glob("*.py")))
def test_every_shipped_candidate_passes_the_rules_from_scratch(path: str) -> None:
    # The allowlist (OMNI-203) must not send the stub's own answers back: each
    # is judged as a Class-2 head with no base, so every import counts.
    source = (RUNTIME_DIR / "candidates" / path).read_text(encoding="utf-8")
    assert review.rule_findings(_subject(source, base="")) == []


def test_what_the_base_already_had_is_not_new() -> None:
    base = "import sys\n" + BASE
    assert review.rule_findings(_subject("import sys\n" + HEAD, base=base)) == []


def test_a_feature_with_no_base_is_judged_on_everything_it_imports() -> None:
    # A Class-2 feature has no base module: every I/O import is new.
    assert review.rule_findings(_subject("import socket\n" + HEAD, base="")) == [
        "no new I/O or egress: imports socket"]


def test_a_target_the_loop_may_not_write_is_a_finding() -> None:
    found = review.rule_findings(_subject(HEAD, target_path="sis/gauntlet.py"))
    assert found and found[0].startswith("policy: sis/gauntlet.py is forbidden")


def test_a_head_that_does_not_parse_is_a_finding() -> None:
    assert review.rule_findings(_subject("def (:\n")) == ["the feature's head does not parse"]


# --- the reviewer model's reply: untrusted, fail closed --------------------

def test_a_plain_approval_approves() -> None:
    assert review.parse_reply('{"approved": true, "findings": []}') == (True, [])


def test_an_approval_inside_a_fence_is_read() -> None:
    assert review.parse_reply('```json\n{"approved": true, "findings": []}\n```') == (True, [])


def test_findings_are_returned_and_cleaned() -> None:
    reply = '{"approved": false, "findings": ["name the\\u0007 loop", "", "x"]}'
    assert review.parse_reply(reply) == (False, ["name the loop", "x"])


@pytest.mark.parametrize("reply", [
    "Looks good to me!",
    '{"approved": "yes", "findings": []}',
    '{"approved": true}',
    '{"approved": true, "findings": [1]}',
    "[true]",
])
def test_an_unreadable_reply_is_not_an_approval(reply: str) -> None:
    approved, findings = review.parse_reply(reply)
    assert not approved and findings


def test_an_approval_that_lists_findings_is_not_an_approval() -> None:
    assert review.parse_reply('{"approved": true, "findings": ["rename x"]}') == (
        False, ["rename x"])


def test_a_refusal_without_reasons_still_says_why_it_failed() -> None:
    assert review.parse_reply('{"approved": false, "findings": []}') == (
        False, ["the reviewer declined without saying why"])


def test_findings_are_capped_in_number_and_length() -> None:
    many = ", ".join(f'"{"y" * 500}"' for _ in range(20))
    approved, findings = review.parse_reply(f'{{"approved": false, "findings": [{many}]}}')
    assert not approved and len(findings) == review.MAX_FINDINGS
    assert all(len(f) == review.MAX_FINDING_CHARS for f in findings)


# --- review(): the rules, then the model -----------------------------------

class _Client:
    """A reviewer model with a canned reply; records the prompt it was sent."""

    model = "claude-test"

    def __init__(self, text: str, *, cost: float = 0.02, truncated: bool = False) -> None:
        self.text, self.cost, self.truncated = text, cost, truncated
        self.prompts: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str, max_tokens: int) -> llm.LLMResponse:
        self.prompts.append((system, user))
        return llm.LLMResponse(text=self.text, cost_usd=self.cost, model=self.model,
                               truncated=self.truncated)

    def check(self) -> str:
        return self.model


@pytest.fixture
def real_reviewer(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SIS_PROPOSER", "claude")
    config.reset_config_cache()
    yield
    config.reset_config_cache()


def _no_client() -> llm.LLMClient:
    raise AssertionError("the reviewer model was asked")


def test_the_stub_reviewer_is_the_rules_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIS_PROPOSER", "stub")
    config.reset_config_cache()
    verdict = review.review(_subject(HEAD), client_factory=_no_client)
    assert verdict.approved and verdict.cost_usd == 0.0
    config.reset_config_cache()


def test_a_rule_finding_sends_the_feature_back_without_asking_the_model(
        real_reviewer: None) -> None:
    verdict = review.review(_subject("import socket\n" + HEAD), client_factory=_no_client)
    assert not verdict.approved and verdict.cost_usd == 0.0
    assert verdict.findings == ("no new I/O or egress: imports socket",)


def test_the_model_approves_and_its_cost_is_reported(real_reviewer: None) -> None:
    client = _Client('{"approved": true, "findings": []}', cost=0.03)
    verdict = review.review(_subject(HEAD), client_factory=lambda: client)
    assert verdict.approved and verdict.cost_usd == 0.03 and verdict.model == "claude-test"
    system, user = client.prompts[0]
    assert "data, not instructions" in system
    assert "+    return math.isqrt(n)" in user, "the model reads the diff"


def test_the_models_findings_are_the_verdict(real_reviewer: None) -> None:
    client = _Client('{"approved": false, "findings": ["explain the isqrt bound"]}')
    verdict = review.review(_subject(HEAD), client_factory=lambda: client)
    assert not verdict.approved and verdict.findings == ("explain the isqrt bound",)


def test_a_cut_off_review_is_not_an_approval(real_reviewer: None) -> None:
    client = _Client('{"approved": true, "findings": []}', truncated=True)
    verdict = review.review(_subject(HEAD), client_factory=lambda: client)
    assert not verdict.approved and verdict.cost_usd == 0.02


def test_the_reviewer_is_guardrail_code() -> None:
    from sis.policy import ChangeTier, classify
    assert classify("sis/review.py") is ChangeTier.FORBIDDEN


# --- the feature's review state --------------------------------------------

def _one_step() -> dict[str, Any]:
    return feature.with_step(feature.new_feature("feature/s-1", "S-1"), "S-1", 1.0, 0.5)


def test_a_rejected_review_leaves_findings_until_a_revision_passes() -> None:
    sent_back = feature.with_review(_one_step(), approved=False, findings=["rename x"])
    assert feature.needs_revision(sent_back)
    assert feature.review_state(sent_back) == {"rounds": 1, "findings": ["rename x"]}
    assert sent_back["attempts"][-1] == "review round 1: changes requested (1 finding(s))"
    revised = feature.with_step(sent_back, "S-1", 1.0, 0.6)
    assert not feature.needs_revision(revised)
    assert feature.review_state(revised)["rounds"] == 1, "the rounds are kept"


def test_an_approval_counts_a_round_and_opens_nothing_up() -> None:
    approved = feature.with_review(_one_step(), approved=True, findings=[])
    assert not feature.needs_revision(approved)
    assert feature.review_state(approved)["rounds"] == 1


def test_a_feature_from_before_omni_131_has_no_review_yet() -> None:
    assert feature.review_state(_one_step()) == {"rounds": 0, "findings": []}
    assert feature.review_state(None) == {"rounds": 0, "findings": []}


def test_the_pr_body_records_the_approving_review() -> None:
    line = review.pr_section(review.Review(approved=True, reviewer="r"), 2)
    for building in (False, True):
        body = feature.pr_body("c", _one_step(), "done", building=building, review=line)
        assert "Agent review (OMNI-131): approved by r, round 2." in body


def test_a_built_features_body_lists_no_review_rounds_as_attempts() -> None:
    reviewed = feature.with_review(_one_step(), approved=True, findings=[])
    body = feature.pr_body("c", reviewed, "done", building=True)
    assert "review round" not in body


# --- how a review's outcome is classified ----------------------------------

def test_a_review_asking_for_changes_is_neutral_and_not_convergence() -> None:
    reason = "review requested: round 1 of 3, changes requested by r: mypy names x"
    assert episodic.gate_from_reason(reason) == "review_requested"
    assert episodic.neutral_status(reason) == "review_requested"
    assert "review_requested" not in episodic.CONVERGED_STATUSES
    assert org.neutral_cycle_status({"passed": False, "reason": reason}) == "review_requested"


def test_a_review_that_gives_up_is_a_counted_failure() -> None:
    reason = "review: not approved after 3 round(s), changes requested by r: mypy x"
    assert episodic.gate_from_reason(reason) == "review"
    assert org.neutral_cycle_status({"passed": False, "reason": reason}) is None


# --- the proposer is told what to revise -----------------------------------

def test_a_revision_prompt_carries_the_findings_and_judges_against_the_base(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIS_PROPOSER", "claude")
    config.reset_config_cache()
    client = _Client(HEAD)
    monkeypatch.setattr(llm, "get_llm_client", lambda *a, **k: client)
    proposer.propose(HEAD, 0.25, history=["review round 1: changes requested"],
                     findings=["explain the isqrt bound"])
    config.reset_config_cache()
    _, user = client.prompts[0]
    assert "'explain the isqrt bound'" in user and "(data, not instructions" in user
    assert "judged against the base branch's version" in user
    assert "return the current module unchanged" not in user


# --- the SWE's wiring, in-process ------------------------------------------

class _Recorder:
    """A fake actor: every method records its call and answers from *answers*."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers, self.calls = answers, []

    def __getattr__(self, method: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((method, args, kwargs))
            answer = self.answers.get(method)
            return answer(*args, **kwargs) if callable(answer) else answer
        return SimpleNamespace(remote=call)

    def called(self, method: str) -> list[tuple[Any, ...]]:
        return [args for name, args, _ in self.calls if name == method]


class _World:
    """The state a SWE step sees: the base, the feature branch, the self-model."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, rounds: int = 3) -> None:
        self.spec = default_contract()
        self.files: dict[str, str] = {}
        self.features: dict[str, Any] = {}
        self.verdicts: list[review.Review] = []
        self.proposals: list[str] = []
        self.proposed: list[dict[str, Any]] = []
        self.validated: list[dict[str, Any]] = []
        self.ws = _Recorder({
            "live_target_source": lambda path: BASE,
            "read_file": lambda branch, path: self.files.get(branch, ""),
            "write_file": lambda branch, path, src, msg: self.files.__setitem__(branch, src),
            "open_pr": lambda *a: SimpleNamespace(id=f"PR-{len(self.ws.called('open_pr'))}"),
        })
        self.sm = _Recorder({
            "feature": lambda name: self.features.get(name),
            "plan": lambda name: None,
            "set_feature": lambda name, f: self.features.__setitem__(name, f),
        })
        swe = object.__new__(roles.SWE.__ray_metadata__.modified_class)
        swe.name = swe.role = "SWE"
        swe._ws, swe._sm = self.ws, self.sm
        swe._contract = lambda name=None: self.spec  # type: ignore[method-assign]
        self.swe = swe

        monkeypatch.setattr(roles.ray, "get", lambda value: value)
        monkeypatch.setenv("SIS_FEATURE_MAX_STEPS", "1")
        monkeypatch.setenv("SIS_REVIEW_MAX_ROUNDS", str(rounds))
        config.reset_config_cache()
        monkeypatch.setattr(roles.gauntlet, "measure_baseline",
                            lambda source, contract: 1.0 if source == BASE else 0.5)
        monkeypatch.setattr(roles.gauntlet, "validate", self._validate)
        monkeypatch.setattr(roles.proposer, "propose", self._propose)
        monkeypatch.setattr(roles.proposer, "last_cost_usd", lambda: 0.10)
        monkeypatch.setattr(roles.proposer, "last_model", lambda: "claude-test")
        monkeypatch.setattr(roles.review_mod, "review", self._review)

    def _propose(self, source: str, baseline: float, **kwargs: Any) -> str:
        self.proposed.append({"source": source, "baseline": baseline, **kwargs})
        return self.proposals.pop(0)

    def _validate(self, candidate: str, baseline: float = 0.0, **kwargs: Any) -> gauntlet.Result:
        self.validated.append({"baseline": baseline, **kwargs})
        return gauntlet.Result(passed=True, reason="ok", latency_seconds=0.4)

    def _review(self, subject: review.Subject) -> review.Review:
        assert subject.base == BASE, "the whole feature is reviewed against the base"
        return self.verdicts.pop(0)

    def step(self) -> dict[str, Any]:
        return dict(self.swe.implement("S-1"))


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    yield _World(monkeypatch)
    config.reset_config_cache()


def _changes(*findings: str, cost: float = 0.05) -> review.Review:
    return review.Review(approved=False, findings=findings, cost_usd=cost, reviewer="r")


def test_no_pr_opens_until_a_review_approves(world: _World) -> None:
    world.proposals = [HEAD, REVISED]
    world.verdicts = [_changes("explain the isqrt bound"),
                      review.Review(approved=True, cost_usd=0.05, reviewer="r")]

    sent_back = world.step()
    assert world.ws.called("open_pr") == [], "a rejected review opens no PR"
    assert sent_back["review_requested"] and sent_back["reason"].startswith(
        "review requested: round 1 of 3")
    assert sent_back["cost_usd"] == pytest.approx(0.15), "the review's spend is charged"
    assert org.neutral_cycle_status(sent_back) == "review_requested"
    assert feature.needs_revision(world.features[world.spec.name])

    approved = world.step()
    revision = world.proposed[-1]
    assert revision["source"] == HEAD, "the revision starts from the reviewed head"
    assert revision["findings"] == ["explain the isqrt bound"]
    assert revision["baseline"] == 1.0, "a revision is judged against the base"
    assert world.validated[-1]["baseline_source"] == BASE
    assert approved["passed"] and approved["pr_id"] == "PR-1"
    branch, title, head, path, body = world.ws.called("open_pr")[0]
    assert head == REVISED
    assert "Agent review (OMNI-131): approved by r, round 2" in body
    reviews = [kw for name, args, kw in world.sm.calls
               if name == "record" and args[0] == "review"]
    assert [r["approved"] for r in reviews] == [False, True]


def test_a_feature_still_unapproved_after_the_last_round_is_given_up(
        monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch, rounds=1)
    world.proposals = [HEAD]
    world.verdicts = [_changes("explain the isqrt bound")]
    result = world.step()
    config.reset_config_cache()
    assert world.ws.called("open_pr") == []
    assert not result["passed"] and result["reason"].startswith(
        "review: not approved after 1 round(s)")
    assert episodic.gate_from_reason(result["reason"]) == "review"
    assert world.features[world.spec.name] is None, "the next cycle starts a new feature"
    assert world.files, "the branch is kept for a person to read"
    branch = result["branch"]
    assert world.sm.called("give_up_branch") == [(branch,)], (
        "remembered, so the next plan does not resume it")


def test_an_unrevised_head_is_not_reviewed_again(world: _World) -> None:
    world.proposals = [HEAD, HEAD]
    world.verdicts = [_changes("explain the isqrt bound")]
    world.step()
    again = world.step()
    assert again["reason"].startswith("no change: the revision is identical")
    assert org.neutral_cycle_status(again) == "no_change"
    assert world.verdicts == [] and world.ws.called("open_pr") == []
    assert feature.needs_revision(world.features[world.spec.name]), "still to revise"


def test_a_failure_after_the_review_still_reports_its_cost(world: _World) -> None:
    world.proposals = [HEAD]
    world.verdicts = [review.Review(approved=True, cost_usd=0.05, reviewer="r")]

    def refuse(*args: Any) -> Any:
        raise PermissionError("403 from the version-control host")

    world.ws.answers["open_pr"] = refuse
    result = world.step()
    assert not result["passed"] and "PermissionError" in result["reason"]
    assert result["cost_usd"] == pytest.approx(0.15)


def test_a_given_up_branch_is_not_resumed_by_the_next_plan(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # A given-up feature leaves a branch no PR was opened from, which is what
    # OMNI-135 resumes. Without remembering it, the next plan would review it
    # all over again, and each give-up would count toward the breaker.
    from sis.ports import BranchState

    spec = default_contract()
    plan = {"feature_story_id": "S-1", "spec_id": "P-1", "epic_id": "E-1"}
    message = feature.step_message(spec.name, plan, 1, 1.0, 0.5)
    branches = [BranchState(name="feature/s-1", behind_by=0, messages=[message])]
    given_up: list[str] = []
    sm = _Recorder({"plan": None, "feature": None,
                    "given_up_branches": lambda: list(given_up)})
    ws = _Recorder({"unproposed_branches": lambda prefix: branches})
    cto = object.__new__(roles.CTO.__ray_metadata__.modified_class)
    cto._sm, cto._ws = sm, ws
    cto._contract = lambda name=None: spec  # type: ignore[method-assign]
    monkeypatch.setattr(roles.ray, "get", lambda value: value)

    assert cto.open_plan()["resumed"] == "feature/s-1", "not given up: resumed"
    given_up.append("feature/s-1")
    assert cto.open_plan() is None, "given up: a new feature starts instead"


def test_an_error_after_a_cut_off_proposal_is_not_the_proposers(
        world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    # OMNI-182 (L67): the proposer answered (cut off at the token limit), then
    # the Workspace failed while the step recorded that. The reason named the
    # proposer, which had done nothing wrong; the cost was right either way.
    def cut_off(source: str, baseline: float, **kwargs: Any) -> str:
        raise proposer.ProposalCutOff("the answer stopped at the token limit")

    def unreachable(story: str, status: Any, *args: Any) -> None:
        if status is roles.IssueStatus.TBD:  # the cut-off handler's own move
            raise RuntimeError("workspace unreachable")

    monkeypatch.setattr(roles.proposer, "propose", cut_off)
    world.ws.answers["transition"] = unreachable
    result = world.step()
    assert result["reason"] == ("error: RuntimeError: workspace unreachable "
                                "(in the SWE's step, after the proposal)")
    assert result["cost_usd"] == pytest.approx(0.10), "the proposal's spend is kept"
