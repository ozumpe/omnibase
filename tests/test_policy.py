"""Tests for the change-authorization policy (no Ray, no network)."""

from sis.policy import (
    ChangeTier,
    Justification,
    _rel,
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
    for path in ("sis/roles.py", "sis/org.py", "sis/proposer.py", "main.py"):
        assert classify(path) is ChangeTier.STRICT, path


def test_rel_strips_a_leading_dot_slash_prefix_not_characters() -> None:
    # L4: the fallback (for paths outside the repo root) must strip a leading
    # "./" *prefix*, not `.`/`/` *characters* — otherwise "../x" → "x" and a
    # dotfile like "../.github/ci.yml" gets its leading dot eaten.
    assert _rel("../x") == "../x"
    assert _rel("../.github/ci.yml") == "../.github/ci.yml"


def test_target_paths_are_configurable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SIS_TARGET_PATHS", "runtime/target.py, runtime/other.py")
    assert classify("runtime/other.py") is ChangeTier.SOFT
    assert classify("runtime/target.py") is ChangeTier.SOFT


def test_guardrail_wins_even_if_listed_as_target(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Mis-configuring a guardrail file as a target must NOT make it writable.
    monkeypatch.setenv("SIS_TARGET_PATHS", "sis/gauntlet.py")
    assert classify("sis/gauntlet.py") is ChangeTier.FORBIDDEN


def test_target_paths_prefix_strip_is_not_char_strip(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # L10: a "./" *prefix* is stripped, but leading dot chars must survive —
    # lstrip("./") would mangle ".github/x" → "github/x". removeprefix must not.
    from sis.policy import target_paths
    monkeypatch.setenv("SIS_TARGET_PATHS", "./runtime/target.py, .github/x.py")
    assert target_paths() == ("runtime/target.py", ".github/x.py")


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


# --- guardrail directories (L5 Layer 1 precondition) -----------------------


def test_contract_space_is_forbidden() -> None:
    # The implementer must not be able to edit its own exam. Enumerating files
    # would decay the moment a contract gains one, so a whole tree is guarded.
    for path in ("specs/README.md",
                 "specs/sum_of_divisors/oracle.py",
                 "specs/sum_of_divisors/tests.py",
                 "specs/deeply/nested/fixture.json"):
        assert classify(path) is ChangeTier.FORBIDDEN, path


def test_the_guarded_directory_itself_is_forbidden() -> None:
    assert classify("specs") is ChangeTier.FORBIDDEN


def test_guardrail_dirs_match_path_segments_not_string_prefixes() -> None:
    # "specs" must not swallow unrelated siblings — a bare
    # str.startswith("specs") would classify both of these FORBIDDEN and
    # quietly freeze code the loop is allowed to touch.
    assert classify("specs_draft/notes.md") is ChangeTier.STRICT
    assert classify("specstest.py") is ChangeTier.STRICT


def test_contract_space_stays_forbidden_when_pointed_at_as_a_target(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Guardrail precedence is absolute: mis-configuring SIS_TARGET_PATHS at the
    # contract must not make the exam writable.
    monkeypatch.setenv("SIS_TARGET_PATHS", "specs/sum_of_divisors/oracle.py")
    assert classify("specs/sum_of_divisors/oracle.py") is ChangeTier.FORBIDDEN


def test_contract_space_is_forbidden_through_a_traversal_path() -> None:
    # ../ games must not launder a contract path into a writable tier.
    assert classify("runtime/../specs/sum_of_divisors/oracle.py") is ChangeTier.FORBIDDEN


def test_contract_changes_are_denied_with_every_permission_granted() -> None:
    # FORBIDDEN has no override path, not even human approval + justification.
    decision = authorize_change(
        "specs/sum_of_divisors/oracle.py",
        checks_passed=True, approved=True, justification=Justification.HUMAN_REQUEST,
    )
    assert not decision.allowed
    assert decision.tier is ChangeTier.FORBIDDEN


def test_classification_does_not_depend_on_the_working_directory(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # The loop spawns subprocesses with their own cwd, and every tier list here
    # is repo-root-relative. Resolving against cwd would make a path's tier
    # depend on where the interpreter was started — and would let a traversal
    # path slip past the contract guardrail from anywhere but the repo root.
    monkeypatch.chdir(tmp_path)
    assert classify("specs/sum_of_divisors/oracle.py") is ChangeTier.FORBIDDEN
    assert classify("runtime/../specs/oracle.py") is ChangeTier.FORBIDDEN
    assert classify("sis/gauntlet.py") is ChangeTier.FORBIDDEN
    assert classify("runtime/target.py") is ChangeTier.SOFT
    assert classify("sis/org.py") is ChangeTier.STRICT
