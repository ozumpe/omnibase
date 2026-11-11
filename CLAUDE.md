# Self-Improving Server (Python + Ray) — "omnibase"

This file is loaded into every session, so it stays short (a test caps it at
200 lines). Each rule is one line here; its detail and its reasons are in
`docs/ENGINE_RULES.md`. History goes in `CHANGELOG.md` and the Jira ticket,
never here and never in a docstring.

**Read when you need it:**
- `DESIGN.md`: the brief. `ACTORS.md`: roles, ports and the self-model.
- `docs/ENGINE_RULES.md`: every rule below in full. Read the section before changing the code it covers.
- `docs/CODE_TOUR.md`: Python + Ray walkthrough. `CONTRIBUTING.md`: contributor flow.
- `docs/CLASS2_CONTRACT.md`: feature contracts. `docs/STAGED_DELIVERY.md`: feature → develop → main, the sandbox worker, develop blue/green, the agent's own review.
- `docs/OMNITRACK_VISION.md`: what comes next and decisions D0–D12. Read it before designing anything the twin touches.
- `docs/SENSOR_RUNBOOK.md`: capture, replay and fixtures. `docs/AWS_RUN.md`: the AWS box. `docs/SERVE_CANARY.md`: the Serve canary.
- `docs/KNOWN_ISSUES.md`: open defects (H/M/L IDs). `CHANGELOG.md`: what each release carried.

**Naming:** *omnibase* = this engine (the `sis/` package). *omnitrack* = the
product it is building toward: a model of an external slice of the world.

## What this is
A server that models a slice of the real world through an actor hierarchy and
extends itself by generating, validating and safely deploying its own code from
specs. The loop: monitor → budget/goal gate → propose on a branch → validation
gauntlet → canary → human merge → promote/rollback → log → circuit breaker.

## Stack (decided, don't relitigate without reason)
- Python 3.14, standard CPython, NOT free-threaded (Ray has no `cp314t` wheels). Poetry.
- Ray for actors, Ray Serve for the opt-in canary. Apache 2.0; never copy Akka source.

## Architecture in one screen
- CEO/CTO/PM + SWE/QA/DevOps/Designer are Ray actors; `Workspace` and `SelfModel` hold shared state.
- Actors coordinate through artifacts (Confluence, Jira, Git), not in-memory chat.
- External systems sit behind ports in `sis/ports.py`, in-memory by default, real ones in `sis/adapters_real.py` (`SIS_ADAPTERS=real`): Document Store, Work Tracker, Version Control, Cloud, Telemetry, Notifier (SNS), Sensor.
- **Sensor:** a `Reading` has a unit, `event_time` and `known_at`; every read names its `TimeAxis`. A replay never shows a value before its `known_at`.
- **Sensor input is untrusted.** It enters only through `sensor_input.sanitise`; a real adapter builds no `Reading` itself.
- `EIASensor` captures EIA's bulk file into an append-only trace (`scripts/capture_sensor.py`, `runtime/sensor_traces/eia.jsonl`).
- **The LA spot price is licensed** (Thomson Reuters via EIA): model it, never redistribute it. Fixtures that need it are private (`specs/<name>/private/`, gitignored, sha256 in `private.json`).
- Scenarios (`specs/scenarios.py`) are seeded and can only refute a candidate, never promote one (D4).
- Prediction error and skill against climatology are computed and reported, never acted on (`sis/prediction_error.py`). Phase B's two triggers are decided in `sis/model_error.py`, not yet wired.
- Episodic store (`sis/episodic.py`) records every cycle: spec → diff → verdict → outcome. Backends: jsonl (default), duckdb, none.

## Hard rules
Detail for each: `docs/ENGINE_RULES.md` § Hard rules.
- **Generated code never runs in the main process.** Every gate is sandboxed (`SIS_SANDBOX=subprocess|docker`). A real proposer requires docker.
- **No gate shares a process with the candidate it judges, except SLO** (L42, OMNI-86). Harnesses run on the host and call a `SandboxWorker` over JSON.
- **A Serve replica never runs its candidate.** It forwards to a sandbox worker. `--canary serve` stays refused with any non-stub proposer until a human lifts it (OMNI-49).
- **The contract picks the gates** (`Contract.gate_profile()`):
  - Class 1 (make it faster): syntax → no-op → mypy → interface → acceptance → invariants → backtest → differential + paired benchmark.
  - Class 2 (build a feature): syntax → mypy → interface → acceptance → invariants → backtest → optional SLO.
- **The benchmark decides on total cost with a paired-bootstrap interval.** No input is timed twice. Timed batches run no per-call Python. Each worker gets its own CPU. Before touching it, read the benchmark section of `docs/ENGINE_RULES.md`.
- **A forecast must beat its baseline on held-out history** (`BacktestSkill`). A rejection shows neither error.
- **Output is compared as plain values**, and each side gets its own copy of the input (`sis/canonical.py`).
- **The exam is read-only to the candidate.** Exam files are hashed and checked after every gate, and a gate needs a positive verdict token, not just exit 0.
- **A broken sandbox is never blamed on the candidate** (`gauntlet.probe_sandbox`, `harness:` verdicts).
- **No feature PR opens without the agent's own approving review** (`sis/review.py`).
- **Policy (`sis/policy.py`):**
  - FORBIDDEN (never, no override): gauntlet, canonicaliser, sandbox worker, review, contract layer, ports, sensor door and trace, replay, fixture builder, scenarios, prediction error and its triggers, contract author, `specs/`, cost, config, settings, adapters, `Dockerfile.gauntlet` and the policy itself.
  - STRICT: other engine code; needs an opt-in plus human approval.
  - SOFT: the `runtime/` targets.
  - The guardrail classification always wins.
- **The only write path into `specs/` is human-approved** (`sis/contract_author.py`, `promote` with a reviewed digest). Automating it is OMNI-215.
- **The loop never merges.** `merge_pr()` raises in every adapter. Promotion follows only an *observed* human merge (`DevOps.observe_merge`).
- **Brakes:** hard spend cap, cost per accepted improvement, and a circuit breaker.
  - Brake state fails closed; spend is reserved before it is spent; an older snapshot never replaces a newer one.
  - An exception inside a cycle is a charged outcome.
  - One loop per box (`runtime/loop.lock`).
  - A trip pages a human through SNS.
- **Secrets** live in `secrets.local.yml` (gitignored) or AWS Secrets Manager. Never commit tokens.

## Repo & GitHub workflow
- Repo `github.com/ozumpe/omnibase` (public). Default branch `develop`.
- Flow: `feature/<name>` → `develop` → `main` (releases).
- **Never push to `develop`/`main` directly.** `git config core.hooksPath hooks` installs the guard; server rulesets enforce it too.
- **Every commit needs an `OMNI-N` key or a `No-Ticket: <reason>` trailer.** `hooks/commit-msg` checks it locally; the `commit-lint` CI job is the backstop. The release PR is exempt.
- CI (`ci.yml`) runs on every PR, whatever its base:
  - `lint`, `fast` and `serve` run in parallel;
  - a docs-only PR (only `.md`/`.svg`/`.png`) runs just `docs`;
  - `test` aggregates them and is the required check.
- **A test that reads repo Markdown must be marked `docs`.**
- After any `pyproject.toml` change, run `poetry lock`.
- Cutting a release:
  - bump `pyproject.toml`'s version and the infra `repo_ref` defaults (`tests/test_release_ref.py` pins them);
  - add the CHANGELOG section;
  - open a release PR from develop to main;
  - Olaf pushes the tag.

## Conventions
- `ruff`, `mypy --strict` and `pytest` are green before a PR.
- Decision logic lives in pure functions; I/O lives in actors.
- Every bug becomes a permanent regression test.
- Keep PRs small. Put the Jira key in each commit and PR title, e.g. `OMNI-6: contract-derived proposer prompt`.
- KNOWN_ISSUES entries start with their Jira ticket (`tests/test_known_issues.py`). File the ticket first. When an entry is fixed, add a CHANGELOG line and a row in the Resolved table.

## Operational quick reference
- One cycle, in-memory: `poetry run python main.py`.
- Gates: `poetry run pytest` (fast half, `-n auto`), `poetry run pytest -m serve -n 0` (Serve half, serial), `poetry run mypy --strict sis/ main.py scripts/`, `poetry run ruff check .`.
- The suite ignores your shell's `SIS_*` variables and credentials. A test that needs a real backend asks for `real_env`.
- Known flakes (L58, L59, L69): re-run, then file or update the ticket. Never call a failure noise.
- Optional deps: `poetry install --with llm|real|analytics|ui`.
- **Configuration is one file, `config.yml`, generated from `sis/config.py`.**
  - Precedence: CLI > env > file > default.
  - `main.py --show-config` shows each value and where it came from.
  - Add a key in `SCHEMA`, then run `python -m sis.config --write`.
  - Don't restate defaults in prose.
- Real adapters: copy `secrets.example.yml` to `secrets.local.yml`, then run `scripts/check_connections.py --deep`.
- Operate a running loop: `python -m sis.admin status|pause|resume|reset-breaker --reason "..."`.
- Operator UI: `python -m sis.frontend`. Twin viewer: `python -m sis.viewer` (reach it as `localhost`).
- Docker sandbox image: `docker build -t sis-gauntlet:latest -f Dockerfile.gauntlet .`
- Planning lives in Jira project `OMNI` (cloudId `760ca470-0091-4601-9704-a56633b5e9b6`), the source of truth for what to work on next. `TES` is the loop's own scratch project. Confluence space SD mirrors the docs.

## Current status
Latest release: **v0.6.0** (2026-11-10). The AWS box runs the latest release
tag, not `develop`. What to work on next is in Jira, not here; defects are in
`docs/KNOWN_ISSUES.md`.
- omnitrack Phase A is done.
- `ca_price_wholesale` (OMNI-163) is the first gated omnitrack contract; its private fixture is promoted.
- Phase B is epic OMNI-194.
- Engine work continues under staged delivery (OMNI-128).

Two traps worth knowing before writing similar code:
- Anti-gaming is only as strong as the input distribution.
- Env vars don't reach the role actors after `bootstrap()`. Pass per-cycle settings as arguments.
