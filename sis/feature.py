"""sis.feature — a feature branch: when it is finished, and what its PR says (OMNI-130).

Pure. A feature is the unit a human reviews: several steps, each a candidate
that passed the gauntlet and was committed to an agent-owned branch, then one
PR to the base branch (``develop``). It is finished after ``N`` accepted steps
(``loop.feature_max_steps``), or sooner, when a step finds no further gain. A
Class-2 feature (OMNI-147) is *built* rather than optimised: it is finished by
its first step, the first candidate that passes every gate, and the attempts
before it are notes the next prompt sees.

The state is a plain dict, so it can cross Ray actor boundaries:
``{"branch", "story", "steps": [...], "attempts": [...]}``.

Every step's commit carries the plan it belongs to and its timings as
trailers (:func:`step_message`), so a feature a process left half built can be
rebuilt from its branch alone (:func:`resumable_feature`, OMNI-135).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

from sis import episodic
from sis.ports import BranchState, PullRequest

# How many earlier attempts a proposer is shown. Enough to stop it repeating
# itself (the second AWS run resubmitted byte-identical code, OMNI-127), few
# enough to keep the prompt about the code.
MAX_NOTES = 5


BRANCH_PREFIX = "feature/"


def feature_branch(story_id: str) -> str:
    """A feature's branch is named after the story of its first step."""
    return f"{BRANCH_PREFIX}{story_id.lower()}"


def awaiting_decision(prs: Iterable[PullRequest]) -> list[PullRequest]:
    """The open PRs that are the loop's own, in the order given (OMNI-136).

    One of these is a human decision the loop must wait for, whichever
    process or box opened it. Matched on the branch namespace the loop has
    always used (``feature/<story>``, before and after OMNI-130). Erring
    towards holding is the safe side: a human's PR from a ``feature/`` branch
    makes the loop wait, while a missed agent PR is what opened #14 beside
    #13 on ``testrun``.
    """
    return [pr for pr in prs if pr.branch.startswith(BRANCH_PREFIX)]


_TITLE = re.compile(r"(?:Optimise|Build) (?P<contract>[\w-]+): \d+ steps? \(")


def contract_from_title(title: str, known: Iterable[str]) -> str | None:
    """The contract a feature PR's title names (see :func:`pr_title`), if *known*.

    Only a registered name comes back: a title is text a human can edit, and
    an unknown name would only be a lookup that finds nothing later.
    """
    match = _TITLE.match(title)
    name = match.group("contract") if match else None
    return name if name in set(known) else None


def new_feature(branch: str, story_id: str) -> dict[str, Any]:
    return {"branch": branch, "story": story_id, "steps": [], "attempts": []}


def ends_feature(reason: str | None) -> bool:
    """Whether a rejection means the feature's head cannot be beaten.

    That is the feature's natural end, not a failure of it: nothing to
    improve (a no-op), no measurable gain (an inconclusive benchmark), or a
    gain below the margin ("no improvement").
    """
    if not reason:
        return False
    return episodic.neutral_status(reason) is not None or reason.startswith("no improvement")


def finds_no_gain(feature: dict[str, Any] | None, reason: str | None) -> bool:
    """Whether a rejected step says the *base* cannot be beaten (OMNI-138). Pure.

    With no feature in progress, a step starts from the base branch's head; a
    verdict that would end a feature (:func:`ends_feature`) then means the
    target has converged. That is not a failure of the loop, and filing a bug
    and counting it toward the breaker is how a finished ``sum_of_divisors``
    tripped the breaker in the fourth AWS run. Inside a feature the same
    verdict ends the feature instead, and its PR opens.
    """
    return feature is None and ends_feature(reason)


def is_full(feature: dict[str, Any], max_steps: int) -> bool:
    return len(feature["steps"]) >= max(1, max_steps)


def with_step(feature: dict[str, Any], story_id: str, baseline_s: float,
              candidate_s: float | None) -> dict[str, Any]:
    """*feature* with one more committed step, and a note the next prompt sees."""
    gain = (f"{(1 - candidate_s / baseline_s) * 100:.1f}% faster"
            if candidate_s is not None and baseline_s else "accepted")
    step = {"story": story_id, "baseline_s": baseline_s, "candidate_s": candidate_s}
    return with_note({**feature, "steps": [*feature["steps"], step]},
                     f"step {len(feature['steps']) + 1} accepted: {gain}")


def with_note(feature: dict[str, Any], note: str) -> dict[str, Any]:
    """*feature* remembering *note* (the last :data:`MAX_NOTES` only)."""
    notes = [*feature["attempts"], " ".join(note.split())[:300]]
    return {**feature, "attempts": notes[-MAX_NOTES:]}


_TRAILER = re.compile(r"^(Sis-[A-Za-z-]+): (\S+)[ \t]*$", re.MULTILINE)
_ID = re.compile(r"[\w.-]{1,64}")


def step_message(contract_name: str, plan: Mapping[str, Any], step: int,
                 baseline_s: float, candidate_s: float | None, *,
                 building: bool = False) -> str:
    """The commit message of a feature's step: a subject, then trailers (OMNI-135).

    The trailers are what :func:`parse_step` reads back after a restart: the
    contract, the plan the step worked under, and its timings for the PR's
    evidence table. A built feature (*building*) has no timings: its step
    records a baseline of 0 and no candidate time.
    """
    story = plan["feature_story_id"]
    return "\n".join([
        f"Step {step}: {'build' if building else 'optimise'} {contract_name} ({story})",
        "",
        f"Sis-Contract: {contract_name}",
        f"Sis-Story: {story}",
        f"Sis-Spec: {plan['spec_id']}",
        f"Sis-Epic: {plan['epic_id']}",
        f"Sis-Baseline-S: {baseline_s!r}",
        f"Sis-Candidate-S: {'none' if candidate_s is None else repr(candidate_s)}",
    ])


def _seconds(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def parse_step(message: str) -> dict[str, Any] | None:
    """A step commit's trailers (see :func:`step_message`), or None. Pure.

    None for anything else, including a commit a human pushed to the branch:
    a commit message is text anyone with push access writes, so every field
    is checked, and a malformed one means "not a loop step".
    """
    trailers = dict(_TRAILER.findall(message))
    ids = [trailers.get(k, "") for k in ("Sis-Contract", "Sis-Story", "Sis-Spec", "Sis-Epic")]
    if not all(_ID.fullmatch(i) for i in ids):
        return None
    baseline = _seconds(trailers.get("Sis-Baseline-S", ""))
    raw_candidate = trailers.get("Sis-Candidate-S", "")
    candidate = None if raw_candidate == "none" else _seconds(raw_candidate)
    if baseline is None or (candidate is None and raw_candidate != "none"):
        return None
    contract_name, story, spec, epic = ids
    return {"contract": contract_name, "story": story, "spec": spec, "epic": epic,
            "baseline_s": baseline, "candidate_s": candidate}


def resumable_feature(
    contract_name: str, branches: Iterable[BranchState]
) -> dict[str, Any] | None:
    """The unfinished feature *contract_name* should carry on with, or None. Pure.

    Returns ``{"feature": ..., "plan": ...}``, rebuilt from the branch's
    commits (OMNI-135). A branch qualifies only if every commit on it is one
    of this contract's steps under one plan, and the base has not moved since
    it forked: those steps were measured against a base that is no longer
    there. Of several, the one with the most steps wins, then the name, so
    the choice does not depend on listing order.
    """
    found: list[tuple[int, str, list[dict[str, Any]]]] = []
    for branch in branches:
        if branch.behind_by or not branch.name.startswith(BRANCH_PREFIX):
            continue
        parsed = [parse_step(m) for m in branch.messages]
        steps = [s for s in parsed if s is not None]
        if not steps or len(steps) != len(parsed):
            continue
        if {(s["contract"], s["story"], s["spec"], s["epic"]) for s in steps} != {
                (contract_name, steps[0]["story"], steps[0]["spec"], steps[0]["epic"])}:
            continue
        found.append((len(steps), branch.name, steps))
    if not found:
        return None
    _, name, steps = max(found, key=lambda f: (f[0], f[1]))
    first = steps[0]
    feature = new_feature(name, first["story"])
    for s in steps:
        feature = with_step(feature, s["story"], s["baseline_s"], s["candidate_s"])
    plan = {"spec_id": first["spec"], "epic_id": first["epic"],
            "feature_story_id": first["story"]}
    return {"feature": feature, "plan": plan}


def pr_title(contract_name: str, feature: dict[str, Any], *, building: bool = False) -> str:
    n = len(feature["steps"])
    verb = "Build" if building else "Optimise"
    return f"{verb} {contract_name}: {n} step{'s' if n != 1 else ''} ({feature['story']})"


def pr_body(contract_name: str, feature: dict[str, Any], finished_because: str, *,
            building: bool = False) -> str:
    """The evidence a reviewer reads: every step, and why the feature stopped.

    A built feature has no timings to show, so its body says which gates passed
    and what the attempts before it were rejected for.
    """
    if building:
        earlier = [n for n in feature["attempts"] if not n.startswith("step ")]
        return "\n".join([
            f"Feature for contract `{contract_name}`, built from its spec (OMNI-147). "
            "The module passed every gate the contract names: types, the public API, "
            "the acceptance tests, the domain laws on generated inputs, and any "
            "recorded history.",
            "",
            *([f"Attempts before it ({len(earlier)} shown, most recent last):", "",
               *(f"- {n}" for n in earlier), ""] if earlier else []),
            f"Finished because: {finished_because}.",
            "",
            "Automated proposal. Human review and merge required.",
        ])
    rows = [
        f"| {i} | {s['story']} | {s['baseline_s']:.6f}s | "
        + (f"{s['candidate_s']:.6f}s |" if s["candidate_s"] is not None else "– |")
        for i, s in enumerate(feature["steps"], 1)
    ]
    return "\n".join([
        f"Feature for contract `{contract_name}`: {len(feature['steps'])} committed "
        "step(s), each passed the gauntlet before it was committed (OMNI-130).",
        "",
        "| Step | Story | Baseline | Candidate |",
        "|---|---|---|---|",
        *rows,
        "",
        f"Finished because: {finished_because}.",
        "",
        "Automated proposal. Human review and merge required.",
    ])
