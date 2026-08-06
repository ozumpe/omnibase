"""Tests for the secrets/config layer (no network, no Ray)."""

import json
import pathlib
from typing import Any

import pytest

from sis import settings as settings_mod
from sis.settings import (
    DEFAULT_SPACES,
    AtlassianSettings,
    EnvSecretSource,
    FileSecretSource,
    _build_settings,
    load_settings,
    reset_settings_cache,
    settings_summary,
    space_keys,
    version_control_base,
)


def test_file_source_parses_json_when_yaml_absent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # YAML is a JSON superset; JSON content always parses even without pyyaml.
    p = tmp_path / "secrets.local.yml"
    p.write_text(json.dumps({"atlassian": {"base_url": "https://x.atlassian.net",
                                           "email": "a@b.c", "api_token": "ATATTsecret"}}))
    settings = load_settings(FileSecretSource(p))
    assert settings.atlassian is not None
    assert settings.atlassian.base_url == "https://x.atlassian.net"


def test_nested_and_flat_keys_both_work() -> None:
    nested = _build_settings("local", "real",
                             {"atlassian": {"base_url": "https://x", "api_token": "t1"}})
    flat = _build_settings("local", "real",
                           {"atlassian_base_url": "https://x", "atlassian_api_token": "t1"})
    assert nested.atlassian == flat.atlassian


def test_api_token_is_masked_in_repr() -> None:
    s = AtlassianSettings(base_url="https://x", email="a@b.c", api_token="ATATTsupersecret9999")
    text = repr(s)
    assert "ATATTsupersecret9999" not in text
    assert "9999" in text  # last 4 shown for identification
    assert "redacted" in text


def test_env_source_maps_prefixed_vars(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SIS_ATLASSIAN_BASE_URL", "https://env.atlassian.net")
    monkeypatch.setenv("SIS_ATLASSIAN_API_TOKEN", "envtoken")
    monkeypatch.setenv("SIS_GITHUB_TOKEN", "ghtoken")
    monkeypatch.setenv("SIS_ENV", "local")  # control var must be ignored as a secret
    raw = EnvSecretSource().load()
    assert raw["atlassian_base_url"] == "https://env.atlassian.net"
    assert "env" not in raw  # SIS_ENV excluded


def test_summary_reports_config_without_secrets() -> None:
    settings = _build_settings("aws", "real",
                               {"atlassian_base_url": "https://x",
                                "atlassian_api_token": "ATATT-secret-zzz",
                                "github_token": "ghp-secret-zzz", "aws_region": "eu-west-1"})
    summary = settings_summary(settings)
    assert summary == {
        "env": "aws", "adapters": "real",
        "atlassian_configured": True, "github_configured": True,
        "aws_region": "eu-west-1", "credential_fields_masked": ["api_token"],
    }
    blob = json.dumps(summary)
    assert "ATATT-secret-zzz" not in blob and "ghp-secret-zzz" not in blob  # no token leaked


def test_missing_integration_raises_clearly() -> None:
    settings = _build_settings("local", "memory", {})
    with pytest.raises(RuntimeError, match="Atlassian settings missing"):
        settings.require_atlassian()


def test_space_keys_fall_back_to_defaults_without_atlassian() -> None:
    # H1: the in-memory path (no Atlassian config) must keep its own space keys,
    # so the local run stays credential-free and unchanged.
    settings = _build_settings("local", "memory", {})
    assert settings.atlassian is None
    assert space_keys(settings) == DEFAULT_SPACES


def test_shipped_example_template_leaves_aws_unconfigured_for_local_runs() -> None:
    # Regression: secrets.example.yml shipped `secret_id: sis/prod/credentials`.
    # check_connections.py skips AWS only when
    #   env != "aws" and (aws is None or not aws.secret_id)
    # so that non-empty placeholder made a plain `cp`-ed template attempt a real
    # STS call — every first-time Level 2 preflight reported a spurious AWS
    # failure (ModuleNotFoundError: boto3) for a service Level 2 never uses.
    example = pathlib.Path(__file__).resolve().parent.parent / "secrets.example.yml"
    settings = load_settings(FileSecretSource(example))
    assert settings.env != "aws"
    assert not (settings.aws and settings.aws.secret_id), (
        "secrets.example.yml must ship an empty aws.secret_id, otherwise copying "
        "the template falsely marks AWS as configured for local runs"
    )


def test_space_keys_come_from_atlassian_settings_when_configured() -> None:
    # H1 regression: the roles must write to the *configured* spaces, not the
    # hardcoded "PROPOSALS"/"SPECS"/"CHARTER" constants that crash a real tenant.
    settings = _build_settings("local", "real", {
        "atlassian_base_url": "https://x", "atlassian_api_token": "t",
        "atlassian_proposal_space": "INTAKE", "atlassian_spec_space": "DESIGN",
        "atlassian_charter_space": "GOV",
    })
    assert space_keys(settings) == {"proposal": "INTAKE", "spec": "DESIGN", "charter": "GOV"}


def test_version_control_base_falls_back_to_main_without_github() -> None:
    # In-memory path (no GitHub config) forks from "main", unchanged.
    settings = _build_settings("local", "memory", {})
    assert settings.github is None
    assert version_control_base(settings) == "main"


def test_hot_accessors_read_the_secret_source_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Regression (2026-07-28 minor list): space_keys()/version_control_base() are
    # called several times per cycle from inside the Ray actors, and each call
    # re-read and re-parsed the whole secrets source — a redundant file read
    # locally, and a redundant Secrets Manager round-trip under SIS_ENV=aws.
    loads = 0

    class _CountingSource:
        def load(self) -> dict[str, Any]:
            nonlocal loads
            loads += 1
            return {}

    monkeypatch.setattr(settings_mod, "_select_source", lambda: _CountingSource())
    reset_settings_cache()
    try:
        space_keys()
        space_keys()
        version_control_base()
        assert loads == 1
    finally:
        # Never leave a cache built from the fake source behind for other tests.
        reset_settings_cache()


def test_explicit_source_always_reloads(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Only the no-argument path is cached: load_settings(source) must still read
    # every time, or tests and check_connections.py would silently get whatever
    # the first caller in the process happened to load.
    loads = 0

    class _CountingSource:
        def load(self) -> dict[str, Any]:
            nonlocal loads
            loads += 1
            return {}

    source = _CountingSource()
    load_settings(source)
    load_settings(source)
    assert loads == 2


def test_version_control_base_comes_from_github_settings() -> None:
    # M4 regression: the loop must fork feature branches from the configured
    # default base — the same branch live_target_source reads the merged target
    # from — not a hardcoded "main".
    settings = _build_settings("local", "real", {
        "github_token": "t", "github_owner": "o", "github_repo": "r",
        "github_default_base": "develop",
    })
    assert version_control_base(settings) == "develop"
