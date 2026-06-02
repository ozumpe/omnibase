"""Tests for the change-authorization policy (no Ray, no network)."""

from sis.policy import (
    ChangeTier,
    Justification,
    authorize_change,
    classify,
)

# --- classification ---

def test_guardrail_code_is_forbidden() -> None:
    for path in ("sis/gauntlet.py", "sis/policy.py", "sis/cost.py",
                 "sis/settings.py", "sis/adapters.py", "sis/adapters_real.py",
                 "Dockerfile.gauntlet"):
        assert classify(path) is ChangeTier.FORBIDDEN, path


def test_target_is_soft() -> None:
    assert classify("runtime/target.py") is ChangeTier.SOFT


def test_other_engine_code_is_strict() -> None:
    for path in ("sis/roles.py", "sis/org.py", "sis/worker.py", "main.py"):
        assert classify(path) is ChangeTier.STRICT, path


def test_target_paths_are_configurable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SIS_TARGET_PATHS", "runtime/target.py, runtime/other.py")
    assert classify("runtime/other.py") is ChangeTier.SOFT
    assert classify("runtime/target.py") is ChangeTier.SOFT


def test_guardrail_wins_even_if_listed_as_target(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Mis-configuring a guardrail file as a target must NOT make it writable.
    monkeypatch.setenv("SIS_TARGET_PATHS", "sis/gauntlet.py")
    assert classify("sis/gauntlet.py") is ChangeTier.FORBIDDEN


# --- authorization: FORBIDDEN has no override ---

def test_forbidden_denied_even_with_everything() -> None:
    d = authorize_change("sis/gauntlet.py", checks_passed=True, approved=True,
                         justification=Justification.HUMAN_REQUEST)
    assert not d.allowed
    assert "never modifiable" in d.reason


# --- authorization: SOFT (the target) ---

def test_soft_allowed_when_checks_pass() -> None:
    d = authorize_change("runtime/target.py", checks_passed=True)
    assert d.allowed and d.tier is ChangeTier.SOFT


def test_soft_denied_when_checks_fail() -> None:
    d = authorize_change("runtime/target.py", checks_passed=False)
    assert not d.allowed
    assert "must pass all checks" in d.reason


# --- authorization: STRICT (off-limits by default, strict bar when enabled) ---

def test_strict_off_limits_by_default() -> None:
    d = authorize_change("sis/roles.py", checks_passed=True, approved=True,
                         justification=Justification.HUMAN_REQUEST)
    assert not d.allowed
    assert "off-limits" in d.reason


def test_strict_enabled_still_needs_approval_and_justification(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SIS_ALLOW_STRICT_CHANGES", "1")
    # missing approval + justification
    d = authorize_change("sis/roles.py", checks_passed=True)
    assert not d.allowed
    assert "must be human-approved" in d.reason
    assert "must be justified" in d.reason


def test_strict_enabled_allowed_when_fully_satisfied(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SIS_ALLOW_STRICT_CHANGES", "1")
    d = authorize_change("sis/roles.py", checks_passed=True, approved=True,
                         justification=Justification.EXCEPTION)
    assert d.allowed and d.tier is ChangeTier.STRICT
