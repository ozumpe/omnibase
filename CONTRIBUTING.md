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

(Server-side rulesets on `develop`/`main` are the authoritative gate — active now
via GitHub Pro; the client-side hook above is belt-and-suspenders.)

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
| Configuration | `sis/config.py`, `config.yml` | Every knob, declared once. POLICY-FORBIDDEN. |
| Secrets | `sis/settings.py` | Local YAML ↔ AWS Secrets Manager. Never `config.yml`. |
| Cost accounting | `sis/cost.py` | Feeds the CEO's spend brakes. |

## Common extension points

- **A new real integration** (e.g. GitLab instead of GitHub): implement the
  relevant `Protocol` from `sis/ports.py` in a new adapter; wire it in
  `sis/workspace.py`. Roles don't change.
- **A new role**: subclass `Role` in `sis/roles.py`, register it in
  `org.bootstrap()`. It coordinates through `Workspace` + `SelfModel`.
- **A new gauntlet gate**: add it to `validate()` in cheapest-first order, run it
  through the sandbox helper `_run()`, and add both a pass and a fail test.
- **A new configuration knob**: add one `Key(...)` to `SCHEMA` in
  `sis/config.py`, then run `poetry run python -m sis.config --write` to
  regenerate `config.yml`. That gives you the typed accessor, the `SIS_*`
  environment variable, the `--section-name` CLI flag, and the documentation, all
  from the one declaration — so **don't restate the default anywhere else**,
  including in prose. A test regenerates the file and compares, and another
  asserts no module reads the environment directly, so both halves of that are
  enforced rather than remembered. Pick the tier (`forbidden_`/`strict_`/`soft_`)
  by what a *human operator* should be allowed to change in the UI; the loop
  cannot write the file at any tier. If a wrong value would fail silently and
  unsafely, give the key `choices`.

## Secrets & safety

- Never commit credentials. `secrets.local.yml` is gitignored; copy from
  `secrets.example.yml`. In the cloud, use AWS Secrets Manager (`SIS_ENV=aws`).
- When working on the real adapters, validate against a **scratch** Jira
  project / throwaway repo first — run `python scripts/check_connections.py
  --deep` to confirm connectivity and the Jira workflow before a real cycle.
- A real proposer (`SIS_PROPOSER=claude`) writes untrusted code and **requires**
  `SIS_SANDBOX=docker` — the loop refuses the soft subprocess sandbox otherwise
  (build the image from `Dockerfile.gauntlet`; explicit override
  `SIS_ALLOW_UNSANDBOXED_LLM=1`, unsafe).

## Reporting issues

Include: what you ran, what you expected, what happened, the `Result.reason` /
traceback, and the output of `poetry run python main.py --show-config` — which
reports every setting *and which layer supplied it*, so a stale `export` in your
shell that is silently overriding `config.yml` shows up instead of costing both
of us an afternoon. For adapter issues against a real tenant, the exact API
error (with credentials redacted) is gold; `--show-config` contains no secrets.

## License

By contributing, you agree your contributions are licensed under the
[Apache License 2.0](LICENSE), the project's license.
