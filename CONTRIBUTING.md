# Contributing to omnibase

Thanks for your interest! omnibase is a self-improving server built on Python +
Ray. This guide covers how to get set up, the bar for changes, and the project's
one non-negotiable design principle.

New to the codebase? Start with [`docs/CODE_TOUR.md`](docs/CODE_TOUR.md) — a
reading guide written for people learning Python and Ray.

## The one principle

**The deterministic gauntlet decides what ships — never a human's good
intentions and never the LLM.** Any change that weakens a gate
([`sis/gauntlet.py`](sis/gauntlet.py)) needs a very good reason and a test. Any
bug found in generated code becomes a permanent case in
[`tests/test_adversarial.py`](tests/test_adversarial.py). The test suite is the
moat; grow it.

## Setup

Requirements: **Python 3.14** (standard CPython — not free-threaded; Ray ships
`cp314` wheels but not `cp314t` yet) and [Poetry](https://python-poetry.org/).

```bash
poetry install                 # core deps (ray, etc.)
poetry install --with llm      # + anthropic, for the real proposer
poetry install --with real     # + requests/boto3/pyyaml, for real adapters
```

## Branch workflow

Work flows through three tiers — never push to `develop` or `main` directly:

```
feature/<name>  →  develop (integration sandbox)  →  main (tested releases)
```

- **`feature/<name>`** — all work happens here. Push freely.
- **`develop`** — the default branch and integration sandbox. Feature branches
  merge in via PR once they work; CI must pass.
- **`main`** — tested releases only. `develop` merges in via PR when it's been
  validated.

Enable the client-side guard once per clone (blocks accidental direct pushes to
`develop`/`main`):

```bash
git config core.hooksPath hooks
```

(Server-side branch protection on `develop`/`main` is the authoritative gate —
enabled when the repo is public or on GitHub Pro.)

## The checks (all must pass before a PR)

```bash
poetry run pytest                       # the full suite
poetry run mypy --strict sis/ main.py scripts/
poetry run ruff check .
```

`mypy --strict` and `pytest` are both CI gates *and* gauntlet gates — they hold
generated code to the same bar as hand-written code, so keep everything fully
type-annotated.

## Conventions

- **Small, reviewable PRs.** One concern per PR.
- **Fully type-annotated.** No untyped defs; `mypy --strict` must be clean.
- **Tests with behavior changes.** New feature → new test. Bug fix → regression
  test that fails before and passes after.
- **Keep decision logic pure where you can.** I/O lives in the Ray actors;
  the logic they call (e.g. `evaluate_brakes` in `sis/roles.py`) is a plain
  function so it's testable without standing up Ray. Copy this pattern.
- **Never weaken a guardrail silently.** Destructive/irreversible actions raise
  `RequiresHumanApproval`; the agent works on feature branches and never merges
  to `main` or promotes a canary to live.
- **Docstrings explain *why*.** The modules are documentation; match that tone.

## Where things live

| Area | Files | Notes |
|---|---|---|
| Validation gauntlet | `sis/gauntlet.py` | The moat. Sandbox modes + timeout live here. |
| Proposer | `sis/proposer.py` | Stub (default) or Claude behind `SIS_PROPOSER=claude`. |
| Ports & adapters | `sis/ports.py`, `adapters.py`, `adapters_real.py` | Add a real backend by implementing the Protocols. |
| Actor org | `sis/roles.py`, `org.py` | The seven roles + the cycle. |
| Shared state | `sis/workspace.py`, `self_model.py` | Named, detached Ray actors. |
| Config & secrets | `sis/settings.py` | Local YAML ↔ AWS Secrets Manager. |
| Cost accounting | `sis/cost.py` | Feeds the CEO's spend brakes. |

## Common extension points

- **A new real integration** (e.g. GitLab instead of GitHub): implement the
  relevant `Protocol` from `sis/ports.py` in a new adapter; wire it in
  `sis/workspace.py`. Roles don't change.
- **A new role**: subclass `Role` in `sis/roles.py`, register it in
  `org.bootstrap()`. It coordinates through `Workspace` + `SelfModel`.
- **A new gauntlet gate**: add it to `validate()` in cheapest-first order, run it
  through the sandbox helper `_run()`, and add both a pass and a fail test.

## Secrets & safety

- Never commit credentials. `secrets.local.yml` is gitignored; copy from
  `secrets.example.yml`. In the cloud, use AWS Secrets Manager (`SIS_ENV=aws`).
- When working on the real adapters, validate against a **scratch** Jira
  project / throwaway repo first — run `python scripts/check_connections.py
  --deep` to confirm connectivity and the Jira workflow before a real cycle.
- Run the loop with `SIS_SANDBOX=docker` once a real LLM is writing code, so
  candidate execution is kernel-isolated (build the image from
  `Dockerfile.gauntlet`).

## Reporting issues

Include: what you ran (command + relevant `SIS_*` env vars), what you expected,
what happened, and the `Result.reason` / traceback. For adapter issues against a
real tenant, the exact API error (with credentials redacted) is gold.

## License

By contributing, you agree your contributions are licensed under the
[Apache License 2.0](LICENSE), the project's license.
