# Self-Improving Server (Python + Ray) — "omnibase"

Detailed brief: @DESIGN.md — read it before starting.
Actor roles, external subsystems & the self-model: @ACTORS.md
New to the code? `docs/CODE_TOUR.md` (Python + Ray walkthrough). Contributor flow: `CONTRIBUTING.md`.
Architecture diagram: `ray_self_improving_control_loop.svg` (this folder).

**Naming:** *omnibase* = this engine (the `sis/` package; `sis` is just the import handle).
*omnitrack* = the future end product (modeling an external slice of the world).

## What this is
A server that models a slice of the real world via an actor hierarchy and can extend
itself by generating, validating, and safely deploying its own code from high-level
specs. Bootstrap goal first: make the server itself self-improving on a trivial
internal target before it models anything external.

## Stack (decided — don't relitigate without reason)
- **Python 3.14, standard CPython — NOT free-threaded.** Ray ships `cp314` wheels but
  not `cp314t`, so a `python3.14t` env can't install Ray. Managed with **Poetry** (`uv`
  works too). Keep this env separate from the unrelated Spark/py4j work project on 3.11.
- Ray for actors, Ray Serve for serving/canary rollouts.
- License: Apache 2.0 (`LICENSE` committed). Never copy Akka source code.

## Architecture (detail in @DESIGN.md; full role spec in @ACTORS.md)
- CEO/CTO/PM + SWE/QA/DevOps/Designer = Ray actors (leadership are named & detached);
  `Workspace` + `SelfModel` are named detached actors holding shared state.
- Actors coordinate through durable artifacts (Confluence/Jira/Git), not just in-memory
  messages — the work queue, message bus, audit trail, and long-term memory. Handoffs
  are artifact state changes.
- External subsystems behind ports/adapters (`sis/ports.py`): Document Store=Confluence,
  Work Tracker=Jira, Version Control=GitHub, Cloud=AWS, Telemetry. Default = in-memory
  adapters; real adapters in `sis/adapters_real.py` (`SIS_ADAPTERS=real`).
- SelfModel (digital twin): live actor registry, deploy slots, provenance graph,
  substrate — the first "piece of the world" the system models.
- Episodic store (`sis/episodic.py`): every cycle's outcome behind a port —
  spec → diff → gauntlet verdict → outcome, incl. every rejected diff and the
  gate that caught it. Backends via `SIS_EPISODIC_STORE`: `jsonl` (default),
  `duckdb` (SQL analytics), `none`. This is the dataset the system learns from;
  Postgres+pgvector can drop in later as another backend.
- Control loop: monitor → budget/goal gate → propose Python on a branch → validation
  gauntlet → canary → promote/rollback → log → circuit breaker.

## Hard rules
- Generated/untrusted code NEVER runs in the main process. The gauntlet sandboxes every
  gate: `SIS_SANDBOX=subprocess` (scrubbed env + in-process egress block, default) or
  `docker` (kernel-enforced `--network none`, no creds, only the temp dir mounted). A
  per-gate timeout kills infinite loops.
- Gauntlet gates hard (`sis/gauntlet.py`): `ast.parse` → `mypy --strict` → `pytest` →
  differential correctness on random inputs (anti-gaming) + benchmark vs a freshly
  measured baseline (must be ≥10% faster) → human PR. Generated code MUST be fully typed.
- **Change-authorization policy (`sis/policy.py`) — what the loop may rewrite:**
  - FORBIDDEN (never, no override): guardrail/safety code — the gauntlet, cost/brakes,
    settings/secrets, the adapters, `Dockerfile.gauntlet`, and the policy itself.
  - STRICT (off-limits unless `SIS_ALLOW_STRICT_CHANGES=1`, then needs human approval +
    justification + passing checks): all other engine code.
  - SOFT (optimisable; checks + review): the designated target(s) — `runtime/target.py`,
    extendable via `SIS_TARGET_PATHS`. Guardrail classification always wins.
- Branches: `feature/*` → `develop` → `main`. Never push to `develop`/`main` directly
  (see workflow below).
- Secrets: `secrets.local.yml` (gitignored) locally; AWS Secrets Manager in cloud
  (`SIS_ENV=aws`). Never commit tokens.
- Destructive Jira/Confluence/GitHub actions, PR merges, and canary→live promotion
  require human approval (`RequiresHumanApproval` in the adapters).
- Hard LLM spend cap + cost-per-accepted-improvement SLO + circuit breaker (CEO brakes).

## Repo & GitHub workflow
- Repo: **github.com/ozumpe/omnibase** (private). `gh` CLI is authenticated (account
  `ozumpe`, SSH). **Default branch: `develop`.**
- Flow: **`feature/<name>` → `develop` (integration sandbox) → `main` (tested releases).**
  Open PRs with `gh pr create --base develop`. CI must pass before merge; self-merge is
  fine (0 required approvals).
- **Never push to `develop`/`main` directly.** Client-side guard: `git config
  core.hooksPath hooks` enables `hooks/pre-push`, which blocks it (emergency override:
  `git push --no-verify`). Server-side rulesets are also active on both branches
  (GitHub Pro; ruleset ids `17153780` main, `17153781` develop) — the client hook
  is belt-and-suspenders, not the sole enforcement.
- CI (`.github/workflows/ci.yml`): `ruff` + `mypy --strict` + `pytest` on push/PR to
  `main` and `develop`. The required status-check context is `test`.
- After ANY `pyproject.toml` change, run `poetry lock` — CI fails on a stale lock file.

## Conventions
- `ruff`, `mypy --strict`, `pytest` — all green before a PR (also CI + gauntlet gates).
- Keep decision logic in pure functions (e.g. `evaluate_brakes`, `policy.classify`) and
  I/O in the Ray actors, so logic is unit-testable without standing up Ray.
- Every bug found becomes a permanent regression test (see `tests/test_adversarial.py`).
- Small reviewable PRs. Provenance: prompt → commit → PR → ticket → outcome.

## Operational quick reference
- Run a cycle: `poetry run python main.py` (in-memory, no creds).
- Gates: `poetry run pytest` · `poetry run mypy --strict sis/ main.py scripts/` · `poetry run ruff check .`
- Optional deps: `poetry install --with llm` (anthropic) · `--with real`
  (requests/boto3/pyyaml) · `--with analytics` (duckdb).
- Env flags (full table in `README.md`): `SIS_PROPOSER` (stub|claude), `SIS_SANDBOX`
  (subprocess|docker), `SIS_ADAPTERS` (memory|real), `SIS_ENV` (local|aws),
  `SIS_EPISODIC_STORE` (jsonl|duckdb|none), `SIS_TARGET_PATHS`,
  `SIS_ALLOW_STRICT_CHANGES`, `SIS_GAUNTLET_TIMEOUT`, `SIS_SANDBOX_IMAGE`,
  `ANTHROPIC_API_KEY`.
- Real adapters: `cp secrets.example.yml secrets.local.yml`; then
  `poetry run python scripts/check_connections.py --deep` before a real cycle.
- Docker sandbox image: `docker build -t sis-gauntlet:latest -f Dockerfile.gauntlet .`
- Confluence specs/docs live in the **"Software Development" (SD)** space (Atlassian MCP,
  cloudId `760ca470-0091-4601-9704-a56633b5e9b6`): Architecture, Validation Gauntlet,
  Codebase Guide, Guardrails & Operations, Milestone Roadmap, Risks & Next Steps.

## Current status — where to pick up
Released through **v0.1.4**. The bootstrap skeleton (original "first task") is
**done**, plus much more:
- Actor org + SelfModel + Workspace; one intake→deploy cycle runs locally and stops at
  the human PR merge.
- Gauntlet hardened: differential-correctness anti-gaming, per-gate timeout, subprocess
  sandbox (default) and kernel-enforced docker sandbox (built + verified on a live daemon).
- Real Claude proposer behind `SIS_PROPOSER=claude`; default stub is offline.
- Ports/adapters (in-memory + real Confluence/Jira/GitHub/AWS), secrets layer, CEO spend
  brakes, change-authorization policy.
- Episodic/provenance store (`sis/episodic.py`) behind a port: `jsonl` default +
  optional `duckdb` SQL analytics + `none`; every cycle outcome recorded.
- Failures become artifacts: every rolled-back/rejected cycle, and every circuit-breaker
  trip, files a bug via `DevOps.file_bug` — outcomes aren't log-only.
- The CEO writes the top-level charter once at bootstrap (`CEO.set_charter`);
  provenance roots at it (`charter → spec → epic → story → outcome`).
- **Level 2 validated (2026-07-25): real cycles ran end-to-end against a live tenant**
  (scratch Jira project `TES` + throwaway repo `ozumpe/testrun`), including re-runs.
  Live-tenant fixes, each with a regression test: Confluence duplicate-title 400 →
  `create_page` updates the existing page in place; cross-space parent 404 → parent
  dropped (provenance lives in the SelfModel); cycles start from the target as merged
  on the base branch (`VersionControl.live_target_source`) instead of the stale local
  file, so a merged optimisation is built upon, not re-proposed.
- 86 tests; `ruff`/`mypy --strict`/`pytest` clean; CI green; `feature → develop → main`
  enforced by both the client-side pre-push hook and active server-side rulesets.

**Known issues:** `docs/KNOWN_ISSUES.md` is the canonical, ID'd list (H/M/L
severity) from the 2026-07-25 full review — reference the IDs in commits/PRs.

**Next (sequencing detail in `docs/KNOWN_ISSUES.md`):**
1. Fix **H1** — `gauntlet.validate()` baselines against the stale local
   `runtime/target.py` instead of the cycle's merged source, so post-merge cycles
   ship no-op PRs (this, not timing jitter, is why PR #3 passed). Plus **M3**
   (identical-source short-circuit), **M4** (hardcoded branch base), and the
   **M1** guard (require the docker sandbox with a real proposer).
2. First real-life test: `SIS_ADAPTERS=real SIS_PROPOSER=claude SIS_SANDBOX=docker`
   on the scratch tenant, tiny CEO budget, verify `cost.py` pricing (L3) first.
3. A small AWS run (one node, a few cycles); fix **M2** (anonymous Ray namespace)
   first; watch the provenance graph and the bill.
4. Ray Serve weighted canary + atomic actor swap (currently a placeholder).
