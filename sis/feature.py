"""sis.feature — a feature branch: when it is finished, and what its PR says (OMNI-130).

Pure. A feature is the unit a human reviews: several steps, each a candidate
that passed the gauntlet and was committed to an agent-owned branch, then one
PR to the base branch (``develop``). It is finished after ``N`` accepted steps
(``loop.feature_max_steps``), or sooner, when a step finds no further gain.

The state is a plain dict, so it can cross Ray actor boundaries:
``{"branch", "story", "steps": [...], "attempts": [...]}``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from sis import episodic
from sis.ports import PullRequest

# How many earlier attempts a proposer is shown. Enough to stop it repeating
# itself (the second AWS run resubmitted byte-identical code, OMNI-127), few
# enough to keep the prompt about the code.
MAX_NOTES = 5


_BRANCH_PREFIX = "feature/"


def feature_branch(story_id: str) -> str:
    """A feature's branch is named after the story of its first step."""
    return f"{_BRANCH_PREFIX}{story_id.lower()}"


def awaiting_decision(prs: Iterable[PullRequest]) -> list[PullRequest]:
    """The open PRs that are the loop's own, in the order given (OMNI-136).

    One of these is a human decision the loop must wait for, whichever
    process or box opened it. Matched on the branch namespace the loop has
    always used (``feature/<story>``, before and after OMNI-130). Erring
    towards holding is the safe side: a human's PR from a ``feature/`` branch
    makes the loop wait, while a missed agent PR is what opened #14 beside
    #13 on ``testrun``.
    """
    return [pr for pr in prs if pr.branch.startswith(_BRANCH_PREFIX)]


_TITLE = re.compile(r"Optimise (?P<contract>[\w-]+): \d+ steps? \(")


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


def pr_title(contract_name: str, feature: dict[str, Any]) -> str:
    n = len(feature["steps"])
    return f"Optimise {contract_name}: {n} step{'s' if n != 1 else ''} ({feature['story']})"


def pr_body(contract_name: str, feature: dict[str, Any], finished_because: str) -> str:
    """The evidence a reviewer reads: every step, and why the feature stopped."""
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
