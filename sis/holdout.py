"""sis.holdout — the holdout budget (OMNI-207, docs/OMNITRACK_VISION.md D5).

Every candidate the backtest gate judges is judged on the same held-out
episodes, and each accept or reject tells the loop a little about them. A loop
that keeps proposing until something clears the skill margin will clear it in
the end by selection, not by skill: it overfits data it was never shown. D5
decided to manage that with a budget, measured from the episodic log.

So each held-out fixture gets a number of judgements. The count is read from
the episodic records, never from an actor's memory, so a restarted loop or a
new box (whose earlier runs' logs are fetched into :data:`HISTORY_DIR`) counts
what came before. It is keyed by the fixture's digest: a re-promoted fixture is
new data and starts a new count. When the budget is spent, a cycle on that
contract is refused before any spend, as a neutral stop that pages a human.

The limit is the contract's ``holdout_budget``, else ``brakes.holdout_budget``.
Both live where the loop cannot write (``specs/`` and the contract layer, the
config schema and ``config.yml``, and this module are all POLICY-FORBIDDEN), so
raising it is a human's edit, never the loop's. Rotating the holdout, D5's
other half, is not built here.

What counts as a judgement is decided by :func:`judged_holdout`, which is pure,
like everything here but the two readers at the bottom. A candidate judged
again on the same fixture (the same ``candidate_sha``) is counted once: the
same code on the same weeks gets the same verdict, so the repeat tells the loop
nothing new. That is the "already built" check after a merge, which judges the
merged module again on every cycle (OMNI-224, M41). Where held-out data is
plentiful and a repeat should count anyway, a contract's ``holdout_count_repeats``
(else ``brakes.holdout_count_repeats``) turns that off.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field, replace
from pathlib import Path

from sis.backtest import Backtest, holdout
from sis.episodic import ACCEPTED_OUTCOMES, EpisodicEvent, EpisodicStore, JsonlEpisodicStore
from sis.paths import PROJECT_ROOT, RUNTIME_DIR

# Earlier runs' episodic logs, as the scheduled AWS runner fetches them from
# the artifacts bucket's ``runs/`` (one directory per run). Below the runtime
# directory, not in it: the artifact sync uploads only the runtime directory's
# own files, so a fetched log is never uploaded again under a new run.
HISTORY_DIR = RUNTIME_DIR / "history"

# The gates every profile runs before the backtest. A candidate rejected at
# one of them never ran on the held-out episodes. ``pytest`` is the
# acceptance gate's name before OMNI-17; ``tampering`` and ``timeout`` are
# not here, since they can happen at any gate (see :func:`judged_holdout`).
BEFORE_BACKTEST = frozenset({"ast", "noop", "mypy", "interface", "acceptance",
                             "pytest", "invariant"})

# What follows a backtest pass: the gates after it in a profile, and every
# judgement made of a candidate that passed the gauntlet (the policy check,
# the review, the canary). Rejected there, the candidate's backtest passed.
AFTER_BACKTEST = frozenset({
    "differential_benchmark", "correctness", "benchmark", "benchmark_inconclusive",
    "benchmark_unmeasurable", "benchmark_malformed", "slo", "slo_error",
    "policy", "review", "review_requested", "built",
    "canary_evidence", "canary_invariant", "canary_disagreement", "canary_regression",
    "canary_errors", "canary_inconclusive",
})

# How a harness, timeout or tampering reason names the gate it happened at:
# "the backtest gate raised", "backtest gate timed out", "while the slo gate ran".
_GATE_NAMED = re.compile(r"\b([a-z_]+) gate\b")

# What :func:`sis.feature.resubmitted` appends: the same candidate, not run again.
_RESUBMITTED = "[resubmitted:"

# The neutral stop a spent budget records: no spend, no breaker count, a page.
HOLDOUT_SPENT = "holdout_spent"


def fixture_digest(
    backtests: Sequence[Backtest], read: Callable[[str], bytes] | None = None
) -> str | None:
    """One digest for a contract's held-out episodes, or None if it has none.

    A private episode's digests are the ones its committed index lists; a
    public one's are read from its files (*read* takes a repo-relative path).
    Each episode's name and both digests go in, sorted by name, so the same
    data in another order is the same fixture and any change is a new one.
    """
    held = holdout(tuple(backtests))
    if not held:
        return None
    reader = read or _read_repo_file

    def file_digest(path: str, known: str | None) -> str:
        if known is not None:
            return known
        try:
            return hashlib.sha256(reader(path)).hexdigest()
        except OSError:
            # The gate will refuse it as a harness fault; the digest only has
            # to differ from the fixture's once it is there.
            return f"missing:{path}"

    lines = sorted(f"{b.name}\t{file_digest(b.fixture, b.fixture_sha256)}\t"
                   f"{file_digest(b.expect, b.expect_sha256)}" for b in held)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def judged_holdout(event: EpisodicEvent) -> bool:
    """Whether the backtest gate gave its verdict on this cycle's candidate. Pure.

    Counted: a candidate that passed the gauntlet, or was rejected by the
    backtest or anything after it. Not counted: a rejection at an earlier gate,
    a cycle with no candidate (a refusal, a pause, a proposal cut off), a
    resubmission the gauntlet did not run again, and a harness fault at the
    backtest gate or before it, which is no verdict. A timeout or tampering is
    counted unless its reason names a gate before the backtest: the candidate
    may have run on the held-out episodes, and an over-count only stops the
    loop sooner. So is an exception after a candidate was proposed: where it
    happened is not recorded.
    """
    reason = (event.reject_reason or "").lower()
    if _RESUBMITTED in reason:
        return False
    if event.outcome in ACCEPTED_OUTCOMES:
        return True
    gate = event.reject_gate
    if gate is None:
        return False
    if gate == "backtest" or gate in AFTER_BACKTEST:
        return True
    named = _GATE_NAMED.search(reason)
    at = named.group(1) if named else None
    if gate == "harness":
        return at is not None and at in AFTER_BACKTEST
    if gate in ("timeout", "tampering"):
        return at not in BEFORE_BACKTEST
    if gate == "error":
        return event.candidate_sha is not None
    return False


def counts(event: EpisodicEvent, judged: AbstractSet[str]) -> bool:
    """Whether *event* uses a judgement, given the candidates *judged* already. Pure.

    *judged* holds the ``candidate_sha`` of every candidate already counted on
    the same contract and fixture. A repeat is free (OMNI-224, M41); a
    judgement with no recorded hash always counts, which only stops the loop
    sooner.
    """
    if not judged_holdout(event):
        return False
    return event.candidate_sha is None or event.candidate_sha not in judged


def _tally(events: Iterable[EpisodicEvent], contract: str, digest: str,
           count_repeats: bool = False) -> tuple[int, frozenset[str]]:
    """The count :func:`spent` returns, and the candidates it counted. Pure.

    With *count_repeats*, every judgement counts, a repeat included, and no
    candidate is remembered."""
    seen: set[str] = set()
    judged: set[str] = set()
    count = 0
    for event in events:
        if (event.contract != contract or event.holdout_digest != digest
                or event.cycle_id in seen):
            continue
        seen.add(event.cycle_id)
        if counts(event, judged):
            count += 1
            if event.candidate_sha is not None and not count_repeats:
                judged.add(event.candidate_sha)
    return count, frozenset(judged)


def spent(events: Iterable[EpisodicEvent], contract: str, digest: str,
          count_repeats: bool = False) -> int:
    """How many candidates the backtest judged on *contract*'s fixture *digest*. Pure.

    One per cycle (an event is a cycle), each cycle once even when it is in
    the log twice (``cycle_id``): a box that kept its own log also fetches the
    copy it uploaded. Each candidate once (:func:`counts`): judging the same
    code again tells the loop nothing new, unless *count_repeats*.
    """
    return _tally(events, contract, digest, count_repeats)[0]


@dataclass(frozen=True)
class Budget:
    """Where one contract's holdout budget stands."""

    contract: str
    digest: str
    limit: int
    used: int
    # The candidates already counted, so a cycle's own record can say whether
    # it used a judgement (:meth:`after`). Not reported anywhere.
    judged: frozenset[str] = field(default=frozenset(), repr=False, compare=False)
    # Whether a candidate judged again counts again (``holdout_count_repeats``).
    count_repeats: bool = False

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def after(self, event: EpisodicEvent) -> Budget:
        """Where the budget stands once *event* is counted. Pure."""
        if not counts(event, self.judged):
            return self
        sha = event.candidate_sha
        judged = (self.judged if sha is None or self.count_repeats
                  else self.judged | {sha})
        return replace(self, used=self.used + 1, judged=judged)

    def describe(self) -> str:
        """One line for the console and ``sis.admin``. Names no held-out value."""
        return (f"holdout budget for {self.contract}: {self.used} of {self.limit} "
                f"judgement(s) used, {self.remaining} left (fixture {self.digest[:12]})")

    def as_dict(self) -> dict[str, object]:
        return {"contract": self.contract, "digest": self.digest, "limit": self.limit,
                "used": self.used, "remaining": self.remaining}


def budget(
    contract: str, backtests: Sequence[Backtest], declared: int | None, default: int,
    events: Iterable[EpisodicEvent], read: Callable[[str], bytes] | None = None,
    count_repeats: bool = False,
) -> Budget | None:
    """*contract*'s holdout budget, or None when it has no held-out episodes.

    *count_repeats* is the contract's ``holdout_count_repeats``, else
    ``brakes.holdout_count_repeats``: see :func:`counts_repeats`."""
    digest = fixture_digest(backtests, read)
    if digest is None:
        return None
    limit = declared if declared is not None else default
    used, judged = _tally(events, contract, digest, count_repeats)
    return Budget(contract, digest, limit, used, judged, count_repeats)


def counts_repeats(declared: bool | None, default: bool) -> bool:
    """Whether a candidate judged again uses another judgement. Pure.

    The contract's ``holdout_count_repeats`` when it declares one, else the
    config's ``brakes.holdout_count_repeats``. Off by default (OMNI-224): the
    same code on the same weeks tells the loop nothing new. On, for a contract
    whose held-out data is plentiful enough that the simpler count is wanted.
    """
    return default if declared is None else declared


def refusal(b: Budget) -> str:
    """The page body for a spent budget: what happened, and what a human can do."""
    return (f"Contract {b.contract} has used its holdout budget: {b.used} of {b.limit} "
            f"candidates have been judged on its held-out fixture ({b.digest[:12]}). "
            "Each judgement tells the loop a little about the held-out weeks, so more "
            "attempts would start to fit them by selection rather than by skill "
            "(docs/OMNITRACK_VISION.md D5). No cycle on this contract runs, and nothing "
            "was spent. To go on: promote a new held-out fixture (its count starts at "
            "zero), or raise the contract's holdout_budget (or brakes.holdout_budget) "
            "by hand.")


# --------------------------------------------------------------------------
# I/O: the records a count reads
# --------------------------------------------------------------------------


def _read_repo_file(path: str) -> bytes:
    return (PROJECT_ROOT / path).read_bytes()


def history_events(history_dir: Path | None = None) -> list[EpisodicEvent]:
    """The events of every earlier run's log fetched into *history_dir*."""
    history_dir = history_dir if history_dir is not None else HISTORY_DIR
    if not history_dir.is_dir():
        return []
    out: list[EpisodicEvent] = []
    for log in sorted(history_dir.rglob("episodic*.jsonl")):
        if log.is_file():
            out.extend(JsonlEpisodicStore(log).events())
    return out


def recorded_events(
    store: EpisodicStore, history_dir: Path | None = None
) -> list[EpisodicEvent]:
    """This box's events and earlier runs', for a count that outlives the box."""
    return [*history_events(history_dir), *store.events()]


__all__ = [
    "AFTER_BACKTEST", "BEFORE_BACKTEST", "HISTORY_DIR", "HOLDOUT_SPENT", "Budget", "budget",
    "counts", "counts_repeats", "fixture_digest", "history_events", "judged_holdout",
    "recorded_events", "refusal",
    "spent",
]
