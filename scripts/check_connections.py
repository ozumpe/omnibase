"""scripts/check_connections.py — read-only credential & connectivity check.

Loads settings from the configured secret source (local YAML or AWS Secrets
Manager) and does a single READ-ONLY call against each configured service so
you can verify credentials before running a full cycle. It never writes,
transitions, commits, or deploys anything.

Usage:
    poetry run python scripts/check_connections.py
    poetry run python scripts/check_connections.py --deep   # also verify Jira workflow

Exit code is 0 only if every *configured* integration responds OK; missing
integrations are reported as SKIP (not a failure). Tokens are never printed.

``--deep`` additionally lists the Jira project's workflow statuses and checks
that the ones the org transitions to (``In Progress``, ``Ready for Review``,
``TBD``, ``Done``, ``To Do``) actually exist — so the first real cycle won't
fail on a ``JiraWorkTracker.transition`` name mismatch. Still read-only.
"""

from __future__ import annotations

import pathlib
import sys

# Allow running as a plain script: put the repo root (parent of scripts/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sis.settings import Settings, load_settings, settings_summary, space_keys  # noqa: E402

OK = "✓"      # ✓
FAIL = "✗"    # ✗
SKIP = "–"    # –


def _line(mark: str, name: str, detail: str) -> None:
    print(f"  {mark} {name:<12} {detail}")


def check_confluence(settings: Settings) -> bool | None:
    """Verify every space the org writes to exists (GET /wiki/api/v2/spaces). Read-only.

    The roles create pages in the proposal, spec, and charter spaces; a missing
    one crashes the first real cycle (ConfluenceDocumentStore raises on an
    unknown space), so all of them are checked here — not just the spec space.
    """
    if settings.atlassian is None:
        _line(SKIP, "Confluence", "not configured")
        return None
    s = settings.atlassian
    keys = sorted(set(space_keys(settings).values()))
    try:
        from sis.adapters_real import _session

        http = _session(s.email, s.api_token)
        missing: list[str] = []
        for key in keys:
            resp = http.get(f"{s.base_url}/wiki/api/v2/spaces", params={"keys": key})
            resp.raise_for_status()
            if not resp.json().get("results", []):
                missing.append(key)
        if missing:
            _line(FAIL, "Confluence",
                  f"spaces not found: {', '.join(missing)} "
                  "(create them, or set atlassian_{proposal,spec,charter}_space)")
            return False
        _line(OK, "Confluence", f"all org spaces present: {', '.join(keys)}")
        return True
    except Exception as exc:  # noqa: BLE001 - report any failure, don't crash the check
        _line(FAIL, "Confluence", _summarise(exc))
        return False


def check_jira(settings: Settings) -> bool | None:
    """Fetch the project (GET /rest/api/3/project/KEY). Read-only."""
    if settings.atlassian is None:
        _line(SKIP, "Jira", "not configured")
        return None
    s = settings.atlassian
    try:
        from sis.adapters_real import _session

        http = _session(s.email, s.api_token)
        resp = http.get(f"{s.base_url}/rest/api/3/project/{s.jira_project}")
        resp.raise_for_status()
        data = resp.json()
        _line(OK, "Jira", f"project {s.jira_project!r} → {data.get('name', '?')}")
        return True
    except Exception as exc:  # noqa: BLE001
        _line(FAIL, "Jira", _summarise(exc))
        return False


def check_github(settings: Settings) -> bool | None:
    """Fetch the repo (GET /repos/{owner}/{repo}). Read-only."""
    if settings.github is None:
        _line(SKIP, "GitHub", "not configured")
        return None
    s = settings.github
    try:
        from sis.adapters_real import _session

        http = _session(None, s.token)
        resp = http.get(f"https://api.github.com/repos/{s.owner}/{s.repo}")
        resp.raise_for_status()
        data = resp.json()
        perms = data.get("permissions", {})
        # This confirms repo *access* only, not that the PAT carries the write
        # scopes a real cycle needs (Contents + Pull requests). GitHub exposes no
        # reliable read-only way to check fine-grained-token scopes, and a
        # side-effect-free preflight must not attempt a write — so an under-scoped
        # token surfaces as a loud 403 at open_pr instead. See KNOWN_ISSUES.md L6
        # (won't fix); the runbook tells you which scopes to grant up front.
        _line(OK, "GitHub", f"{data.get('full_name')} (push={perms.get('push', '?')})")
        return True
    except Exception as exc:  # noqa: BLE001
        _line(FAIL, "GitHub", _summarise(exc))
        return False


# Statuses the org actually transitions issues *to* (see sis.roles). These must
# exist in the project's workflow or JiraWorkTracker.transition will raise.
_FLOW_STATUSES = ("To Do", "In Progress", "Ready for Review", "TBD", "Done")


def check_jira_workflow(settings: Settings) -> bool | None:
    """Deep check: list the Jira project's statuses and confirm the flow set exists."""
    if settings.atlassian is None:
        _line(SKIP, "Jira flow", "not configured")
        return None
    s = settings.atlassian
    try:
        from sis.adapters_real import _session

        http = _session(s.email, s.api_token)
        # Read-only: all statuses across the project's issue types.
        resp = http.get(f"{s.base_url}/rest/api/3/project/{s.jira_project}/statuses")
        resp.raise_for_status()
        names = {
            status["name"]
            for issue_type in resp.json()
            for status in issue_type.get("statuses", [])
        }
        missing = [name for name in _FLOW_STATUSES if name not in names]
        if missing:
            _line(FAIL, "Jira flow", f"missing workflow statuses: {', '.join(missing)}")
            return False
        _line(OK, "Jira flow", f"all flow statuses present ({len(names)} total)")
        return True
    except Exception as exc:  # noqa: BLE001
        _line(FAIL, "Jira flow", _summarise(exc))
        return False


def check_aws(settings: Settings) -> bool | None:
    """Confirm the caller identity (STS GetCallerIdentity). Read-only."""
    if settings.env != "aws" and (settings.aws is None or not settings.aws.secret_id):
        _line(SKIP, "AWS", "not configured (local secrets in use)")
        return None
    try:
        import boto3

        region = settings.aws.region if settings.aws else None
        identity = boto3.client("sts", region_name=region).get_caller_identity()
        _line(OK, "AWS", f"account {identity.get('Account')} ({region})")
        return True
    except Exception as exc:  # noqa: BLE001
        _line(FAIL, "AWS", _summarise(exc))
        return False


def _summarise(exc: Exception) -> str:
    """One-line error summary that never echoes credentials."""
    text = str(exc)
    return f"{type(exc).__name__}: {text[:120]}" if text else type(exc).__name__


def main() -> int:
    deep = "--deep" in sys.argv
    settings = load_settings()
    print("Configuration:")
    for key, value in settings_summary(settings).items():
        print(f"  {key}: {value}")

    print("\nConnectivity (read-only):")
    results = [
        check_confluence(settings),
        check_jira(settings),
        check_github(settings),
        check_aws(settings),
    ]
    if deep:
        results.append(check_jira_workflow(settings))

    checked = [r for r in results if r is not None]
    failed = [r for r in checked if r is False]
    if not checked:
        print("\nNothing configured to check. Fill in secrets.local.yml "
              "(see secrets.example.yml) and set SIS_ADAPTERS=real.")
        return 0
    if failed:
        print(f"\n{FAIL} {len(failed)} of {len(checked)} configured integrations failed.")
        return 1
    print(f"\n{OK} All {len(checked)} configured integrations responded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
