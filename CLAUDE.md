# Self-Improving Server (Python + Ray) — "omnibase"

Detailed brief: @DESIGN.md — read it before starting.
Actor roles, external subsystems & the self-model: @ACTORS.md
New to the code? `docs/CODE_TOUR.md` (Python + Ray walkthrough). Contributor flow: `CONTRIBUTING.md`.
Architecture diagram: `ray_self_improving_control_loop.svg` (this folder).
Class 2 (feature construction) design: `docs/CLASS2_CONTRACT.md`. What comes after
Class 2, toward omnitrack: `docs/OMNITRACK_VISION.md`.

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
- **The contract selects which gates run** (`Contract.gate_profile()`,
  `sis/gauntlet.py`); both task classes flow through one `validate()`:
  - **Class 1** (`OptimizationContract` — make a working function faster):
    `ast.parse` → no-op check → `mypy --strict` → interface → the contract's
    acceptance tests → invariant gate → backtest gate → differential
    correctness on random inputs (anti-gaming) + benchmark vs a freshly
    measured baseline (must clear the contract's margin, ≥10% faster by
    default).
  - **Class 2** (`FeatureContract` — build what a spec describes, no
    pre-existing version to diff against): `ast.parse` → `mypy --strict` →
    interface → acceptance → invariant gate → backtest gate. No no-op (nothing
    to be identical to) and no differential/benchmark (no reference exists,
    and "faster" isn't what makes a feature correct).
  - **Invariant gate** (`sis/invariant.py`, Hypothesis-generated inputs) and
    **backtest gate** (`sis/backtest.py`, recorded-episode fixtures under
    `specs/<name>/`, held-out split) are the anti-gaming layer for targets with
    no reference oracle to differ against — domain laws over generated inputs,
    and "does it reproduce recorded reality", respectively.
  - Every gate ends in a human PR. Generated code MUST be fully typed. What
    counts as correct/better is per-target — see `sis/contract.py`.
- **Change-authorization policy (`sis/policy.py`) — what the loop may rewrite:**
  - FORBIDDEN (never, no override): guardrail/safety code — the gauntlet, the
    contract layer (`sis/contract.py`, `sis/backtest.py`, `sis/invariant.py`,
    `sis/clock.py`), the contract-author approval gate (`sis/contract_author.py`),
    `specs/` (the exam itself — oracles, acceptance tests, domain laws, backtest
    fixtures), cost/brakes, settings/secrets, **the configuration
    (`sis/config.py` + `config.yml`, OMNI-27)**, the adapters,
    `Dockerfile.gauntlet`, and the policy itself.
  - STRICT (off-limits unless `policy.allow_strict_changes` /
    `SIS_ALLOW_STRICT_CHANGES=1`, then needs human approval + justification +
    passing checks): all other engine code.
  - SOFT (optimisable; checks + review): the designated target(s) —
    `runtime/target.py`, `runtime/sort_target.py`, `runtime/roman.py` (Class 2,
    deliberately absent until an implementer writes it), extendable via
    `policy.target_paths` / `SIS_TARGET_PATHS`. **Guardrail classification always
    wins** — and that ordering is now load-bearing rather than tidy, because the
    target list is itself a config key: "name the config as a target, then
    rewrite the spend cap" is inert only because `classify` checks the guardrail
    list first. Asserted in `tests/test_config.py`.
- **The one write path into `specs/` is human-approved** (`sis/contract_author.py`,
  OMNI-21/26): a spec drafts into `runtime/contract_staging/` (loop-writable, no
  gate reads it), and `promote()` raises `RequiresHumanApproval` with no
  override. Worked examples stated in a spec transcribe into real assertions
  (values only, `ast.literal_eval` never `eval`); prose criteria and domain laws
  draft as `NotImplementedError` stubs, because the system cannot invent
  domain laws. A discrimination check runs the drafted exam against a null
  implementation before a human sees it, so an exam that asserts nothing is
  flagged rather than silently approved.
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
- CI (`.github/workflows/ci.yml`): `ruff` + `mypy --strict` + `pytest`. Runs on
  every push to `main`/`develop` and on **any PR regardless of base** (OMNI-16,
  fixed 2026-08-09) — the `pull_request` trigger used to filter on `[main,
  develop]`, which matches the PR's *base*, so a PR stacked on a feature branch
  skipped CI entirely and could merge looking green with zero verification
  (what happened to #67/#68). The required status-check context is `test`.
- Commit-lint (`.github/workflows/commit-lint.yml`): every non-merge commit newly
  introduced by a PR needs an `OMNI-N` key or a `No-Ticket:` trailer — the
  server-side backstop for the Jira-key convention above, for when
  `hooks/commit-msg` is bypassed or not enabled in a given clone. Runs on a PR
  against any base, same as `ci.yml` now. Required status-check context:
  `commit-lint`.
- After ANY `pyproject.toml` change, run `poetry lock` — CI fails on a stale lock file.
- `pyproject.toml`'s `version` tracks the last git tag (bump it as part of cutting
  a release, e.g. `git tag v0.1.5`); it had drifted to `0.1.0` across five
  releases (OMNI-22) before being corrected to match `v0.1.4`, the last actual
  tag — unreleased work on `develop` has no version number until its own tag.

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
- **`pytest` defaults to `-m "not serve" -n auto`** (fast inner loop, ~45s,
  parallel): it deselects the Ray Serve integration tests
  (`test_live_canary.py`, `test_serve_cloud.py`, `test_serving.py`,
  `test_loadgen.py`, `test_loop_serve.py`, ~65 tests), which stand up a real
  cluster/Serve deployment and take minutes serially. **Not a full verification
  by itself** — run `poetry run pytest -m serve -n 0` (serial: they share a
  cluster and a port) before trusting a change touches Serve, or let CI run
  both halves, which it always does explicitly (`tests/test_test_layout.py`
  pins that it must, so the excluded half can never silently run nowhere).
- Optional deps: `poetry install --with llm` (anthropic) · `--with real`
  (requests/boto3/pyyaml) · `--with analytics` (duckdb) · `--with ui` (panel).
- Operator UI (OMNI-28): `poetry run python -m sis.frontend`. Locally,
  `SIS_FRONTEND_AUTH=none` on the default loopback bind needs no OAuth app —
  GitHub exempts `localhost` from the HTTPS redirect rule. It refuses to start
  unauthenticated on any non-loopback bind. Panel is only partly annotated, so
  it is `follow_imports = "skip"` in the mypy overrides: otherwise the type
  gate would mean something different on a machine that has the `ui` group
  installed than in CI, which does not.
- **Configuration is one file: `config.yml`** (OMNI-27), generated from the
  schema in `sis/config.py` — every key with its default, doc, env var, and CLI
  flag. `poetry run python main.py --show-config` prints the effective values
  **and where each came from**. Don't restate defaults in prose anywhere: this
  list used to be a hand-maintained table in `README.md` and had already drifted.
  - Precedence: **CLI flag > env var > `config.yml` > built-in default.** Every
    legacy `SIS_*` var still works — it is the env layer.
  - Sections: `brakes`, `sandbox`, `policy`, `episodic`, `adapters`, `proposer`,
    `canary`, `loop`, `contracts`. Each key is prefixed `forbidden_` /
    `strict_` / `soft_`.
  - **The prefix gates the human operator UI (OMNI-28), not the loop.** The loop
    is stopped by `config.yml` and `sis/config.py` both being POLICY-FORBIDDEN,
    at every tier. Naming the config as an optimisation target buys nothing —
    guardrail classification is checked first, and `tests/test_config.py` pins it.
  - Add a knob by adding one `Key(...)` to `SCHEMA`, then
    `poetry run python -m sis.config --write` (which re-renders *around the
    values already in the file*, so adding a key doesn't reset operator edits).
    A test re-renders the committed file from its own values and compares, so
    its **structure** cannot drift from the code. Values may now differ from
    their defaults, because the operator UI writes here (OMNI-28) — except for
    `forbidden_` keys, which a separate test pins to their defaults so that
    deleting `config.yml` can never weaken a guardrail.
  - Secrets are **not** here (`config.yml` is committed) — they stay in
    `secrets.local.yml` behind `sis/settings.py`. `ANTHROPIC_API_KEY` is a
    credential, not a config key.
  - `SIS_CONTRACT`/`SIS_CANARY`'s BEFORE-launch caveat still applies to the *env*
    layer only (actors snapshot the environment at creation); the file and CLI
    layers do reach the actors. Prefer `--contract`/`run_cycle(contract_name=)`.
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
    gauntlet's sandbox.
- **The loop closes on a human merge** (`DevOps.observe_merge`, OMNI-15).
  `loop.serve(watch_merges=True)` (default) polls the pending PR while a canary
  holds green; when a human merges, the candidate is promoted, green is released
  and the next cycle starts — previously `--loop` ran one cycle and idled
  forever, because `Cloud.promote()` had no caller at all. Provenance now
  terminates in `promote` instead of stopping at `canary`.
- **`DevOps.canary()` can judge a candidate against real traffic**
  (`canary_backend="serve"`, OMNI-14; `--canary serve` / `SIS_CANARY=serve`;
  RUNBOOK Level 0e) — `sis.loadgen` fills the window itself (nothing external
  calls the target yet), forces `SHADOW` mode when the contract has no
  invariants (a split would have no live correctness signal), and a live
  rejection now changes the cycle's own outcome (`org.cycle_outcome`, pure) —
  QA approval alone no longer decides success. `DevOps` holds one `ServeCloud`
  per contract (`Workspace.cloud` is a single, contract-agnostic slot;
  `ServeCloud` needs a contract at construction) and opts in per call, so the
  legacy in-memory recording — and a zero-setup `main.py` — stay the default.
- **Class 2 (feature construction) shipped — the engine verifies two kinds of
  target now, not one** (OMNI-3 epic, closed 2026-08-11 except two low/parked
  items). `Contract.gate_profile()` (`sis/contract.py`) is what lets a
  `FeatureContract` and an `OptimizationContract` flow through one
  `gauntlet.validate()` with different gate stacks — see Hard rules above.
  Landed as five stories:
  - **OMNI-19 — `BacktestGate`** (`sis/backtest.py`): recorded episodes under
    `specs/<name>/`, a held-out `Split`, and a comparator *named* by the
    contract and resolved inside the sandbox (`specs/comparators.py` shared,
    or contract-local) — the seam where a future stochastic contract plugs in
    a proper scoring rule instead of `within_tolerance` without the gate
    itself changing. Re-prioritised ahead of OMNI-17/18 because
    `docs/OMNITRACK_VISION.md`'s Phases A/B (comparing model output against
    recorded reality) *are* this gate.
  - **OMNI-23 — `Clock` port** (`sis/clock.py`): `WallClock` (default) and
    `ReplayClock` (event time driven by a trace), so a backtest fixture's
    `event_time` is a parsed, timezone-aware instant, not an incidental string
    — landed ahead of the fixtures that need it, because a fixture recorded
    without event time can never be replayed and history can't be re-recorded.
  - **OMNI-17 — `FeatureContract` + contract-selected gate profile**: adds
    `determinism: Determinism` (default `DETERMINISTIC`) as a field on *any*
    contract rather than a third contract class — a slow Monte Carlo
    simulation is a stochastic Class-1 optimisation, which a linear
    Class-1→2→3 ladder has nowhere to put. A `STOCHASTIC` contract's interface
    gate additionally requires a `seed` parameter on the entry point. First
    Class-2 target: `roman` (`to_roman`/`from_roman`, `specs/roman/`).
  - **OMNI-18 — `InvariantGate`** (`sis/invariant.py`, Hypothesis): domain
    laws over generated inputs, resolved by name inside the sandbox exactly
    like `Backtest.compare`. Two predicate shapes — `check(args, output)` is
    also usable by the live canary (`sis.canary.BoundInvariant`); `check(args,
    output, impl)` gets the candidate module *and* its bound entry (for a
    round-trip law that must call a sibling export) and is offline-only by
    construction, since re-invoking a candidate on a live response would
    change what production does. Every run is seeded and the seed rides in
    the reject reason, because Hypothesis's example database is disabled in
    the sandbox — the seed is the only way to reproduce a shrunk
    counterexample later.
  - **OMNI-21 + OMNI-26 — contract-author, the only write path into `specs/`**
    (`sis/contract_author.py`): draft (in-process) → stage
    (`runtime/contract_staging/`, loop-writable, no gate reads it) →
    `specs/` (`promote()` raises `RequiresHumanApproval`, no override, no
    `force`). Worked examples stated in a spec (`` `f(4) -> "IV"` ``) transcribe
    into real parametrised assertions — values only, via `ast.literal_eval`,
    **never `eval`**, since a spec page is prose an outside author can
    influence. Prose criteria/laws draft as `NotImplementedError` stubs; the
    system cannot invent domain laws, so a skeleton that guessed would produce
    an exam that looks authored and checks nothing. `check_discrimination`
    runs the drafted exam against a null implementation before a human sees
    it and reports (not enforces) whether it asserts anything — a code-review
    pass on the initial OMNI-26 landing found the first version of this check
    read pytest's *exit code*, which a `NotImplementedError` stub always makes
    non-zero, so it reported "discriminates" for exams asserting nothing; it
    now parses JUnit XML per-test. That review also found unescaped spec
    prose could break out of a generated docstring and execute at stage time
    (fixed: `repr`, never raw interpolation) and that the check bypassed the
    M1 sandbox backstop (fixed: `gauntlet.ensure_sandbox_ready()`, one
    precondition called by every executor of generated code).
  - **Epic closed 2026-08-27.** Two stories were detached to standalone
    backlog items rather than closed with it, since both are still wanted and
    neither is on the critical path: OMNI-24 (`SloGate` — a latency/accuracy
    budget, explicitly *not* a correctness gate) and OMNI-20
    (`ToolchainAdapter` — language genericity), parked.
- **`docs/OMNITRACK_VISION.md`** sequences what comes after Class 2: five new
  components (`Sensor`+`Clock` ports, the determinism axis above,
  stateful-actor swap, per-actor deploy slots, an emergence gate) across
  phases A–F, plus a 13-decision register (D0–D12, each with a
  recommendation and a "decide by" phase). Of the five components only the
  `Clock` half of E1 exists (`sis/clock.py`, OMNI-23); **Phase A is filed as
  OMNI-30** — the `Sensor` port, the first `RealSensor`, the `SimSensor`, and
  the first recorded fixture.
  - **The register is settled as of 2026-08-27: D0–D9, D11 and D12 are all
    decided; D10 alone stays open** (does the emergence gate block promotion —
    not due until Phase F, and unanswerable before E5 has run advisory).
    Read the doc for the full text; the load-bearing ones:
    - **D0 — regional air traffic** (OpenSky / ADS-B Exchange), OMNI-25 Done.
      Richest, highest-frequency public data of the four candidates; also the
      heaviest scope/optics tax, the one candidate where "wrong is dangerous"
      needs active management even for a passive twin.
    - **D2 — twin state is externalised** behind a `StateStore` port, DuckDB
      and human-readable, with an *optional, non-default* per-transition log
      carrying clock time + cause so reasoning can be replayed.
    - **D3 — runtime LLMs are allowed**, against the original recommendation,
      but only within mockable boundaries: each LLM-dependent actor gets its
      own LLM interface that tests can stub with canned responses; responses
      are schema-constrained and treated as untrusted input; runtime calls
      must come under the CEO brakes (which today meter only the proposer).
      **Known gap:** an LLM-backed actor is STOCHASTIC on the E2 axis, but
      E2 requires determinism under seed, which no LLM API provides
      (temperature 0 is not determinism). First such actor needs its own
      design note.
    - **D4 — recorded, human-approved real data is the preferred promotion
      evidence**; simulated data only where no recording exists; and a
      candidate faces real traffic before promotion, judged on error rate,
      format/volume handling, and responses landing in an expected range.
    - **D7 — the simulator is an improvable target**, but improvements are
      judged only against held-out *real* traces, never its own output, with
      the copy that generates gauntlet inputs pinned and FORBIDDEN.
    - **D9 — two triggers, deliberately not one — but both spend.** An
      *absolute* error budget answers "is the twin fit for purpose now"
      (→ alert a human, downgrade confidence, *and* start a cycle — a harder
      world absolutely triggers an improvement, since the twin must adapt to
      the world as it now is); *skill vs climatology* answers "is there
      headroom a code change could capture" (→ spend LLM budget on a cycle,
      no human alert needed). They stay separate triggers because they detect
      different situations — a world that got harder is not a model that got
      worse — not because one of them is forbidden to spend.
    - **D8 / D12 — humans decide, actors may propose.** The loop can propose
      the actor decomposition and its own exam; a human reviews, edits,
      approves or rejects. `specs/` write access stays with the
      human-approved path.
- **Configuration is one schema (OMNI-27, PR #92, merged 2026-08-16).** `sis/config.py` declares
  every knob once; `config.yml` is generated from it and committed; precedence
  is CLI flag > env var > file > built-in default; `main.py --show-config`
  prints the effective values *and their source*. Every legacy `SIS_*` var
  still works as the env layer. Both files are POLICY-FORBIDDEN. See the
  Operational quick reference above for how to add a knob. Two silent failure
  modes closed on the way: `SIS_SANDBOX=dcoker` used to compare unequal to
  `"docker"` and run untrusted code in the *soft* sandbox saying nothing, and
  `SIS_ALLOW_STRICT_CHANGES=yes` used to mean "disabled". Both now raise.
- **Operator frontend, first slice (OMNI-28, PR #93).** Panel (`sis/frontend.py`,
  `poetry install --with ui`, `python -m sis.frontend`) renders system state +
  every config key with its tier and *source*. Four things worth knowing:
  - **The tier gate is in `sis/operator.py`, not the browser.** A disabled
    widget is a courtesy; `save_edits()` refuses a `forbidden_` key and an
    unconfirmed `strict_` key whatever the caller sends, and edits are
    all-or-nothing so a rejected third edit leaves the first two unwritten.
  - **Operator edits land in `config.yml` itself** — no gitignored overlay, so
    a changed knob stays a reviewable diff. That cost one guarantee and
    replaced it with a sharper one: `render_config_file(values)` now renders
    current values and the drift test re-renders from the file's *own* values
    (still catching added/removed keys, stale docs, wrong tier prefix,
    reordering), while a new test pins that **no `forbidden_` key may differ
    from its default** — so deleting `config.yml` can never weaken a guardrail.
  - **Auth is GitHub OAuth, and the settings that govern it are `forbidden_`**
    (`frontend.auth`/`allowed_logins`/`bind`): an operator who could append a
    login through the UI those settings guard would be escalating through its
    own front door. Empty allowlist denies everyone; `auth: none` is refused on
    a non-loopback bind (same shape as the M1 sandbox rule). Credentials go in
    `secrets.local.yml`, never `config.yml`.
  - **A shadowed key is shown as shadowed.** `config.yml` is the third of four
    layers, so an edit to a key currently set by `SIS_*` saves correctly and
    changes nothing; the UI says so at the point of editing.
  - Design + the Caddy/TLS decision: `docs/OPERATOR_FRONTEND.md`. Deployment
    artifacts (`Dockerfile.frontend`, `Caddyfile`) are deliberately not in this
    slice.
- 518 tests (`pytest -m "not serve" -n auto`, the default, ~46s; the ~62
  Ray-Serve-integration tests run separately, see Operational quick reference
  above); `ruff`/`mypy --strict`/`pytest` clean; CI green; `feature → develop
  → main` enforced by both the client-side pre-push hook and active
  server-side rulesets.
- **Two known test flakes, both pre-existing and both parallel-execution
  artifacts** (noted 2026-08-14; **not ticketed** — no Jira issue exists for
  either, confirmed against the board 2026-08-27):
  `test_correct_but_not_faster_is_rejected`
  measures a fresh baseline, which gets noisy under xdist CPU contention
  (passes 5/5 in isolation); `test_a_drafted_skeleton_stages_without_touching_specs`
  compares two `specs/` listings and races another worker creating
  `specs/__pycache__`. Neither indicates a real defect — re-run before chasing.

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
not here.** Check the board for current status rather than trusting this list.
**Last reconciled against a live query on 2026-08-27** (30 issues, OMNI-1
through OMNI-30; 25 Done, 5 open):

1. ~~**[OMNI-1](https://olafzumpe.atlassian.net/browse/OMNI-1) — L5 target
   contract** (Class 1)~~ — **done 2026-08-06** (OMNI-4/5/6/7). Two targets ship
   and the engine names neither. Design: `docs/CLASS2_CONTRACT.md`.
2. ~~**[OMNI-2](https://olafzumpe.atlassian.net/browse/OMNI-2) — Ray Serve
   canary.**~~ — **done 2026-08-09** (OMNI-8/9/11/12/13/14/15). The full
   mechanism works end to end: `DevOps.canary(canary_backend="serve")` deploys
   behind Ray Serve, fills the window itself, `evaluate_canary()` decides — a
   live rejection now changes the cycle's outcome — and a human merge is
   observed and promotes automatically. Opt-in; the legacy in-memory path
   stays the default. Design: `docs/SERVE_CANARY.md`.
3. ~~**[OMNI-3](https://olafzumpe.atlassian.net/browse/OMNI-3) — Class 2**,
   feature construction~~ — **done 2026-08-11, epic closed 2026-08-27**
   (OMNI-17/18/19/21/23/26; see "Current status" above for what each landed).
   `FeatureContract` + the contract-selected gate profile, `InvariantGate`,
   `BacktestGate`, the `Clock` port, and the contract-author write path all
   shipped. OMNI-24 (`SloGate`, Low) and OMNI-20 (`ToolchainAdapter`,
   Low/parked) were **detached** to standalone backlog items rather than closed
   with the epic — both still wanted, neither on the omnitrack critical path.
   Design: `docs/CLASS2_CONTRACT.md`.
4. ~~**[OMNI-25](https://olafzumpe.atlassian.net/browse/OMNI-25) — D0: pick
   omnitrack's first domain**~~ — **Done 2026-08-14: regional air traffic**
   (OpenSky / ADS-B Exchange). It fixes the first `RealSensor` adapter and the
   domain of Phase A. D8 (who decomposes it into modelled actors) is a
   separate, still-open decision.
5. ~~**[OMNI-27](https://olafzumpe.atlassian.net/browse/OMNI-27) — unified
   config**~~ — **done 2026-08-16**, PR #92 merged to `develop`. One schema,
   `config.yml`, env/CLI override; see "Current status" above.
6. **[OMNI-28](https://olafzumpe.atlassian.net/browse/OMNI-28) — operator
   frontend.** **First slice in PR #93** — Panel app, tier-gated write path,
   GitHub OAuth, `frontend.*` schema keys; see "Current status" above for what
   each decision cost. Still open: the deployment artifacts
   (`Dockerfile.frontend` + `Caddyfile` for TLS on 443 in front of Panel's
   8080), and whether this is exposed publicly at all — a tunnel or Tailscale
   with a loopback bind is a stronger posture than any TLS config for a console
   that edits the settings of a system which runs generated code. Design:
   `docs/OPERATOR_FRONTEND.md`.

7. **[OMNI-29](https://olafzumpe.atlassian.net/browse/OMNI-29) — first AWS
   run** (one node, a few supervised cycles — watch the provenance graph and
   the bill). Designed + Terraformed: `docs/AWS_RUN.md` is the design note and
   runbook, `infra/aws/` the (small) Terraform. One m7i.xlarge, **zero ingress
   ports** (SSM Session Manager only; the OMNI-28 console stays loopback-bound,
   reached over SSM port forwarding), instance role + one Secrets Manager
   secret whose value never enters Terraform state, episodic log synced to S3
   as the run's durable artifact, and two *independent* spend brakes
   (`SIS_BUDGET_USD` in the loop; an AWS Budget alarm in billing). Not applied
   yet. Explicitly **not** RUNBOOK Level 4: no autostart, no autonomy — a human
   starts, watches, and stops it.

8. **[OMNI-30](https://olafzumpe.atlassian.net/browse/OMNI-30) — omnitrack
   Phase A: the `Sensor` port.** `To Do`, filed 2026-08-27 — the first epic of
   omnitrack proper, unblocked by D0 and by OMNI-3 closing. `Sensor` port +
   `SimSensor` default + an OpenSky/ADS-B `RealSensor`, the first recorded
   fixture, and prediction error computed but not yet acted on (that is Phase
   B). **Sanitising sensor input is inside the adapter story, not later
   hardening** — ADS-B is outside-influenceable data that reaches scenario
   libraries, backtest fixtures and potentially LLM prompts.

Not yet scheduled: the **atomic actor swap** for internal, never-served actors,
which `docs/SERVE_CANARY.md` scopes out and which has no design doc yet. E3/D2 in
`docs/OMNITRACK_VISION.md` either is that design or subsumes it — whoever
schedules either piece writes the one document, so drain/handoff machinery isn't
built twice. With D2 now decided (state externalised behind a `StateStore` port),
that document has its central question already answered.
