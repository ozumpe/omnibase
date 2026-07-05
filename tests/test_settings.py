"""Tests for the secrets/config layer (no network, no Ray)."""

import json

import pytest

from sis.settings import (
    DEFAULT_SPACES,
    AtlassianSettings,
    EnvSecretSource,
    FileSecretSource,
    _build_settings,
    load_settings,
    settings_summary,
    space_keys,
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


def test_space_keys_come_from_atlassian_settings_when_configured() -> None:
    # H1 regression: the roles must write to the *configured* spaces, not the
    # hardcoded "PROPOSALS"/"SPECS"/"CHARTER" constants that crash a real tenant.
    settings = _build_settings("local", "real", {
        "atlassian_base_url": "https://x", "atlassian_api_token": "t",
        "atlassian_proposal_space": "INTAKE", "atlassian_spec_space": "DESIGN",
        "atlassian_charter_space": "GOV",
    })
    assert space_keys(settings) == {"proposal": "INTAKE", "spec": "DESIGN", "charter": "GOV"}
