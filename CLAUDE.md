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
  per-gate timeout kills infinite loops. A real (non-stub) proposer writes untrusted code
  and REQUIRES `SIS_SANDBOX=docker` — the loop refuses otherwise (override:
  `SIS_ALLOW_UNSANDBOXED_LLM=1`); the subprocess sandbox leaves host files readable (M1).
- Gauntlet gates hard (`sis/gauntlet.py`): `ast.parse` → no-op check →
  `mypy --strict` → interface (exports the contract's entry point) → the
  contract's acceptance tests → differential correctness on random inputs
  (anti-gaming) + benchmark vs a freshly measured baseline (must clear the
  contract's margin, ≥10% faster by default) → human PR. Generated code MUST be
  fully typed. What counts as correct/better is per-target — see `sis/contract.py`.
- **Change-authorization policy (`sis/policy.py`) — what the loop may rewrite:**
  - FORBIDDEN (never, no override): guardrail/safety code — the gauntlet, cost/brakes,
    settings/secrets, the adapters, `Dockerfile.gauntlet`, and the policy itself.
  - STRICT (off-limits unless `SIS_ALLOW_STRICT_CHANGES=1`, then needs human approval +
    justification + passing checks): all other engine code.
  - SOFT (optimisable; checks + review): the designated target(s) —
    `runtime/target.py` and `runtime/sort_target.py`, extendable via
    `SIS_TARGET_PATHS`. Guardrail classification always wins.
- Branches: `feature/*` → `develop` → `main`. Never push to `develop`/`main` directly
  (see workflow below).
- Secrets: `secrets.local.yml` (gitignored) locally; AWS Secrets Manager in cloud
  (`SIS_ENV=aws`). Never commit tokens.
- Destructive Jira/Confluence/GitHub actions and PR merges require human approval
  (`RequiresHumanApproval` in the adapters). **Canary→live promotion follows an
  *observed* merge** (OMNI-15): `Cloud.promote()` no longer raises, because doing
  so unconditionally made promotion unreachable rather than gated. Its only
  caller is `DevOps.observe_merge()`, which promotes solely when the PR reads
  back as `merged`. The agent cannot manufacture that — `merge_pr()` still raises
  in every adapter and `Workspace` exposes no merge-shaped method at all (both
  asserted by tests). It applies a human's decision; it never makes one.
- Hard LLM spend cap + cost-per-accepted-improvement SLO + circuit breaker (CEO brakes).

## Repo & GitHub workflow
- Repo: **github.com/ozumpe/omnibase** (public since 2026-08-06). `gh` CLI is
  authenticated (account `ozumpe`, SSH). **Default branch: `develop`.**
- Flow: **`feature/<name>` → `develop` (integration sandbox) → `main` (tested releases).**
  Open PRs with `gh pr create --base develop`. CI must pass before merge; self-merge is
  fine (0 required approvals).
- **Never push to `develop`/`main` directly.** Client-side guard: `git config
  core.hooksPath hooks` enables `hooks/pre-push`, which blocks it (emergency override:
  `git push --no-verify`). Server-side rulesets are also active on both branches
  (GitHub Pro; ruleset ids `17153780` main, `17153781` develop) — the client hook
  is belt-and-suspenders, not the sole enforcement.
- **Every commit needs a Jira key or an explicit opt-out.** Same `core.hooksPath
  hooks` setting enables `hooks/commit-msg`, which rejects a commit message with
  neither an `OMNI-N` key nor a `No-Ticket: <reason>` trailer line (same style as
  `Co-Authored-By:`; emergency override: `git commit --no-verify`). A trailer, not
  free text like `[no-ticket]` — free text collides with prose *about* the marker
  (this hook's own commit needed to document it, which would otherwise trip the
  bypass). Merge commits and `fixup!`/`squash!` commits are exempt. The hook is
  client-side, so the `commit-lint` CI job below is its server-side backstop for
  when it is bypassed or not enabled in a clone.
- CI (`.github/workflows/ci.yml`): `ruff` + `mypy --strict` + `pytest` on push/PR to
  `main` and `develop`. The required status-check context is `test`.
- Commit-lint (`.github/workflows/commit-lint.yml`): every non-merge commit newly
  introduced by a PR needs an `OMNI-N` key or a `No-Ticket:` trailer — the
  server-side backstop for the Jira-key convention above, for when
  `hooks/commit-msg` is bypassed or not enabled in a given clone. Runs on a PR
  against any base (no branch filter, unlike `ci.yml`'s `test` job). Required
  status-check context: `commit-lint`.
- After ANY `pyproject.toml` change, run `poetry lock` — CI fails on a stale lock file.

## Conventions
- `ruff`, `mypy --strict`, `pytest` — all green before a PR (also CI + gauntlet gates).
- Keep decision logic in pure functions (e.g. `evaluate_brakes`, `policy.classify`) and
  I/O in the Ray actors, so logic is unit-testable without standing up Ray.
- Every bug found becomes a permanent regression test (see `tests/test_adversarial.py`).
- Small reviewable PRs. Provenance: prompt → commit → PR → ticket → outcome.
- **Every commit and PR title touching planned work references its Jira key**
  (e.g. `OMNI-6: contract-derived proposer prompt`) — anywhere in the message,
  first line preferred. This is what lets the GitHub-for-Jira app populate the
  issue's Development panel automatically; without the key present, nothing
  links. Ad-hoc fixes with no OMNI story don't need one.

## Operational quick reference
- Run a cycle: `poetry run python main.py` (in-memory, no creds).
- Gates: `poetry run pytest` · `poetry run mypy --strict sis/ main.py scripts/` · `poetry run ruff check .`
- Optional deps: `poetry install --with llm` (anthropic) · `--with real`
  (requests/boto3/pyyaml) · `--with analytics` (duckdb).
- Env flags (full table in `README.md`): `SIS_PROPOSER` (stub|claude), `SIS_SANDBOX`
  (subprocess|docker), `SIS_ADAPTERS` (memory|real), `SIS_ENV` (local|aws),
  `SIS_EPISODIC_STORE` (jsonl|duckdb|none), `SIS_TARGET_PATHS`,
  `SIS_CONTRACT` (which target a cycle optimises: sum_of_divisors|sort —
  must be set BEFORE launch; prefer `--contract`/`run_cycle(contract_name=)`),
  `SIS_ALLOW_STRICT_CHANGES`, `SIS_GAUNTLET_TIMEOUT`, `SIS_SANDBOX_IMAGE`,
  `SIS_ALLOW_UNSANDBOXED_LLM`, `SIS_BUDGET_USD` (CEO hard cap; + the other
  brakes), `SIS_HTTP_TIMEOUT` (real-adapter request timeout), `SIS_LLM_PROVIDER`
  / `SIS_LLM_MODEL` (which LLM backs the proposer; adapters in `sis/llm.py`),
  `ANTHROPIC_API_KEY`.
- Real adapters: `cp secrets.example.yml secrets.local.yml`; then
  `poetry run python scripts/check_connections.py --deep` before a real cycle.
- Docker sandbox image: `docker build -t sis-gauntlet:latest -f Dockerfile.gauntlet .`
- Confluence specs/docs live in the **"Software Development" (SD)** space (Atlassian MCP,
  cloudId `760ca470-0091-4601-9704-a56633b5e9b6`): Architecture, Validation Gauntlet,
  Codebase Guide, Guardrails & Operations, Milestone Roadmap, Risks & Next Steps.
- **Milestone plan lives in Jira: project `OMNI` ("OmniBase"), same cloudId**
  (<https://olafzumpe.atlassian.net/browse/OMNI>). Epics + stories for the next
  milestone; **this is the source of truth for what to work on next.** Move a story
  to Done as its PR merges, and reference the key in the commit/PR
  (e.g. "OMNI-6: contract-derived proposer prompt"). `docs/KNOWN_ISSUES.md` stays
  the canonical record of *defects* (H/M/L IDs); Jira holds *planned work*.
  Note the scratch project `TES` is where the **loop itself** files artifacts during
  live runs — don't put planning there.

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
- **Post-Level-2 hardening (on `develop`): the full-review High/Medium list is closed.**
  H1 — `gauntlet.validate()` now benchmarks against the cycle's baseline source, not
  the stale local file, so a post-merge no-op can't pass every gate. A candidate
  identical to the baseline is rejected as a benign `no_change` outcome — no bug filed,
  no breaker increment (the CEO's `record_neutral` still records spend). A real
  (non-stub) proposer now REQUIRES `SIS_SANDBOX=docker` (M1), the SWE forks from the
  configured base branch (M4), and the `cost.py` pricing table is verified against
  published rates (L3).
- The CEO spend brakes are env-configurable (`SIS_BUDGET_USD` + the other
  thresholds), so a first real run can set a deliberately tiny cap without
  editing source (M5).
- **The engine is target-agnostic (L5 closed, 2026-08-06).** Nothing in `sis/`
  knows a target by name. `sis/contract.py` holds each target's entry point,
  margin and trial count; `specs/<name>/` holds its reference oracle, benchmark
  inputs and acceptance tests as a module the gauntlet copies into the sandbox.
  `specs/` is POLICY-FORBIDDEN — the implementer cannot edit its own exam. The
  proposer prompt is contract-derived too, so the LLM is told to write the right
  function for whichever target is active. **Two targets ship** and both run the
  full loop on the same engine: `runtime/target.py` (`sum_of_divisors`) and
  `runtime/sort_target.py` (`sort_numbers`). Select with
  `--contract <name>` / `run_cycle(contract_name=...)`. A third target is a new
  `specs/` directory plus a registry entry, not an engine change.
- **The sort is served over HTTP behind Ray Serve** (`sis/serving.py`, OMNI-11):
  blue and green run simultaneously with *different source* (`/sort`,
  `/sort-green`), each response carrying its own `version`/`slot`. Stateless by
  construction. `python -m sis.serving`; see RUNBOOK Level 0b. **A replica is
  not the gauntlet sandbox** — OMNI-13 closed the credential half (green's env
  is scrubbed) but egress stays open by construction, since a replica must
  answer HTTP. Treat it as a credential boundary, never as a sandbox.
- **Synthetic load** (`sis/loadgen.py`, OMNI-12): concurrent, varied, valid
  traffic against the served target, inputs from the contract's oracle so the
  online and offline gates share one definition of a valid input. Observations
  feed `InMemoryCloud.observe()`. `python -m sis.loadgen`; RUNBOOK Level 0c.
  Measured: the candidate is ~5x faster in isolation but only ~30% faster at
  p95 under 8-way load — the GIL makes both queue-bound. That gap is why the
  canary exists.
<
- **`ServeCloud`** (`sis/serve_cloud.py`, OMNI-13): the third `Cloud` adapter —
  real weighted split, real shadow dispatch, real per-version windows.
  `python -m sis.serve_cloud` runs serve → load → verdict in one command
  (RUNBOOK Level 0d). Three things worth knowing:
  - **Ray Serve has no weighted split**, so it is a component: `CanaryRouter`
    in `sis/serving.py`, also the only place that sees both versions answer and
    therefore the only place a *paired* sample can be recorded.
  - **Green is a separate Serve application** the router attaches to. The
    one-app topology was built first and measured to **restart the blue
    replica** on every canary deploy — cycling the stable version at the moment
    it becomes the baseline. Regression-tested.
  - **Green's `runtime_env` is scrubbed** (every non-allowlisted env var
    blanked, since Ray *merges* `env_vars` rather than replacing them), so
    candidate code cannot read a credential. **Egress stays open by
    construction** — a replica must answer HTTP. A credential boundary, not the
    gauntlet's sandbox. Not yet wired into `Workspace`; that is OMNI-14.
- 282 tests; `ruff`/`mypy --strict`/`pytest` clean; CI green; `feature → develop → main`
  enforced by both the client-side pre-push hook and active server-side rulesets.

**Known issues:** `docs/KNOWN_ISSUES.md` is the canonical, ID'd list (H/M/L
severity) from the 2026-07-25 full review + a 2026-07-28 second pass — reference
the IDs in commits/PRs. **High, Medium and Low are all clear**; L5 (the target
contract / benchmark oracle) resolved 2026-08-06. Planned work lives in Jira
([`OMNI`](https://olafzumpe.atlassian.net/browse/OMNI)), defects here.

Two traps L5 surfaced, both worth knowing before writing similar code:
- **Anti-gaming is only as strong as the input distribution.** A candidate that
  was silently wrong above a size threshold passed every gate until the oracle's
  random inputs spanned that range.
- **Env vars don't reach the role actors.** They are detached Ray actors in
  their own processes, inheriting the driver's environment when *created* — so
  anything exported after `bootstrap()` (including `monkeypatch.setenv`) is
  invisible. Per-cycle configuration must be passed as an argument.

- **First real-life test PASSED (2026-07-28): the full loop ran end-to-end with a
  real Claude proposer + real adapters + the kernel-enforced docker sandbox.** Cycle
  `cb4f6fe13ed7`: `claude-opus-4-8` proposed the O(√n) `isqrt` form against the naive
  baseline (re-seeded on `testrun/main`), the gauntlet passed **inside docker**
  (192.4µs → 1.6µs, 99.2% faster), and it filed Confluence spec `6356994`, Jira
  `TES-20`, GitHub `testrun` PR #4, stopping at `verified_awaiting_human_merge`. Cost
  $0.014375, reconciled against the Anthropic console. Details in
  `docs/KNOWN_ISSUES.md` (Resolved).

**Next — the milestone plan is in Jira ([`OMNI`](https://olafzumpe.atlassian.net/browse/OMNI)),
not here.** Three epics; design detail in the docs each links to. Check the board
for current status rather than trusting this list:

1. ~~**[OMNI-1](https://olafzumpe.atlassian.net/browse/OMNI-1) — L5 target
   contract** (Class 1)~~ — **done 2026-08-06** (OMNI-4/5/6/7). Two targets ship
   and the engine names neither. Design: `docs/CLASS2_CONTRACT.md`.
2. **[OMNI-2](https://olafzumpe.atlassian.net/browse/OMNI-2) — Ray Serve canary.**

   Steps 5/6/7/8/9/11 done (`evaluate_canary`, the served sort, the load
   generator, the `Cloud` traffic+metrics port, `ServeCloud`, the live breach
   trigger); open: rework `DevOps.canary()` for the real flow (**OMNI-14**, the
   step that wires `ServeCloud` into `Workspace`), and **OMNI-15** — observe the
   human merge so `promote()` has a caller at all. Design: `docs/SERVE_CANARY.md`.
3. **[OMNI-3](https://olafzumpe.atlassian.net/browse/OMNI-3) — Class 2**, feature
   construction: `FeatureContract` + acceptance gates, `InvariantGate`, backtests,
   `ToolchainAdapter`, the contract-author actor. Design: `docs/CLASS2_CONTRACT.md`.

Not yet scheduled: a small AWS run (one node, a few cycles) — watch the provenance
graph and the bill; and the **atomic actor swap** for internal, never-served actors,
which `docs/SERVE_CANARY.md` scopes out and which has no design doc yet.
