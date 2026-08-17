"""Tests for the unified configuration layer (OMNI-27).

Three things are worth pinning here, in descending order of how badly it hurts
when they break:

1. **The config system cannot be used to unprotect the config system.** Every
   knob the engine has is now reachable from one file, including the sandbox
   mode and the spend cap, so "the loop may not write it" has to hold even when
   the loop is the thing choosing what the loop may write.
2. **The committed ``config.yml`` cannot drift from the code.** The whole point
   of one schema is that the documented default and the actual default are the
   same object; a hand-maintained file would be back to the README problem this
   replaced.
3. **Precedence is exactly CLI > env > file > default**, and every value can say
   which of those it came from — the model the operator UI (OMNI-28) renders.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any

import pytest

from sis import config, gauntlet, policy
from sis.config import ConfigTier, Key, Kind, Source
from sis.policy import ChangeTier, Justification, authorize_change, classify


@pytest.fixture(autouse=True)
def _isolate_config() -> Any:
    """Keep a test's CLI overlay, file cache, and environment out of the next test.

    The environment half is not paranoia: :func:`config.apply_cli_overrides`
    writes through to ``os.environ`` on purpose (it is the only way an override
    reaches a Ray actor, which snapshots the environment at creation), and a
    direct assignment is invisible to ``monkeypatch``'s undo. Without this,
    ``--sandbox-mode docker`` set by one test here leaks into every later test in
    the same xdist worker, which then tries to reach a Docker daemon that isn't
    running and fails somewhere entirely unrelated.
    """
    saved = {key.env: os.environ.get(key.env) for key in config.SCHEMA}
    config.clear_cli_overrides()
    config.reset_config_cache()
    yield
    config.clear_cli_overrides()
    config.reset_config_cache()
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _write(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    path = tmp_path / "config.yml"
    path.write_text(body, encoding="utf-8")
    return path


# --- the schema itself ----------------------------------------------------


def test_every_key_is_uniquely_addressable() -> None:
    """A duplicate would make one of the two silently unreachable."""
    for attribute in ("path", "env", "flag", "yaml_key"):
        seen: dict[str, str] = {}
        for key in config.SCHEMA:
            value = getattr(key, attribute)
            if attribute == "yaml_key":
                value = f"{key.section}.{value}"
            assert value not in seen, (
                f"{attribute} {value!r} is claimed by both {seen.get(value)} and {key.path}"
            )
            seen[value] = key.path


def test_every_env_var_keeps_the_sis_prefix() -> None:
    # Back-compat is the whole reason the env layer still exists: a run that
    # exported SIS_BUDGET_USD before this module landed must keep working.
    assert all(key.env.startswith("SIS_") for key in config.SCHEMA)


def test_no_credential_is_declared_as_configuration() -> None:
    """``config.yml`` is committed, so anything in it is public by construction.

    Secrets live in the gitignored ``secrets.local.yml`` behind
    :mod:`sis.settings`, which masks them in ``repr()``. A token that drifted
    into this schema would be committed in plaintext by the very next
    ``--write``.
    """
    forbidden_words = ("token", "secret_key", "password", "api_key", "credential")
    for key in config.SCHEMA:
        assert not any(word in key.path.lower() for word in forbidden_words), key.path
    # aws_secret_id is an *identifier* naming a secret, not the secret itself —
    # the value it points at is fetched from Secrets Manager at run time.
    assert config.key_for("adapters.aws_secret_id").kind is Kind.OPT_STR


def test_the_two_tier_enums_stay_in_step() -> None:
    """``ConfigTier`` and ``policy.ChangeTier`` answer different questions.

    One is "may a human edit this value in the UI", the other is "may the loop
    rewrite this file". They are deliberately separate types — importing one
    into the other would be a cycle, since policy reads its target paths from
    config — but the three names are a shared vocabulary, and a tier added to
    one and not the other would silently mean nothing on the far side.
    """
    assert {t.name for t in ConfigTier} == {t.name for t in ChangeTier}
    assert {t.value for t in ConfigTier} == {t.value for t in ChangeTier}


# --- the committed file ---------------------------------------------------


def test_the_committed_config_declares_every_key() -> None:
    """The file a human reads describes every knob the code has."""
    from_file = config.load_file(config.CONFIG_FILE)
    assert from_file, "config.yml parsed to nothing — is it committed?"
    for key in config.SCHEMA:
        assert key.path in from_file, f"{key.path} is missing from config.yml"


def test_no_guardrail_key_drifts_from_its_built_in_default() -> None:
    """Deleting ``config.yml`` must never weaken a guardrail.

    Values here may legitimately differ from their defaults now that the
    operator UI writes to this file (OMNI-28) — but only for ``soft_`` and
    ``strict_`` keys. Every ``forbidden_`` key must still match ``SCHEMA``, so
    a checkout with no ``config.yml`` at all falls back to exactly the spend
    cap, sandbox mode, target list and episodic backend that shipped.

    Changing a guardrail default is therefore a reviewed change to ``SCHEMA``,
    not a quiet value edit in a generated file; a one-off run that needs a
    different brake uses the environment layer, as ``sis/config.py`` says.
    """
    from_file = config.load_file(config.CONFIG_FILE)
    for key in config.SCHEMA:
        if key.tier is ConfigTier.FORBIDDEN:
            assert from_file[key.path] == key.default, (
                f"{key.path} is forbidden_ but config.yml disagrees with its "
                f"built-in default ({from_file[key.path]!r} vs {key.default!r})"
            )


def test_the_committed_file_is_exactly_what_the_renderer_produces() -> None:
    # Re-rendered from the file's *own* values, so an operator-set value is not
    # drift — but an added or removed key, a stale doc comment, a wrong tier
    # prefix or reordering still is, and is caught here rather than surfacing as
    # a confusing parse error much later.
    rendered = config.render_config_file(config.load_file(config.CONFIG_FILE))
    assert config.CONFIG_FILE.read_text(encoding="utf-8") == rendered


def test_every_key_is_documented_and_tier_prefixed() -> None:
    for key in config.SCHEMA:
        assert key.doc.strip(), f"{key.path} has no doc string for the operator UI"
        assert key.yaml_key.startswith(f"{key.tier.value}_")


# --- precedence -----------------------------------------------------------


def test_the_default_layer_applies_when_nothing_else_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "_FILE_CACHE", {})
    monkeypatch.delenv("SIS_BUDGET_USD", raising=False)
    resolved = config.resolve(config.key_for("brakes.budget_usd"))
    assert resolved.value == 5.0
    assert resolved.source is Source.DEFAULT


def test_the_file_layer_beats_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_FILE_CACHE", {"brakes.budget_usd": 1.0})
    monkeypatch.delenv("SIS_BUDGET_USD", raising=False)
    resolved = config.resolve(config.key_for("brakes.budget_usd"))
    assert (resolved.value, resolved.source) == (1.0, Source.FILE)


def test_the_env_layer_beats_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_FILE_CACHE", {"brakes.budget_usd": 1.0})
    monkeypatch.setenv("SIS_BUDGET_USD", "0.25")
    resolved = config.resolve(config.key_for("brakes.budget_usd"))
    assert (resolved.value, resolved.source) == (0.25, Source.ENV)


def test_the_cli_layer_beats_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_FILE_CACHE", {"brakes.budget_usd": 1.0})
    monkeypatch.setenv("SIS_BUDGET_USD", "0.25")
    config.apply_cli_overrides(config.parse_cli(["--brakes-budget-usd", "0.05"]))
    resolved = config.resolve(config.key_for("brakes.budget_usd"))
    assert (resolved.value, resolved.source) == (0.05, Source.CLI)


def test_an_empty_env_var_does_not_count_as_set(monkeypatch: pytest.MonkeyPatch) -> None:
    # `SIS_BUDGET_USD=` in a shell profile is an operator clearing the variable,
    # not an operator asking for a budget of "".
    monkeypatch.setattr(config, "_FILE_CACHE", {})
    monkeypatch.setenv("SIS_BUDGET_USD", "")
    assert config.resolve(config.key_for("brakes.budget_usd")).source is Source.DEFAULT


def test_effective_reports_every_key_with_its_source() -> None:
    report = config.effective()
    assert len(report) == len(config.SCHEMA)
    assert all(isinstance(item.source, Source) for item in report)
    assert {item.key.path for item in report} == {k.path for k in config.SCHEMA}


# --- the CLI --------------------------------------------------------------


def test_a_flag_reaches_the_ray_actors_via_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subtle half of the CLI layer, and the reason it writes to os.environ.

    Role actors are detached Ray actors in their own OS processes: they inherit
    the driver's environment when they are *created* and never see a later
    change. An override held only in this module's memory would configure the
    driver and leave the actor that actually runs the gauntlet on the old value
    — i.e. ``--sandbox-mode docker`` would be a lie. Same trap as
    ``SIS_CONTRACT``; here it is closed rather than documented.
    """
    monkeypatch.delenv("SIS_SANDBOX", raising=False)
    config.apply_cli_overrides(config.parse_cli(["--sandbox-mode", "docker"]))
    assert os.environ["SIS_SANDBOX"] == "docker"


def test_the_older_flag_spellings_still_work() -> None:
    parsed = config.parse_cli(["--contract", "sort", "--canary", "serve"])
    assert parsed == {"contracts.default": "sort", "canary.backend": "serve"}


def test_a_flag_with_no_value_fails_instead_of_eating_the_next_flag() -> None:
    # The behaviour main.py's hand-rolled parser had, and worth keeping: the
    # alternative is `--contract --loop` silently selecting a contract named
    # "--loop" and failing much later with a confusing message.
    with pytest.raises(SystemExit, match="--contract"):
        config.parse_cli(["--contract"])


def test_unknown_arguments_are_left_for_the_caller() -> None:
    # main.py owns mode flags like --loop; the config parser must not claim them.
    assert config.parse_cli(["--loop", "--show-config"]) == {}


# --- validation -----------------------------------------------------------


def test_a_mistyped_sandbox_mode_is_rejected_rather_than_silently_downgraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nastiest failure this schema closes.

    ``SIS_SANDBOX=dcoker`` used to compare unequal to ``"docker"`` everywhere it
    was read, so the engine ran untrusted generated code in the *soft* subprocess
    sandbox and said nothing at all. Typos in a security control must be loud.
    """
    monkeypatch.setenv("SIS_SANDBOX", "dcoker")
    with pytest.raises(ValueError, match="SIS_SANDBOX"):
        gauntlet.sandbox_mode()


def test_a_bad_value_names_the_layer_the_operator_actually_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point at the thing to fix, not at an internal path nobody typed."""
    monkeypatch.setenv("SIS_BUDGET_USD", "0.1O")  # letter O, not zero
    with pytest.raises(ValueError, match="SIS_BUDGET_USD"):
        config.get("brakes.budget_usd")

    monkeypatch.delenv("SIS_BUDGET_USD")
    config.apply_cli_overrides({})
    with pytest.raises(ValueError, match=r"--brakes-budget-usd"):
        config.parse_cli(["--brakes-budget-usd", "0.1O"])


def test_a_negative_or_zero_value_is_rejected_where_zero_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIS_BUDGET_USD", "-1")
    with pytest.raises(ValueError, match="must not be negative"):
        config.get("brakes.budget_usd")
    # A zero budget is a legitimate (if useless) setting; a zero gate timeout
    # fails every gate instantly, which is not a weaker setting but a broken one.
    monkeypatch.setenv("SIS_GAUNTLET_TIMEOUT", "0")
    with pytest.raises(ValueError, match="must be positive"):
        config.get("sandbox.timeout_seconds")


def test_a_non_boolean_flag_value_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SIS_ALLOW_STRICT_CHANGES=yes`` used to mean "disabled", silently.

    The old test was ``== "1"``, so every other spelling failed safe — but
    invisibly, which is its own hazard: the operator believes they enabled
    something and nothing contradicts them.
    """
    monkeypatch.setenv("SIS_ALLOW_STRICT_CHANGES", "maybe")
    with pytest.raises(ValueError, match="not a boolean"):
        config.get("policy.allow_strict_changes")

    for spelling in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("SIS_ALLOW_STRICT_CHANGES", spelling)
        assert config.get("policy.allow_strict_changes") is True
    for spelling in ("0", "false", "no", "off"):
        monkeypatch.setenv("SIS_ALLOW_STRICT_CHANGES", spelling)
        assert config.get("policy.allow_strict_changes") is False


def test_an_unknown_key_in_the_file_is_rejected(tmp_path: pathlib.Path) -> None:
    # A typo'd `forbidden_budget_used` that parsed to nothing would be a spend
    # cap the operator believes they set and the engine never sees.
    path = _write(tmp_path, "brakes:\n  forbidden_budget_used: 0.05\n")
    with pytest.raises(ValueError, match="unknown key brakes.forbidden_budget_used"):
        config.load_file(path)


def test_an_unknown_section_in_the_file_is_rejected(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, "brakez:\n  forbidden_budget_usd: 0.05\n")
    with pytest.raises(ValueError, match="unknown section"):
        config.load_file(path)


def test_relabelling_a_keys_tier_in_the_file_is_rejected(tmp_path: pathlib.Path) -> None:
    """The tier is declared in code; the file may not renegotiate it.

    Otherwise the prefix is decoration: an operator (or a UI bug) could demote
    ``forbidden_budget_usd`` to ``soft_budget_usd`` and hand themselves
    permission to edit the spend cap freely, by editing the very file whose
    protection level is in question.
    """
    path = _write(tmp_path, "brakes:\n  soft_budget_usd: 999.0\n")
    with pytest.raises(ValueError, match="wrong tier prefix"):
        config.load_file(path)


def test_a_missing_file_is_not_an_error(tmp_path: pathlib.Path) -> None:
    # A fresh clone, or a deployment shipping only the package, falls back to
    # the built-in defaults rather than refusing to start.
    assert config.load_file(tmp_path / "nope.yml") == {}


# --- the guardrail: config cannot unprotect config ------------------------


def test_the_loop_may_never_write_the_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every ``forbidden_`` key is unreachable from the code-generation path.

    Not asserted key by key, because per-key protection is not the mechanism:
    a value can only be changed by writing one of exactly two files, and both
    are FORBIDDEN with no override. That is a stronger statement than a
    per-key allowlist, and it does not decay when a key is added.
    """
    for path in ("config.yml", "sis/config.py"):
        assert classify(path) is ChangeTier.FORBIDDEN, path
        decision = authorize_change(
            path, checks_passed=True, approved=True,
            justification=Justification.HUMAN_REQUEST,
        )
        assert not decision.allowed
        assert "no override" in decision.reason

    monkeypatch.setattr(config, "_FILE_CACHE", {})
    assert {key.tier for key in config.SCHEMA} >= {ConfigTier.FORBIDDEN}


def test_config_cannot_be_used_to_widen_the_soft_tier_over_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The self-referential attack, and the reason guardrail precedence is absolute.

    ``policy.target_paths`` is itself a config key, so the obvious escalation is
    to name the config as an optimisation target and let the loop rewrite its own
    brakes. ``classify`` checks the guardrail list *before* the target list
    precisely so that this is inert.
    """
    monkeypatch.setenv(
        "SIS_TARGET_PATHS", "config.yml,sis/config.py,sis/policy.py,sis/gauntlet.py"
    )
    assert "config.yml" in policy.target_paths()  # the setting does take effect...
    for path in ("config.yml", "sis/config.py", "sis/policy.py", "sis/gauntlet.py"):
        assert classify(path) is ChangeTier.FORBIDDEN, path  # ...and buys nothing


def test_an_empty_target_list_falls_back_rather_than_optimising_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIS_TARGET_PATHS", "")
    assert policy.target_paths() == config.DEFAULT_TARGET_PATHS


# --- the wiring: every read site actually goes through the schema ---------


def test_the_engine_reads_no_sis_variable_outside_the_schema() -> None:
    """One schema means one reader. This is what stops the next ad-hoc getenv.

    Three ``os.environ`` uses in ``sis/`` are legitimately not configuration and
    are allowed by name: the gauntlet's sandbox allowlist, the Serve replica's
    credential scrub, and the secrets scanner — all three consume the whole
    environment rather than reading a named knob.
    """
    import ast

    from sis.paths import PROJECT_ROOT

    allowed = {"sis/config.py", "sis/gauntlet.py", "sis/serving.py", "sis/settings.py"}
    offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "sis").glob("*.py")):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if rel in allowed:
            continue
        # Parsed, not grepped: a prose mention of "the environment" in a
        # docstring is not a read, and the first version of this test said it was.
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            if node.value.id == "os" and node.attr in ("getenv", "environ"):
                offenders.append(f"{rel}:{node.lineno} os.{node.attr}")
    assert not offenders, (
        "these read the environment directly instead of through sis.config: "
        + "; ".join(offenders)
    )


def test_the_brake_defaults_are_not_restated_outside_the_schema() -> None:
    """``roles`` used to spell the same four numbers the README documented."""
    from sis import roles

    assert roles.DEFAULT_BUDGET_USD == config.key_for("brakes.budget_usd").default
    assert roles.DEFAULT_BREAKER_THRESHOLD == config.key_for(
        "brakes.breaker_threshold").default
    assert roles.ceo_config_from_env({}).budget_usd == config.key_for(
        "brakes.budget_usd").default


def test_ceo_brakes_resolve_through_the_whole_precedence_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sis import roles

    monkeypatch.setattr(config, "_FILE_CACHE", {"brakes.budget_usd": 2.0})
    # An explicit mapping is still the unit-testable path, and it is the *env*
    # layer — so the file underneath it still shows through for keys it omits.
    cfg = roles.ceo_config_from_env({"SIS_BREAKER_THRESHOLD": "1"})
    assert cfg.breaker_threshold == 1
    assert cfg.budget_usd == 2.0


def test_every_env_var_named_in_the_docs_exists_in_the_schema() -> None:
    """A variable documented but absent from ``SCHEMA`` is fiction.

    Nothing reads it, so a reader who exports it gets silence — the exact
    failure OMNI-27 was filed for, where README's table had drifted from the
    code it described. Checking every markdown file, not just README, because
    the same table existed twice: `docs/RUNBOOK.md` had its own copy, which had
    drifted differently.
    """
    import re

    from sis.paths import PROJECT_ROOT

    declared = {key.env for key in config.SCHEMA}
    # Secrets and test-only variables are documented elsewhere and are
    # deliberately not configuration; see test_no_credential_is_declared_as_configuration.
    exempt = {
        "SIS_ATLASSIAN_BASE_URL", "SIS_ATLASSIAN_EMAIL", "SIS_ATLASSIAN_API_TOKEN",
        "SIS_GITHUB_TOKEN", "SIS_FAKE_SECRET", "SIS_TEST_FAKE_CREDENTIAL",
    }
    skip_dirs = {".venv", "node_modules", ".git"}
    undocumented: dict[str, str] = {}
    for path in sorted(PROJECT_ROOT.rglob("*.md")):
        if any(part in skip_dirs for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for name in re.findall(r"\bSIS_[A-Z_]+\b", text):
            if name not in declared and name not in exempt:
                undocumented[name] = path.relative_to(PROJECT_ROOT).as_posix()
    assert not undocumented, (
        "these are named in the docs but no longer exist in the schema: "
        + ", ".join(f"{name} ({where})" for name, where in sorted(undocumented.items()))
    )


# --- types ----------------------------------------------------------------


def test_the_typed_view_covers_every_section() -> None:
    built = config.config()
    for section in config.SECTIONS:
        assert hasattr(built, section), f"Config has no {section!r} attribute"
    # And every key is a field on its section, so nothing is declared and dropped.
    for key in config.SCHEMA:
        assert hasattr(getattr(built, key.section), key.name), key.path


def test_optional_keys_round_trip_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_FILE_CACHE", {})
    for name in ("canary.backend", "loop.max_cycles", "contracts.default"):
        monkeypatch.delenv(config.key_for(name).env, raising=False)
        assert config.get(name) is None


def test_a_key_can_be_looked_up_and_an_unknown_one_raises() -> None:
    assert isinstance(config.key_for("sandbox.mode"), Key)
    with pytest.raises(KeyError, match="unknown config key"):
        config.key_for("sandbox.mode_typo")
