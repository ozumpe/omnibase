"""scripts/aws_secret.py — put the AWS run's credentials into Secrets Manager.

OMNI-29 (docs/AWS_RUN.md). The run box reads one JSON secret: the content of
``secrets.local.yml`` plus the Anthropic key it exports at run time. This
builds that document from the local file and ``$ANTHROPIC_API_KEY`` and
uploads it straight to Secrets Manager — no plaintext ``secrets.aws.json`` on
disk, and no credential value ever printed.

Usage:
    poetry run python scripts/aws_secret.py            # dry run: where it would route
    poetry run python scripts/aws_secret.py --upload   # after `tofu apply` created the secret

Dry run is the default because an upload replaces the secret's current value.
Both modes refuse a secrets file that routes the loop at the real planning
board or the engine's own repo: a run files real Jira issues, Confluence pages
and GitHub PRs, and those belong in the scratch tenant (``TES`` +
``ozumpe/testrun``), never in ``OMNI`` or ``ozumpe/omnibase``.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import sys
from typing import Any

# Allow running as a plain script: put the repo root (parent of scripts/) on the path.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sis.settings import (  # noqa: E402
    FileSecretSource,
    Settings,
    load_settings,
    space_keys,
    version_control_base,
)

DEFAULT_SECRET_ID = "sis/first-run/credentials"  # infra/aws variables.tf secret_name
DEFAULT_REGION = "us-east-1"

# Where a run must never file artifacts. The planning board is where humans
# track the engine's own work; the engine repo is the code under development.
PROTECTED_JIRA_PROJECTS = frozenset({"OMNI"})
PROTECTED_REPOS = frozenset({"ozumpe/omnibase"})


def build_payload(raw: dict[str, Any], anthropic_key: str) -> dict[str, Any]:
    """The secret document: the local secrets plus ``anthropic.api_key``.

    Nested under ``anthropic`` because that is where the runbook's
    ``jq -r .anthropic.api_key`` reads it; ``sis.settings`` ignores the key.
    """
    payload = copy.deepcopy(raw)
    section = payload.get("anthropic")
    payload["anthropic"] = {**(section if isinstance(section, dict) else {}),
                            "api_key": anthropic_key}
    return payload


def existing_anthropic_key(raw: dict[str, Any]) -> str:
    """An ``anthropic.api_key`` already carried in the secrets file, or ''."""
    section = raw.get("anthropic")
    if isinstance(section, dict):
        return str(section.get("api_key") or "")
    return str(raw.get("anthropic_api_key") or "")


def routing_problems(settings: Settings) -> list[str]:
    """Why these settings must not be uploaded for a run, or [] if they may."""
    problems: list[str] = []
    if settings.atlassian is None:
        problems.append("no Atlassian credentials (atlassian.base_url + api_token)")
    elif settings.atlassian.jira_project.upper() in PROTECTED_JIRA_PROJECTS:
        problems.append(
            f"jira_project is {settings.atlassian.jira_project!r} — the run would file "
            "real issues on the planning board; point it at the scratch project (TES)"
        )
    if settings.github is None:
        problems.append("no GitHub credentials (github.token)")
    else:
        repo = f"{settings.github.owner}/{settings.github.repo}".lower()
        if repo in PROTECTED_REPOS:
            problems.append(
                f"github repo is {repo!r} — the run would open PRs against the engine "
                "itself; point it at the throwaway repo (ozumpe/testrun)"
            )
    return problems


def describe(settings: Settings, anthropic_key: str) -> list[str]:
    """Human-readable, value-free summary: where the run routes, what is set."""
    lines = ["Routing (not secret):"]
    if settings.atlassian is not None:
        spaces = ", ".join(sorted(set(space_keys(settings).values())))
        lines.append(f"  Jira project     {settings.atlassian.jira_project}")
        lines.append(f"  Confluence       {settings.atlassian.base_url}  spaces: {spaces}")
    if settings.github is not None:
        lines.append(f"  GitHub           {settings.github.owner}/{settings.github.repo}"
                     f"  (base branch {version_control_base(settings)})")
    lines.append("Credentials (presence only, never values):")
    for name, present in (
        ("Atlassian API token", settings.atlassian is not None
         and bool(settings.atlassian.api_token)),
        ("GitHub token", settings.github is not None and bool(settings.github.token)),
        ("Anthropic API key", bool(anthropic_key)),
    ):
        lines.append(f"  {'✓' if present else '✗'} {name}")
    return lines


def upload(payload: dict[str, Any], secret_id: str, region: str) -> str:
    """put-secret-value; returns the new version id. The value never touches disk."""
    import boto3  # lazy: `poetry install --with real`

    client = boto3.client("secretsmanager", region_name=region)
    response = client.put_secret_value(SecretId=secret_id, SecretString=json.dumps(payload))
    return str(response.get("VersionId", "?"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Put the AWS run's credentials into Secrets Manager (OMNI-29).")
    parser.add_argument("--upload", action="store_true",
                        help="actually put the value (default: dry run)")
    parser.add_argument("--secrets-file", default=str(REPO_ROOT / "secrets.local.yml"))
    parser.add_argument("--secret-id", default=DEFAULT_SECRET_ID)
    parser.add_argument("--region", default=DEFAULT_REGION)
    args = parser.parse_args(argv)

    path = pathlib.Path(args.secrets_file)
    if not path.exists():
        print(f"✗ {path} not found — copy secrets.example.yml and fill it in first.")
        return 1
    raw = FileSecretSource(path).load()
    settings = load_settings(FileSecretSource(path))
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "") or existing_anthropic_key(raw)

    print("\n".join(describe(settings, anthropic_key)))
    problems = routing_problems(settings)
    if problems:
        print("\n✗ Refusing: " + "; ".join(problems))
        return 1
    if not args.upload:
        print(f"\nDry run. With --upload this becomes {args.secret_id!r} in {args.region}."
              + ("" if anthropic_key else "\nExport ANTHROPIC_API_KEY first."))
        return 0
    if not anthropic_key:
        print("\n✗ Refusing: ANTHROPIC_API_KEY is not set (the run's proposer needs it).")
        return 1
    version = upload(build_payload(raw, anthropic_key), args.secret_id, args.region)
    print(f"\n✓ Uploaded {args.secret_id!r} in {args.region} (version {version}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
