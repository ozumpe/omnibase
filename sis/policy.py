"""sis.policy — what the self-improvement loop is allowed to change.

Every path the loop wants to modify is classified into one of three tiers, and
a change is authorised (pre-write) only if its tier permits it:

- **FORBIDDEN — guardrail / safety code.** NEVER modifiable by the loop, with
  **no override path** (not even human approval). If the rewriter could edit its
  own gauntlet, sandbox, spend brakes, secret handling, or *this policy*, the
  safety guarantees evaporate. This is the load-bearing invariant.
- **STRICT — all other engine code.** Off-limits to the loop by default. Enabling
  it (``SIS_ALLOW_STRICT_CHANGES=1``) still requires, before the loop may even
  propose a change: a **justification** (a collected exception or an explicit
  human request) **and** human **approval** — plus it must pass every check.
- **SOFT — the designated optimisation target(s).** Meant to be changed and
  optimised. Must pass the checks; review is enforced downstream by QA + the
  mandatory human PR merge (the loop never merges to ``main``).

This module is itself in the FORBIDDEN set, so the loop cannot loosen its own
rules.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from enum import Enum

from sis.paths import PROJECT_ROOT


class ChangeTier(str, Enum):
    FORBIDDEN = "forbidden"
    STRICT = "strict"
    SOFT = "soft"


class Justification(str, Enum):
    NONE = "none"
    HUMAN_REQUEST = "human_request"
    EXCEPTION = "exception"


# Safety-critical code the loop must NEVER modify. Absolute — no override.
# Paths are repo-root-relative (posix).
GUARDRAIL_PATHS: tuple[str, ...] = (
    "sis/policy.py",          # this policy itself
    "sis/gauntlet.py",        # the validation moat + sandbox + timeout
    "sis/cost.py",            # spend accounting that feeds the brakes
    "sis/settings.py",        # secret loading + masking
    "sis/adapters.py",        # RequiresHumanApproval guardrails (in-memory)
    "sis/adapters_real.py",   # RequiresHumanApproval guardrails (real)
    "Dockerfile.gauntlet",    # the sandbox image
)

# The designated optimisation target(s) — the SOFT tier. Configurable via
# SIS_TARGET_PATHS (comma-separated, repo-root-relative posix paths) so widening
# what the loop may optimise is a deliberate, reviewed change. Guardrail paths
# always win over this list (see classify), so a target can never silently
# overlap safety code even if mis-configured here.
DEFAULT_TARGET_PATHS: tuple[str, ...] = (
    "runtime/target.py",
)


def target_paths() -> tuple[str, ...]:
    """The SOFT-tier optimisation targets (env-overridable)."""
    raw = os.getenv("SIS_TARGET_PATHS")
    if not raw:
        return DEFAULT_TARGET_PATHS
    return tuple(
        part.strip().replace("\\", "/").lstrip("./")
        for part in raw.split(",")
        if part.strip()
    )


@dataclass(frozen=True)
class TierPolicy:
    tier: ChangeTier
    loop_may_modify: bool      # may the loop modify this tier at all?
    must_pass_checks: bool     # full gauntlet must pass
    must_be_reviewed: bool     # QA + human merge (enforced downstream)
    must_be_approved: bool     # explicit human approval before the loop proposes
    must_be_justified: bool    # justified by an exception or a human request


_POLICIES: dict[ChangeTier, TierPolicy] = {
    ChangeTier.FORBIDDEN: TierPolicy(
        ChangeTier.FORBIDDEN, loop_may_modify=False,
        must_pass_checks=True, must_be_reviewed=True,
        must_be_approved=True, must_be_justified=True,
    ),
    ChangeTier.STRICT: TierPolicy(
        ChangeTier.STRICT, loop_may_modify=False,  # off-limits unless explicitly enabled
        must_pass_checks=True, must_be_reviewed=True,
        must_be_approved=True, must_be_justified=True,
    ),
    ChangeTier.SOFT: TierPolicy(
        ChangeTier.SOFT, loop_may_modify=True,
        must_pass_checks=True, must_be_reviewed=True,
        must_be_approved=False, must_be_justified=False,
    ),
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    tier: ChangeTier
    reason: str


def _strict_enabled() -> bool:
    return os.getenv("SIS_ALLOW_STRICT_CHANGES", "0") == "1"


def _rel(path: str | pathlib.Path) -> str:
    """Repo-root-relative posix path; falls back to a cleaned string."""
    candidate = pathlib.Path(path)
    try:
        return candidate.resolve().relative_to(PROJECT_ROOT).as_posix()
    except (ValueError, OSError):
        return str(path).replace("\\", "/").lstrip("./")


def classify(path: str | pathlib.Path) -> ChangeTier:
    """Classify a path into its change tier."""
    rel = _rel(path)
    # Guardrail precedence is absolute: a path that is both listed as a target
    # AND is guardrail code is still FORBIDDEN.
    if rel in GUARDRAIL_PATHS:
        return ChangeTier.FORBIDDEN
    if rel in target_paths():
        return ChangeTier.SOFT
    return ChangeTier.STRICT


def policy_for(tier: ChangeTier) -> TierPolicy:
    return _POLICIES[tier]


def authorize_change(
    path: str | pathlib.Path,
    *,
    checks_passed: bool,
    approved: bool = False,
    justification: Justification = Justification.NONE,
) -> Decision:
    """Pre-write authorization: may the loop write this path with this evidence?

    Review (``must_be_reviewed``) is *not* gated here — it is enforced downstream
    by QA + the mandatory human PR merge. This function gates the decision the
    loop makes *before* proposing/writing a change.
    """
    tier = classify(path)
    policy = _POLICIES[tier]

    if tier is ChangeTier.FORBIDDEN:
        return Decision(False, tier, "guardrail code is never modifiable by the loop (no override)")

    may_modify = policy.loop_may_modify or (tier is ChangeTier.STRICT and _strict_enabled())
    if not may_modify:
        return Decision(
            False, tier,
            f"{tier.value} code is off-limits to the loop "
            "(set SIS_ALLOW_STRICT_CHANGES=1 to enable, under strict constraints)",
        )

    unmet: list[str] = []
    if policy.must_pass_checks and not checks_passed:
        unmet.append("must pass all checks")
    if policy.must_be_approved and not approved:
        unmet.append("must be human-approved")
    if policy.must_be_justified and justification is Justification.NONE:
        unmet.append("must be justified by an exception or a human request")
    if unmet:
        return Decision(False, tier, "; ".join(unmet))

    return Decision(True, tier, f"{tier.value} change authorised")
