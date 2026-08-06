# Self-Improving Server (Python + Ray)

A server that models a slice of the real world via a Ray actor hierarchy and can
extend itself by generating, validating, and safely deploying its own code from
high-level specs.

**Bootstrap goal first:** before it models anything external, the server must be
self-improving on a trivial *internal* target — receive a spec, generate code,
validate it through a hard gauntlet, deploy it on a canary, and roll back on
regression. That loop works today, driven by an actor org (CEO/PM/CTO/SWE/QA/
DevOps/Designer) that coordinates through durable artifacts (Confluence/Jira/
GitHub), with a SelfModel digital twin tracking the running system.

> **New to the code?** [`docs/CODE_TOUR.md`](docs/CODE_TOUR.md) is a reading
> guide written for people learning Python + Ray. Want to contribute? See
> [`CONTRIBUTING.md`](CONTRIBUTING.md). Licensed [Apache 2.0](LICENSE).
>
> Authoritative specs: [`DESIGN.md`](DESIGN.md) (detailed brief),
> [`ACTORS.md`](ACTORS.md) (role + subsystem spec), [`CLAUDE.md`](CLAUDE.md)
> (working rules). Diagrams: `ray_self_improving_control_loop.svg`,
> `actor_org_interaction_map.svg`.

---

## Quick start

```bash
poetry install
poetry run python main.py          # run one full org cycle (in-memory, no creds)
poetry run python main.py --loop   # run continuously as a server (Ctrl-C to stop)
poetry run pytest                  # run the test suite
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

Everything is controlled by environment variables; defaults need no setup,
credentials, or extra installs. Set these to opt into more.

| Variable | Default | Values / effect |
|----------|---------|-----------------|
| `SIS_PROPOSER` | `stub` | `stub` = offline hand-written candidate. Any other value = a real LLM via `sis.llm` (needs `poetry install --with llm` + a provider key). |
| `SIS_LLM_PROVIDER` | `anthropic` | Which LLM backend the proposer uses. Adapters live in `sis/llm.py`; add a vendor with a small adapter + registry line. |
| `SIS_LLM_MODEL` | provider default | Override the model within a provider (e.g. `claude-sonnet-5`). |
| `SIS_SANDBOX` | `subprocess` | Gauntlet isolation. `subprocess` = scrubbed env + in-process egress block. `docker` = kernel-enforced (`--network none`, no creds; needs the image, see below). **Required** when `SIS_PROPOSER` is not `stub` — untrusted LLM code can read host files in `subprocess` mode. |
| `SIS_ALLOW_UNSANDBOXED_LLM` | `0` | `1` lets a real proposer run in the soft `subprocess` sandbox (loud warning). Unsafe — the candidate can read local files; never use it with real credentials. |
| `SIS_SANDBOX_IMAGE` | `sis-gauntlet:latest` | Image used when `SIS_SANDBOX=docker`. |
| `SIS_SANDBOX_MEMORY` / `SIS_SANDBOX_CPUS` | `1g` / `2` | Per-container resource caps in `docker` mode. |
| `SIS_GAUNTLET_TIMEOUT` | `120` | Per-gate wall-clock cap (seconds) — kills infinite loops (and, in docker mode, the container). |
| `SIS_TARGET_PATHS` | `runtime/target.py` | Comma-separated SOFT-tier paths the loop may optimise. Guardrail code can never be added. |
| `SIS_ALLOW_STRICT_CHANGES` | `0` | `1` lets the loop propose changes to non-guardrail engine code — still requires approval + justification + checks. |
| `SIS_BUDGET_USD` | `5.0` | CEO hard spend cap (USD). Set a **tiny** value for a first real run so the brakes trip early. Also: `SIS_BREAKER_THRESHOLD` (`3`), `SIS_MAX_COST_PER_ACCEPTED_USD` (`2.0`), `SIS_SLO_MIN_SPEND_USD` (`0.50`). A bad value fails loudly. |
| `SIS_EPISODIC_STORE` | `jsonl` | Provenance/episodic backend: `jsonl` (zero-dep), `duckdb` (SQL analytics; `poetry install --with analytics`), or `none`. |
| `SIS_ADAPTERS` | `memory` | `memory` = in-memory artifact bus (no creds). `real` = Confluence/Jira/GitHub/AWS (needs `--with real` + secrets). |
| `SIS_LOOP_INTERVAL` / `SIS_LOOP_MAX_CYCLES` | `30` / — | `main.py --loop` only: seconds between ticks, and an optional cycle bound (unset = run until Ctrl-C / SIGTERM). |
| `SIS_HTTP_TIMEOUT` | `30` | Per-request timeout (seconds) for every real-adapter call, so a wedged tenant API can't hang a cycle. A bad value fails loudly. |
| `SIS_ENV` | `local` | Secret source. `local` = `secrets.local.yml` → `SIS_*` env vars. `aws` = AWS Secrets Manager. |
| `SIS_SECRETS_FILE` | `secrets.local.yml` | Override the local secrets file path. |
| `SIS_AWS_SECRET_ID` / `SIS_AWS_REGION` | — | Secrets Manager secret id + region (when `SIS_ENV=aws`). |
| `ANTHROPIC_API_KEY` | — | Required for the `anthropic` provider (the default when `SIS_PROPOSER` isn't `stub`). Other providers read their own key. |

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

1. **CEO** gates on budget; holds circuit-breaker authority.
2. A proposal is dropped into the Confluence *proposal* space (intake).
3. **PM** refines it into a spec page; **Designer** adds an outline.
4. **CTO** turns the spec into a Jira epic + stories.
5. **SWE** implements on a feature branch — reusing the existing
   `proposer` + `gauntlet` — and opens a PR carrying the validated change.
6. **QA** re-runs the deterministic gauntlet and verifies the story.
7. **DevOps** canary-deploys to the **green** slot.
8. Promotion to live = the **human PR merge** — intentionally *not* done by the
   agent. The cycle ends at `verified_awaiting_human_merge`.

Every handoff is an artifact state change; every step is recorded in the
**SelfModel** provenance graph.

**On failure** (gauntlet rollback at step 5, or QA rejection at step 6), the
cycle doesn't just log and stop: **DevOps files a bug** in the work tracker
carrying the story + reason, so a rejected diff is a durable artifact, not just
an episodic-log line. Three consecutive failures trip the circuit breaker,
which files a second, distinct `CIRCUIT BREAKER OPEN` bug — the "page a human"
the design calls for, made concrete.

### The validation gauntlet (`sis/gauntlet.py`)

Python has no compiler, so the gauntlet gates hard — cheapest checks first, all
in a subprocess so a bad diff can't hang the main process:

| # | Gate | Status |
|---|------|--------|
| 1 | `ast.parse` (syntax) | ✅ |
| 2 | `mypy --strict` (types) | ✅ |
| 3 | `pytest` (regression suite) | ✅ |
| 4 | benchmark vs baseline | ✅ |
| 5 | sandboxed run (no egress / no creds) | ✅ subprocess (soft) · 🐳 docker (kernel-enforced) |
| 6 | human PR review | ✅ (enforced — agent never merges to `main`) |

Gate 5 has two modes via `SIS_SANDBOX`: `subprocess` (default — scrubbed env +
in-process network block) and `docker` (`--network none --cap-drop ALL
--read-only`, only the temp dir mounted; build the image once with
`docker build -t sis-gauntlet:latest -f Dockerfile.gauntlet .`). Every gate
also has a wall-clock timeout (`SIS_GAUNTLET_TIMEOUT`, default 120s) so an
infinite loop in generated code is killed, not left to hang.

**Invariant:** the gauntlet is the *only* place candidate or target code runs —
including the baseline measurement (`measure_baseline()`) and the DevOps canary.
No role ever executes a module in the main process where credentials live.

### Provenance / episodic store (`sis/episodic.py`)

Every cycle records an event — spec → diff → gauntlet verdict → outcome,
**including each rejected diff and the gate that caught it** — so the log is the
dataset the system learns from, not an afterthought. It sits behind an
`EpisodicStore` port selected by `SIS_EPISODIC_STORE`:

- `jsonl` (default) — append-only, zero-dependency, durable.
- `duckdb` — embedded SQL analytics over the events (`poetry install --with
  analytics`); `summary()` rollups (reject-rate by gate, cost-per-accepted) plus
  an `sql()` escape hatch.
- `none` — disabled.

Postgres + pgvector can be added as another backend later (multi-node cluster /
embedding retrieval) without changing the loop.

---

## Project layout

```
sis/                     # the engine package (immutable code)
  ports.py               # capability interfaces (Protocols) + artifact types
  adapters.py            # default in-memory adapters (the artifact bus)
  adapters_real.py       # real Confluence/Jira/GitHub/AWS adapters
  settings.py            # secrets/config: local YAML ↔ AWS Secrets Manager
  workspace.py           # shared artifact-bus Ray actor (picks adapters by config)
  self_model.py          # SelfModel/Registry digital-twin Ray actor
  roles.py               # CEO/PM/CTO/SWE/QA/DevOps/Designer actors
  org.py                 # bootstrap + run one intake→deploy cycle
  paths.py               # single source of truth for filesystem paths
  proposer.py            # propose(source, baseline) → candidate (stub → Claude)
  gauntlet.py            # validate(code, baseline) → Result
  policy.py              # change-authorization tiers (FORBIDDEN/STRICT/SOFT)
  cost.py                # LLM cost accounting for the CEO spend brakes
  episodic.py            # provenance/episodic store (jsonl | duckdb | none)
runtime/                 # runtime-mutable state (kept apart from the engine)
  target.py              # the live target (naive baseline, committed)
  candidates/            # proposer's hand-written variant
  episodic.jsonl         # episodic store (gitignored; or episodic.duckdb)
tests/                   # pytest suite (162 tests)
scripts/check_connections.py   # read-only credential/connectivity preflight
main.py                  # entry point
secrets.example.yml      # secrets template (copy to secrets.local.yml)
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

`check_connections.py` does one **read-only** call per configured service
(list the Confluence space, get the Jira project, GET the repo, STS caller
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

---

## Guardrails (enforced in code)

- **Untrusted code never runs in the main process** — every gauntlet gate runs
  in a sandbox (`SIS_SANDBOX=subprocess` soft guard, or `docker` for
  kernel-enforced no-egress / no-credential isolation).
- **Never commit to `main`**; the agent works on feature branches only.
- Destructive/irreversible actions — merging a PR, archiving a page, deleting an
  issue, promoting a canary to live — raise `RequiresHumanApproval` instead of
  executing.
- **Three CEO brakes:** a hard total LLM spend cap, a circuit breaker after N
  regressed cycles, and a cost-per-accepted-improvement SLO (so low-value spend
  trips the breaker, not just regressions). Real Claude token usage is priced
  per cycle and fed into the gate. A trip files a `CIRCUIT BREAKER OPEN` bug via
  DevOps — an artifact a human sees, not just a telemetry flag.
- Every rolled-back or QA-rejected cycle files a bug in the work tracker
  (`DevOps.file_bug`) — failures are durable artifacts, not just log lines.
- Secrets in gitignored YAML locally / Secrets Manager in cloud — never committed.

---

## Development

```bash
poetry run pytest            # 162 tests (adapters, settings, gauntlet, org cycle + failures, brakes, adversarial corpus, …)
poetry run mypy --strict sis/ main.py scripts/
poetry run ruff check .
```

`mypy --strict` and `pytest` are both CI gates *and* gauntlet gates. Every bug
found becomes a permanent regression test (the suite is the moat).

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
| – | Long-running server loop (`main.py --loop`; monitor-trigger still simulated) | ✅ partial |
| 4 | Ray Serve weighted canary + atomic actor swap | next |
| 5 | Target/oracle contract (L5) + Class-2 feature verification | planned |
| 6 | Language-agnostic `ToolchainAdapter` (build/verify non-Python targets, e.g. Java) | wanted |
| 7 | Model an external slice of the real world | vision |

See the **Milestone Roadmap** Confluence page for detail.

---

## License

Apache 2.0. Never copy Akka source code; the actor *concepts* (Hewitt 1973,
Erlang/OTP supervision) are reimplemented cleanly.
