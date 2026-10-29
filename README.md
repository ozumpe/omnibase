# Self-Improving Server (Python + Ray)

A Python + Ray engine that generates and validates target implementations from
human-defined contracts. The goal is to extend it into omnitrack, a server that
models a slice of the real world and simulates outcomes under specified inputs.

**Bootstrap goal first:** before it models anything external, the server must be
self-improving on a trivial *internal* target — receive a spec, generate code,
validate it through a hard gauntlet, deploy it on a canary, and roll back on
regression. That loop works today, driven by an actor org (CEO/PM/CTO/SWE/QA/
DevOps/Designer) that coordinates through durable artifacts (Confluence/Jira/
GitHub), with a SelfModel registry tracking the running system. External-domain
modeling is still planned; the EIA sensor captures observations but no domain
model consumes them in the control loop yet.

> **New to the code?** [`docs/CODE_TOUR.md`](docs/CODE_TOUR.md) is a reading
> guide written for people learning Python + Ray. Want to contribute? See
> [`CONTRIBUTING.md`](CONTRIBUTING.md). Licensed [Apache 2.0](LICENSE).
>
> Authoritative specs: [`DESIGN.md`](DESIGN.md) (detailed brief),
> [`ACTORS.md`](ACTORS.md) (role + subsystem spec), [`CLAUDE.md`](CLAUDE.md)
> (working rules). Current runtime diagrams: [`docs/DIAGRAMS.md`](docs/DIAGRAMS.md).
> The SVGs `ray_self_improving_control_loop.svg` and
> `actor_org_interaction_map.svg` illustrate the original conceptual design.

---

## Quick start

```bash
poetry install
poetry run python main.py          # run one full org cycle (in-memory, no creds)
poetry run python main.py --loop   # repeat demo intake; holds for review and stops on convergence
poetry run pytest                  # fast test suite (the Ray Serve half: see Development)
```

`python main.py` simulates an intake proposal and drives it all the way to a
validated, canary-deployed change — printing the cycle status, the
before/after benchmark, the provenance graph, and the live actor registry.
Nothing external is required: the default adapters are an in-memory artifact
bus.

Requirements: **Python 3.14** (standard CPython, not free-threaded — Ray ships
`cp314` wheels but not `cp314t` yet), managed with Poetry (or `uv`).

---

## Configuration & run modes

**Every knob lives in [`config.yml`](config.yml)** — one file, generated from
the schema in `sis/config.py`, with each key's default, its documentation, its
environment variable, and its CLI flag written next to it. The defaults need no
setup, credentials, or extra installs.

```
CLI flag  >  environment variable  >  config.yml  >  built-in default
```

```bash
poetry run python main.py --show-config      # every value + where it came from
poetry run python main.py --sandbox-mode docker --brakes-budget-usd 0.05
export SIS_BUDGET_USD=0.05                   # same key, environment layer
```

Keys are grouped by function (`brakes`, `sandbox`, `policy`, `episodic`,
`adapters`, `proposer`, `canary`, `loop`, `sensor`, `contracts`, `frontend`) and each is prefixed with
how strongly it is protected — `forbidden_`, `strict_`, `soft_`. That prefix
governs what a **human operator** may change (the operator console,
`python -m sis.frontend`, shows `forbidden_` keys read-only, and its write path
refuses direct edits to them; a `strict_` edit needs a written justification).
The current console has known OAuth and YAML-rendering defects (OMNI-53/52);
see [its deployment limits](docs/OPERATOR_FRONTEND.md). A separate, read-only
twin viewer (`python -m sis.viewer`, OMNI-40) replays the recorded fuel data and
generated scenarios under the same serving rules. The prefix grants the
loop nothing, because `config.yml` and
`sis/config.py` are both POLICY-FORBIDDEN and the loop may not write either at
any tier. Naming the config as an optimisation target doesn't help — guardrail
classification is checked first (`tests/test_config.py`).

Because this file is committed, **secrets do not go in it** — those live in the
gitignored `secrets.local.yml`; see [Connecting to the real world](#connecting-to-the-real-world).

The three worth knowing before a real run:

| Key (env var) | Why it matters |
|---|---|
| `sandbox.mode` (`SIS_SANDBOX`) | `docker` is kernel-enforced (`--network none`, no creds). **Required** whenever `proposer.backend` isn't `stub` — untrusted LLM code can read host files in `subprocess` mode, and the loop refuses to start without it. |
| `proposer.backend` (`SIS_PROPOSER`) | `stub` is offline and free. Any other value is a real LLM via `sis.llm` (needs `poetry install --with llm` + a provider key) and is treated as untrusted. |
| `brakes.budget_usd` (`SIS_BUDGET_USD`) | CEO hard spend cap. Set a **tiny** value for a first real run so the brakes trip early. A bad value fails loudly rather than reverting to a default. |

`ANTHROPIC_API_KEY` is not configuration and is not in `config.yml`: it is a
credential, required for the `anthropic` provider. Other providers read their own key.

`contracts.default` (`SIS_CONTRACT`) and `canary.backend` (`SIS_CANARY`) are read
inside the role actors, which are separate processes that snapshot the
environment when created — so the *environment* layer only reaches them if
exported before launch. `config.yml` and the CLI flags do reach them; the
per-cycle `main.py --contract <name>` / `run_cycle(contract_name=...)` remains
the precise mechanism.

### Common workflows

```bash
# 1. Default — fully local, offline, no credentials
poetry run python main.py

# 2. Real Claude proposer (LLM writes the optimisation; gauntlet still gates it).
#    An LLM writes untrusted code, so the kernel-enforced docker sandbox (below)
#    is REQUIRED — the loop refuses SIS_PROPOSER=claude without SIS_SANDBOX=docker.
poetry install --with llm
docker build -t sis-gauntlet:latest -f Dockerfile.gauntlet .   # once
export SIS_PROPOSER=claude SIS_SANDBOX=docker ANTHROPIC_API_KEY=sk-ant-...
poetry run python main.py

# 3. Kernel-enforced sandbox on its own (e.g. with the stub proposer)
docker build -t sis-gauntlet:latest -f Dockerfile.gauntlet .
export SIS_SANDBOX=docker
poetry run python main.py

# 4. Talk to real Confluence/Jira/GitHub/AWS
cp secrets.example.yml secrets.local.yml      # fill in tokens
poetry install --with real
export SIS_ADAPTERS=real
poetry run python scripts/check_connections.py --deep   # read-only preflight (+ Jira workflow)
poetry run python main.py

# Combine freely, e.g. real LLM + kernel sandbox + real adapters:
export SIS_PROPOSER=claude SIS_SANDBOX=docker SIS_ADAPTERS=real
```

The CEO's spend brakes (hard cap + cost-per-accepted SLO) apply automatically
once `SIS_PROPOSER=claude` is spending real tokens; tune them via the
environment (`SIS_BUDGET_USD`, `SIS_MAX_COST_PER_ACCEPTED_USD`, …) — set a tiny
`SIS_BUDGET_USD` for a first real run so the brakes trip early.

---

## How it works

The control loop, mapped onto the actor org:

```
monitor → budget/goal gate → propose → validation gauntlet → canary → promote/rollback → log → circuit breaker
              CEO            SWE        gauntlet + QA          DevOps    human PR merge     SelfModel   CEO
```

At bootstrap, the **CEO** writes a top-level charter once (idempotent); every
cycle's provenance then roots at it (`charter → spec → epic → story → outcome`).

One **cycle** (`sis/org.py :: run_cycle`):

1. **CEO** gates on pause, breaker and budget. A Class-2 target already built is
   revalidated before any plan is created; passing returns neutral `no_gain`.
2. A proposal is dropped into the Confluence *proposal* space (intake).
3. **PM** refines it into a spec page; **Designer** adds an outline.
4. **CTO** turns the spec into a Jira epic + stories.
5. **SWE** proposes and validates a step, committing only a passing candidate
   to its feature branch. Optimization features can accumulate accepted steps
   before opening one fixed-head PR. A Class-2 feature opens its PR at the first
   passing step. An unfinished optimization returns `feature_step` and continues
   on the next cycle; existing plans and attempt feedback are reused. See
   [staged delivery](docs/STAGED_DELIVERY.md).
6. **QA** re-runs the deterministic gauntlet and verifies the story.
7. **DevOps** canary-deploys to the **green** slot.
8. Promotion to live = the **human PR merge** — intentionally *not* done by the
   agent. The cycle ends at `verified_awaiting_human_merge`; in `--loop` mode
   the loop then watches the PR, and once a human merges it promotes the
   canary, releases the green slot and starts the next cycle (OMNI-15).

Every handoff is an artifact state change; every step is recorded in the
**SelfModel** provenance graph.

**On a counted failure** (gauntlet rollback at step 5, or QA rejection at step 6), the
cycle doesn't just log and stop: **DevOps files a bug** in the work tracker
carrying the story + reason, so a rejected diff is a durable artifact, not just
an episodic-log line. The configured failure threshold trips the breaker and
files a distinct `CIRCUIT BREAKER OPEN` bug; the Notifier also pages a human.
No-op, no-gain and inconclusive outcomes are neutral: spend is recorded without
filing a bug or incrementing the breaker. Exceptions after a proposal are
charged and recorded as `error` outcomes. `loop.converged_after` no-op or
no-gain cycles in a row stop the loop as converged; an inconclusive one resets
that count, since only a candidate that looked faster reaches it (OMNI-145).

### The validation gauntlet (`sis/gauntlet.py`)

The gauntlet checks cheapest-first and keeps candidate execution out of the
main process. Trusted acceptance, invariant and backtest harnesses run on the
host and call the candidate's sandbox worker over JSON; the host also owns the
benchmark clock, reference and verdict. The SLO gate is the remaining gate
that judges its candidate in the same sandboxed process.
**Which gates run is chosen by the target's contract** (`Contract.gate_profile()`
in `sis/contract.py`); both task classes flow through one `validate()`:

| Gate | Class 1 — make it faster (`OptimizationContract`) | Class 2 — build a feature (`FeatureContract`) |
|---|---|---|
| `ast.parse` (syntax) | ✅ | ✅ |
| no-op (identical to the baseline) | ✅ | — nothing to be identical to |
| `mypy --strict` (types) | ✅ | ✅ |
| interface (exports what the contract names) | ✅ | ✅ |
| acceptance (the contract's trusted tests, from `specs/`) | ✅ | ✅ |
| invariant (domain laws over Hypothesis-generated inputs) | ✅ | ✅ |
| backtest (reproduces recorded episodes, held-out split) | ✅ | ✅ |
| differential correctness on random inputs + benchmark vs a fresh baseline (paired timing on fresh inputs; accept / reject / inconclusive, OMNI-41) | ✅ (≥10% faster in total by default) | — no reference exists, and "faster" isn't correctness |
| SLO (optional latency budget) | — | ✅ when the contract declares one; no shipped contract does yet |

The exam each gate reads lives in `specs/<target>/`, which is POLICY-FORBIDDEN:
the implementer cannot edit its own exam. After the gates, every change still
ends in a **human PR** once the feature is finished — the agent never merges.

The sandbox has two modes via `SIS_SANDBOX`: `subprocess` (default — scrubbed
env + in-process network block; host files stay readable) and `docker`
(`--network none --cap-drop ALL --read-only`, only the temp dir mounted; build
the image once with `docker build -t sis-gauntlet:latest -f Dockerfile.gauntlet .`).
A real (non-stub) proposer requires `docker`. Every gate also has a wall-clock
timeout (`SIS_GAUNTLET_TIMEOUT`; see `config.yml`) so an infinite loop in generated
code is killed, not left to hang.

**Invariant:** candidate or target code never runs in the main process, where
credentials live. Candidate gates and baseline measurement use the gauntlet's
sandbox; the recording canary only records measured latency. The Serve canary's
replicas are Ray workers, so they never run a candidate themselves: each
forwards requests to the candidate in a sandbox worker (OMNI-48). `--canary
serve` is still refused for non-stub proposers, with no override, until a human
lifts that refusal (M19); it is also refused for Class-2 contracts. Passing offline gates does not remove that restriction.
See [the execution model](docs/CODE_TOUR.md#5-the-gauntlet-gate-by-gate-sisgauntletpy).

### Provenance / episodic store (`sis/episodic.py`)

Every cycle records an event — spec → diff → gauntlet verdict → outcome,
**including each rejected diff and the gate that caught it** — so the log is the
dataset the system learns from, not an afterthought. It sits behind an
`EpisodicStore` port selected by `SIS_EPISODIC_STORE`:

- `jsonl` (default) — append-only, zero-dependency, durable.
- `duckdb` — embedded SQL analytics over the events (`poetry install --with
  analytics`); `summary()` rollups (reject-rate by gate, cost-per-accepted) plus
  an `sql()` escape hatch.
- `none` — disabled; refused with a real proposer or real adapters, because
  spend/brake state must persist.

Postgres + pgvector can be added as another backend later (multi-node cluster /
embedding retrieval) without changing the loop.

---

## Project layout

```
sis/                        # the engine (STRICT/FORBIDDEN to the loop — policy.py)
  roles.py                  # CEO/PM/CTO/SWE/QA/DevOps/Designer actors
  org.py                    # bootstrap + run one intake→deploy cycle
  loop.py                   # the long-running driver (main.py --loop)
  workspace.py              # shared artifact-bus Ray actor (picks adapters by config)
  self_model.py             # SelfModel/Registry digital-twin Ray actor
  ports.py                  # capability interfaces (Protocols) + artifact types
  adapters.py               # default in-memory adapters (the artifact bus)
  adapters_real.py          # real Confluence/Jira/GitHub/AWS adapters + EIASensor
  proposer.py  llm.py       # propose a candidate: offline stub, or any LLM via a provider port
  contract.py               # what "correct" and "better" mean per target; picks the gates
  gauntlet.py               # contract-selected gates; host harnesses + isolated workers
  canonical.py              # rebuild candidate output from plain builtins before ==
  sandbox_worker.py         # host side of the candidate's JSON/stdio boundary
  sandbox_worker_main.py    # candidate-side worker; trusted timed batch loop
  feature.py                # accepted steps, attempt feedback and fixed-head feature PRs
  invariant.py  backtest.py # domain laws over generated inputs; replay of recorded episodes
  slo.py                    # an optional latency budget from the spec (not correctness)
  clock.py                  # event time behind a port, so history can be replayed
  sensor_input.py           # untrusted observations → readings, absences or refusals
  sensor_trace.py           # append-only sensor captures and revisions
  replay.py                 # what a model may see at a moment of a replay (known_at)
  fixture_builder.py        # held-out backtest episodes built from a sensor trace
  contract_author.py        # the one (human-approved) write path into specs/
  policy.py                 # change-authorization tiers (FORBIDDEN/STRICT/SOFT)
  serving.py  serve_cloud.py  # a target behind Ray Serve; the Cloud port on real Serve
  canary.py  metrics.py     # the online half of the contract; percentile helpers
  loadgen.py                # concurrent synthetic traffic against a served target
  config.py                 # one schema for every knob → config.yml
  settings.py               # secrets: local YAML ↔ AWS Secrets Manager
  cost.py                   # LLM cost accounting for the CEO spend brakes
  episodic.py               # provenance/episodic store (jsonl | duckdb | none)
  admin.py                  # operator pause/resume/reset-breaker on the live CEO
  artifact_sync.py          # the run's dataset synced to S3 by the loop itself
  atomic.py                 # replace a file all at once (brake state, config.yml)
  version.py                # which code is running, for provenance
  run_lock.py               # one main.py loop per box
  frontend.py  operator.py  # operator console (Panel) + its tier-gated write path
  paths.py                  # single source of truth for filesystem paths
specs/<target>/             # each target's exam: oracle, tests, laws, fixtures (FORBIDDEN)
runtime/                    # runtime-mutable state (kept apart from the engine)
  target.py  sort_target.py # the SOFT-tier targets (naive baselines, committed)
  candidates/               # the stub proposer's hand-written variants
  contract_staging/         # drafted exams awaiting human approval
  sensor_traces/            # captured observations; staging, never the exam
  episodic.jsonl            # episodic store (gitignored; or episodic.duckdb)
config.yml                  # every knob, with its default, env var and CLI flag
tests/                      # fast, Serve integration and docs-marked tests
infra/aws/                  # OpenTofu for the one-node AWS run (docs/AWS_RUN.md)
scripts/                    # check_connections.py (read-only preflight); the AWS run's
                            #   aws_bootstrap.sh, aws_secret.py, rehearse_aws_run.sh
                            # capture_sensor.py (fetch EIA into the local trace)
                            # build_fixture.py (stage held-out episodes from a trace)
                            # ci_gate.py (which CI jobs a change needs)
main.py                     # entry point
secrets.example.yml         # secrets template (copy to secrets.local.yml)
```

The **`sis/` (engine) vs `runtime/` (data) split** is deliberate: the system
rewrites code at runtime, so the mutable target/candidates/log live physically
apart from the engine the loop must not touch.

---

## Connecting to the real world

The default adapters are in-memory — no credentials needed. To talk to real
Confluence/Jira/GitHub/AWS, provide secrets and flip one switch.

### Secrets

The **same code path** works locally and in the cloud; the source is chosen by
`SIS_ENV`:

| `SIS_ENV` | Secret source | Use |
|-----------|---------------|-----|
| `local` (default) | `secrets.local.yml` (gitignored), falling back to `SIS_*` env vars | your machine |
| `aws` | AWS Secrets Manager (`SIS_AWS_SECRET_ID`, region `SIS_AWS_REGION`) | cloud deploy |

- `secrets.local.yml` is **gitignored** — never commit it. `secrets.example.yml`
  is the committed template.
- Tokens are masked in `repr()` (only the last 4 chars shown), so they don't
  leak into logs or tracebacks.
- `boto3`/`pyyaml`/`requests` are optional (the `real` Poetry group) and imported
  lazily — the in-memory path needs none of them.

### Bring it online

```bash
cp secrets.example.yml secrets.local.yml     # fill in tokens, ids, owner/repo
poetry install --with real                   # installs requests/boto3/pyyaml
export SIS_ADAPTERS=real                      # default is "memory"
poetry run python scripts/check_connections.py --deep   # read-only preflight (+ Jira workflow)
poetry run python main.py                     # run a real cycle
```

`check_connections.py` does **read-only** calls per configured service
(every Confluence space the org writes to, the Jira project, GET the repo, STS caller
identity). It prints `✓ / ✗ / –` per service and exits non-zero if any
configured integration fails. It never writes, commits, or deploys, and never
prints credentials. Add `--deep` to also list the Jira project's workflow
statuses and confirm the ones the org transitions to (`In Progress`,
`Ready for Review`, `TBD`, `Done`, `To Do`) exist — catching a
`JiraWorkTracker.transition` name mismatch before the first real cycle.

### Deploying to AWS

Store the same keys as a JSON secret in AWS Secrets Manager, give the
task/instance an IAM role with `secretsmanager:GetSecretValue`, then:

```bash
export SIS_ENV=aws SIS_ADAPTERS=real SIS_AWS_SECRET_ID=sis/prod/credentials
```

No code changes — only environment.

The supervised AWS run (OMNI-29) uses one EC2 node with zero ingress ports
(SSM only), provisioned by OpenTofu under `infra/aws/`. Seven runs are recorded
in [`docs/AWS_RUN.md`](docs/AWS_RUN.md), including staged optimization and
Class-2 construction. An unattended, monitor-driven server is still planned.

### Reading the modelled world

`SimSensor` replays supplied readings; `EIASensor` reads a locally captured EIA
trace. Capture and read are separate: choosing `sensor.backend=real` does not
download data or attach a domain model to the loop. Install the `real` group and
run `poetry run python scripts/capture_sensor.py` to populate the trace.
See [`docs/SENSOR_RUNBOOK.md`](docs/SENSOR_RUNBOOK.md) for the declared series,
capture reports, replay semantics and historical timestamp limitations.

---

## Guardrails (enforced in code)

- **Untrusted code never runs in the main process** — candidate execution uses
  `SIS_SANDBOX=subprocess` (soft guard) or `docker` (kernel-enforced no-egress /
  no-credential isolation); trusted harnesses judge through isolated workers.
- **Never commit to `main`**; the agent works on feature branches only.
- Destructive/irreversible actions — merging a PR, archiving a page, deleting an
  issue — raise `RequiresHumanApproval` instead of executing.
- **Promotion follows an *observed* merge, never a decision.** The loop polls the
  pending PR while a canary holds the green slot and promotes only once it reads
  back as merged. It cannot manufacture that: `merge_pr()` raises in every
  adapter, and the `Workspace` surface roles talk through exposes no merge at
  all. The agent applies a human's decision; it never makes one.
- **Three CEO brakes:** a hard total LLM spend cap, a circuit breaker after N
  regressed cycles, and a cost-per-accepted-improvement SLO (so low-value spend
  trips the breaker, not just regressions). Real Claude token usage is priced
  per cycle and fed into the gate. A trip files a `CIRCUIT BREAKER OPEN` bug via
  DevOps and pages through the Notifier (SNS with real adapters).
- Counted rejected/error cycles file a bug in the work tracker
  (`DevOps.file_bug`); neutral outcomes do not.
- `main.py` holds an exclusive lock on `runtime/loop.lock`; a second start
  exits 3 and names the holder. Operator pause/resume/reset operations use
  `python -m sis.admin` with a written reason; resetting a breaker preserves spend.
- Secrets in gitignored YAML locally / Secrets Manager in cloud — never committed.

---

## Development

```bash
poetry run pytest                  # fast suite, parallel (default: -m "not serve" -n auto)
poetry run pytest -m serve -n 0    # Ray Serve integration tests (serial)
poetry run pytest -m docs -n 0     # documentation checks
poetry run mypy --strict sis/ main.py scripts/
poetry run ruff check .
```

The default run is the fast inner loop and **not a full verification on its
own**: the Serve half stands up a real cluster and takes minutes. Run it before
trusting a change that touches serving, or let CI run both halves, which it
always does. `mypy --strict` and `pytest` are both CI gates *and* gauntlet
gates. Every bug found becomes a permanent regression test (the suite is the
moat).

Open bugs and limitations are tracked with stable IDs in
[`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md) — check it before starting work.

---

## Roadmap

| # | Milestone | Status |
|---|-----------|--------|
| 0 | Bootstrap skeleton (supervisor+worker, gauntlet, episodic log) | ✅ |
| 1 | Stub proposer end-to-end | ✅ |
| – | Actor org + ports/adapters + SelfModel | ✅ |
| – | Real Confluence/Jira/GitHub adapters + secrets layer | ✅ |
| 2 | Real Claude proposer (`SIS_PROPOSER=claude`) | ✅ |
| 3 | Harden the gauntlet: sandboxed run (gate 5) | ✅ subprocess + docker |
| – | Cost cap + cost-per-accepted SLO (CEO brakes) | ✅ |
| – | Change-authorization policy (FORBIDDEN/STRICT/SOFT) | ✅ |
| – | Episodic/provenance store, pluggable (jsonl + duckdb) | ✅ |
| – | Kernel-enforced docker sandbox + per-gate timeout | ✅ |
| – | Adversarial regression corpus (wrong/gaming/hanging diffs rejected) | ✅ |
| – | `--deep` Jira workflow checker | ✅ |
| – | Live-tenant adapter validation (real scratch cycles, incl. re-runs) | ✅ |
| – | Cycles build on the merged target (`live_target_source`) | ✅ |
| – | Post-review hardening: gauntlet baseline (H1), benign no-op outcome, docker required for a real proposer (M1), branch base (M4), pricing (L3) | ✅ |
| – | First real-life run: real Claude + real adapters + docker sandbox | ✅ (2026-07-28) |
| – | Env-configurable CEO spend brakes (`SIS_BUDGET_USD`, …) (M5) | ✅ |
| – | Shared Ray namespace + persisted CEO brake/spend state (M2/L9) | ✅ |
| – | Provider-agnostic LLM interface (`sis/llm.py`; not locked to one vendor) | ✅ |
| – | Long-running server loop (`main.py --loop`) — the sustained-breach trigger exists (`loop.breach_trigger`, OMNI-10) but `--loop` still drives the demo `repeat()` intake | ✅ partial |
| – | Served target + load generator + `ServeCloud` (weighted split, shadow dispatch, live windows) | ✅ (2026-08-08) |
| – | Observe the human merge: `promote()` has a caller, green is released, the loop resumes | ✅ (2026-08-09) |
| – | `DevOps.canary()` judges a candidate against real traffic (`canary_backend="serve"`, OMNI-14); a live rejection changes the cycle's outcome | ✅ (2026-08-09) |
| 4 | Target/oracle contract (L5) + Class-2 feature verification (`FeatureContract`, invariant + backtest gates, `Clock` port, contract-author) | ✅ (L5 2026-08-06; Class 2 2026-08-11) |
| – | One config schema: `config.yml` + env var + CLI flag (OMNI-27) | ✅ (2026-08-16) |
| – | Operator console: state, brakes, episodic history, tier-gated config edits (OMNI-28) | ✅ (2026-08-28) |
| – | Supervised AWS runs: one node, real proposer, adapters and sandbox (OMNI-29) | ✅ seven runs documented |
| – | Sensor port, sanitising boundary and EIA capture/replay (OMNI-31/32/33) | ✅ on `develop` (2026-10-13), unreleased |
| – | Replay on `known_at` + fixture builder for the first recorded fixture (OMNI-35) | in progress: tooling on `develop`; the real fixture waits for the box's first capture and a human promotion |
| 5 | Language-agnostic `ToolchainAdapter` (build/verify non-Python targets, e.g. Java) (OMNI-20) | parked |
| 6 | Model an external slice of the real world — omnitrack; first domain the California gasoline market (Phase A: OMNI-30), second Iowa's spirits supply chain (OMNI-39) | next |

The live plan is the Jira board ([`OMNI`](https://olafzumpe.atlassian.net/browse/OMNI));
what comes after Class 2 is in [`docs/OMNITRACK_VISION.md`](docs/OMNITRACK_VISION.md).

---

## License

Apache 2.0. Never copy Akka source code; the actor *concepts* (Hewitt 1973,
Erlang/OTP supervision) are reimplemented cleanly.
