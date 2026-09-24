"""Tests for scripts/aws_secret.py — the OMNI-29 credentials upload (no AWS calls)."""

import importlib.util
import json
import pathlib
import sys
from types import ModuleType
from typing import Any

import pytest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "aws_secret.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("aws_secret", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["aws_secret"] = module
    spec.loader.exec_module(module)
    return module


aws_secret = _load()

_TOKENS = ("ATATT-atlassian-secret-value", "ghp_github-secret-value", "sk-ant-anthropic-secret")


def _secrets(tmp_path: pathlib.Path, *, jira: str = "TES", repo: str = "testrun",
             extra: dict[str, Any] | None = None) -> pathlib.Path:
    data: dict[str, Any] = {
        "atlassian": {"base_url": "https://x.atlassian.net", "email": "a@b.c",
                      "api_token": _TOKENS[0], "jira_project": jira},
        "github": {"token": _TOKENS[1], "owner": "ozumpe", "repo": repo},
    }
    data.update(extra or {})
    path = tmp_path / "secrets.local.yml"
    path.write_text(json.dumps(data), encoding="utf-8")  # JSON is valid YAML
    return path


@pytest.fixture
def no_upload(monkeypatch):  # type: ignore[no-untyped-def]
    """Fail the test if anything tries to reach Secrets Manager."""
    calls: list[tuple[dict[str, Any], str, str]] = []

    def fake(payload: dict[str, Any], secret_id: str, region: str) -> str:
        calls.append((payload, secret_id, region))
        return "v-test"

    monkeypatch.setattr(aws_secret, "upload", fake)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return calls


def test_payload_nests_the_anthropic_key_without_mutating_the_input() -> None:
    raw = {"github": {"token": "t"}, "anthropic": {"model_hint": "keep me"}}
    payload = aws_secret.build_payload(raw, "sk-ant-x")
    # Where the runbook's `jq -r .anthropic.api_key` reads it on the box.
    assert payload["anthropic"] == {"model_hint": "keep me", "api_key": "sk-ant-x"}
    assert payload["github"] == {"token": "t"}
    assert "api_key" not in raw["anthropic"]  # the local file's dict is untouched


def test_an_anthropic_key_already_in_the_file_is_found_nested_or_flat() -> None:
    assert aws_secret.existing_anthropic_key({"anthropic": {"api_key": "k1"}}) == "k1"
    assert aws_secret.existing_anthropic_key({"anthropic_api_key": "k2"}) == "k2"
    assert aws_secret.existing_anthropic_key({}) == ""


@pytest.mark.parametrize(("jira", "repo", "needle"), [
    ("OMNI", "testrun", "planning board"),
    ("omni", "testrun", "planning board"),       # case-insensitive
    ("TES", "omnibase", "engine itself"),
    ("TES", "OmniBase", "engine itself"),
])
def test_routing_at_the_real_board_or_engine_repo_is_refused(
    tmp_path: pathlib.Path, no_upload: list[Any], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, jira: str, repo: str, needle: str,
) -> None:
    # A run files real issues and PRs; they belong in the scratch tenant.
    monkeypatch.setenv("ANTHROPIC_API_KEY", _TOKENS[2])
    path = _secrets(tmp_path, jira=jira, repo=repo)
    assert aws_secret.main(["--upload", "--secrets-file", str(path)]) == 1
    assert needle in capsys.readouterr().out
    assert no_upload == []  # refused before any AWS call


def test_dry_run_is_the_default_and_prints_no_credential(
    tmp_path: pathlib.Path, no_upload: list[Any], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", _TOKENS[2])
    assert aws_secret.main(["--secrets-file", str(_secrets(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "TES" in out and "ozumpe/testrun" in out   # routing is shown…
    assert not any(token in out for token in _TOKENS)  # …credentials never are
    assert no_upload == []


def test_upload_requires_an_anthropic_key(
    tmp_path: pathlib.Path, no_upload: list[Any], capsys: pytest.CaptureFixture[str],
) -> None:
    assert aws_secret.main(["--upload", "--secrets-file", str(_secrets(tmp_path))]) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out
    assert no_upload == []


def test_upload_sends_the_file_plus_the_key_and_prints_no_credential(
    tmp_path: pathlib.Path, no_upload: list[Any], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", _TOKENS[2])
    path = _secrets(tmp_path)
    assert aws_secret.main(["--upload", "--secrets-file", str(path),
                            "--secret-id", "sis/x", "--region", "eu-west-1"]) == 0
    [(payload, secret_id, region)] = no_upload
    assert (secret_id, region) == ("sis/x", "eu-west-1")
    assert payload["anthropic"]["api_key"] == _TOKENS[2]
    assert payload["atlassian"]["jira_project"] == "TES"
    assert not any(token in capsys.readouterr().out for token in _TOKENS)


def test_missing_secrets_file_is_reported(
    tmp_path: pathlib.Path, no_upload: list[Any], capsys: pytest.CaptureFixture[str],
) -> None:
    assert aws_secret.main(["--secrets-file", str(tmp_path / "nope.yml")]) == 1
    assert "not found" in capsys.readouterr().out
