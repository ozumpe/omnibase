"""sis.settings — configuration & secrets loading, environment-portable.

The same code path works in two deployments, chosen by ``SIS_ENV``:

- ``local`` (default): secrets come from a **gitignored** YAML file
  (``secrets.local.yml`` by default, override with ``SIS_SECRETS_FILE``),
  falling back to ``SIS_*`` environment variables if the file is absent.
- ``aws``: secrets come from **AWS Secrets Manager** (the secret id is
  ``SIS_AWS_SECRET_ID``; the region is ``SIS_AWS_REGION`` or the AWS default).

Whichever source is used, it yields the same flat dict of keys, which is
parsed into typed, frozen settings objects. Credential fields are
**masked in repr()** so tokens never leak into logs or tracebacks.

Nothing here imports ``yaml`` or ``boto3`` at module load — those are
optional (``poetry install --with real``) and imported lazily, so local
in-memory runs need neither.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Protocol

DEFAULT_SECRETS_FILE = "secrets.local.yml"
_MASK = "***redacted***"


def _mask(value: str) -> str:
    """Show only the last 4 chars of a secret, masking the rest."""
    if not value:
        return ""
    return f"{_MASK}{value[-4:]}" if len(value) > 4 else _MASK


# --------------------------------------------------------------------------
# Typed settings (frozen; secrets masked in repr)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AtlassianSettings:
    base_url: str  # e.g. https://olafzumpe.atlassian.net
    email: str
    api_token: str = field(repr=False)
    cloud_id: str | None = None
    spec_space: str = "SD"
    proposal_space: str = "PROPOSALS"
    charter_space: str = "SD"  # top-level charter (CEO.set_charter) lives here
    jira_project: str = "SD"  # Jira project key for created epics/stories

    def __repr__(self) -> str:
        return (f"AtlassianSettings(base_url={self.base_url!r}, email={self.email!r}, "
                f"api_token={_mask(self.api_token)!r}, cloud_id={self.cloud_id!r})")


@dataclass(frozen=True)
class GitHubSettings:
    token: str = field(repr=False)
    owner: str = ""
    repo: str = ""
    default_base: str = "main"

    def __repr__(self) -> str:
        return (f"GitHubSettings(owner={self.owner!r}, repo={self.repo!r}, "
                f"token={_mask(self.token)!r}, default_base={self.default_base!r})")


@dataclass(frozen=True)
class AwsSettings:
    region: str = "us-east-1"
    secret_id: str | None = None


@dataclass(frozen=True)
class Settings:
    env: str = "local"
    adapters: str = "memory"  # "memory" | "real"
    atlassian: AtlassianSettings | None = None
    github: GitHubSettings | None = None
    aws: AwsSettings | None = None

    def require_atlassian(self) -> AtlassianSettings:
        if self.atlassian is None:
            raise RuntimeError("Atlassian settings missing — check your secrets source")
        return self.atlassian

    def require_github(self) -> GitHubSettings:
        if self.github is None:
            raise RuntimeError("GitHub settings missing — check your secrets source")
        return self.github


# --------------------------------------------------------------------------
# Secret sources
# --------------------------------------------------------------------------


class SecretSource(Protocol):
    def load(self) -> dict[str, Any]: ...


class FileSecretSource:
    """Reads a flat secrets mapping from a YAML (or JSON) file on disk."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        text = self.path.read_text(encoding="utf-8")
        try:
            import yaml  # lazy: only needed for the file source
        except ModuleNotFoundError:
            return dict(json.loads(text))  # YAML is a superset; plain JSON still parses
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError(f"{self.path} must contain a top-level mapping")
        return dict(data)


class EnvSecretSource:
    """Reads secrets from ``SIS_*`` environment variables.

    Maps e.g. ``SIS_ATLASSIAN_API_TOKEN`` → ``atlassian_api_token``.
    """

    PREFIX = "SIS_"

    def load(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in os.environ.items():
            if key.startswith(self.PREFIX) and key not in {"SIS_ENV", "SIS_ADAPTERS",
                                                            "SIS_SECRETS_FILE",
                                                            "SIS_AWS_SECRET_ID",
                                                            "SIS_AWS_REGION"}:
                out[key[len(self.PREFIX):].lower()] = value
        return out


class AwsSecretsManagerSource:
    """Reads a JSON secret blob from AWS Secrets Manager (used in the cloud)."""

    def __init__(self, secret_id: str, region: str | None = None) -> None:
        self.secret_id = secret_id
        self.region = region

    def load(self) -> dict[str, Any]:
        import boto3  # lazy: only needed in the AWS deployment

        client = boto3.client("secretsmanager", region_name=self.region)
        response = client.get_secret_value(SecretId=self.secret_id)
        payload = response.get("SecretString") or "{}"
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("Secrets Manager payload must be a JSON object")
        return dict(data)


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def _build_settings(env: str, adapters: str, raw: dict[str, Any]) -> Settings:
    """Map a flat secrets dict onto the typed settings objects.

    Recognised keys (flat): atlassian_base_url, atlassian_email,
    atlassian_api_token, atlassian_cloud_id, atlassian_spec_space,
    atlassian_proposal_space, github_token, github_owner, github_repo,
    aws_region, aws_secret_id. Nested dicts (``atlassian: {...}``) are also
    accepted and flattened.
    """
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            for subkey, subval in value.items():
                flat[f"{key}_{subkey}"] = subval
        else:
            flat[key] = value

    atlassian: AtlassianSettings | None = None
    if flat.get("atlassian_base_url") and flat.get("atlassian_api_token"):
        atlassian = AtlassianSettings(
            base_url=str(flat["atlassian_base_url"]),
            email=str(flat.get("atlassian_email", "")),
            api_token=str(flat["atlassian_api_token"]),
            cloud_id=_opt_str(flat.get("atlassian_cloud_id")),
            spec_space=str(flat.get("atlassian_spec_space", "SD")),
            proposal_space=str(flat.get("atlassian_proposal_space", "PROPOSALS")),
            charter_space=str(flat.get("atlassian_charter_space", "SD")),
            jira_project=str(flat.get("atlassian_jira_project", "SD")),
        )

    github: GitHubSettings | None = None
    if flat.get("github_token"):
        github = GitHubSettings(
            token=str(flat["github_token"]),
            owner=str(flat.get("github_owner", "")),
            repo=str(flat.get("github_repo", "")),
            default_base=str(flat.get("github_default_base", "main")),
        )

    aws = AwsSettings(
        region=str(flat.get("aws_region", os.getenv("SIS_AWS_REGION", "us-east-1"))),
        secret_id=_opt_str(flat.get("aws_secret_id")),
    )

    return Settings(env=env, adapters=adapters, atlassian=atlassian, github=github, aws=aws)


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _select_source() -> SecretSource:
    env = os.getenv("SIS_ENV", "local")
    if env == "aws":
        secret_id = os.environ.get("SIS_AWS_SECRET_ID")
        if not secret_id:
            raise RuntimeError("SIS_ENV=aws requires SIS_AWS_SECRET_ID")
        return AwsSecretsManagerSource(secret_id, os.getenv("SIS_AWS_REGION"))
    secrets_file = Path(os.getenv("SIS_SECRETS_FILE", DEFAULT_SECRETS_FILE))
    if secrets_file.exists():
        return FileSecretSource(secrets_file)
    return EnvSecretSource()


def load_settings(source: SecretSource | None = None) -> Settings:
    """Load typed settings from the chosen secret source (auto-selected if None)."""
    env = os.getenv("SIS_ENV", "local")
    adapters = os.getenv("SIS_ADAPTERS", "memory")
    src = source if source is not None else _select_source()
    try:
        raw = src.load()
    except FileNotFoundError:
        raw = {}
    return _build_settings(env, adapters, raw)


# Confluence space keys the org writes to when no Atlassian integration is
# configured (the in-memory path). Real runs use the AtlassianSettings values,
# so the space keys are configuration — not constants baked into the roles.
DEFAULT_SPACES: dict[str, str] = {
    "proposal": "PROPOSALS",
    "spec": "SPECS",
    "charter": "CHARTER",
}


def space_keys(settings: Settings | None = None) -> dict[str, str]:
    """Confluence space keys the org writes to: proposal / spec / charter.

    Sourced from :class:`AtlassianSettings` so a real tenant's spaces are
    configurable, not hardcoded. Falls back to :data:`DEFAULT_SPACES` when no
    Atlassian integration is configured (e.g. the default in-memory run), which
    keeps the local path credential-free and unchanged.
    """
    resolved = settings if settings is not None else load_settings()
    atl = resolved.atlassian
    if atl is None:
        return dict(DEFAULT_SPACES)
    return {
        "proposal": atl.proposal_space,
        "spec": atl.spec_space,
        "charter": atl.charter_space,
    }


def settings_summary(settings: Settings) -> dict[str, Any]:
    """A safe, log-friendly view of which integrations are configured (no secrets)."""
    return {
        "env": settings.env,
        "adapters": settings.adapters,
        "atlassian_configured": settings.atlassian is not None,
        "github_configured": settings.github is not None,
        "aws_region": settings.aws.region if settings.aws else None,
        # Touch dataclass fields so a refactor that drops repr-masking is caught.
        "credential_fields_masked": [f.name for f in fields(AtlassianSettings) if not f.repr],
    }
