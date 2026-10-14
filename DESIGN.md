# Design Brief — Self-Improving Server (Python + Ray)

This is the detailed companion to `CLAUDE.md`. The control-loop diagram lives in
`ray_self_improving_control_loop.svg` in this folder.

**Implementation boundary:** the engine builds and optimizes designated Python
targets under human-authored contracts. Supervised AWS runs are complete;
external-domain simulation and an unattended monitor-driven server remain
planned. Sections 3–5 distinguish the implemented runtime from its intended
extensions; §8 is explicitly a historical bootstrap snapshot.

## 1. Vision

A server that monitors and models a slice of the real world (traffic, appliances,
a building, a factory, a delivery process, …) using an actor hierarchy and
self-implemented interfaces. It generates a model from high-level specs, finds and
calls APIs to read the state of each modeled part (one actor per part), updates its
state, makes decisions, and spawns child actors for specific tasks.

**Bootstrap precondition:** before it models anything external, the server itself
must be self-improving — able to receive a spec, generate code, validate it safely,
deploy it, and roll back on regression. Get that loop working on a trivial internal
target first. Everything else is built on top of it.

## 2. Stack decisions (and why)

- **Python 3.14, standard CPython — not free-threaded.** The original plan was the
  free-threaded build (`python3.14t`), but Ray ships `cp314` wheels and not `cp314t`,
  so a `python3.14t` env can't install Ray — the free-threaded build is off the table
  until those wheels exist. Managed with **Poetry** (`uv` works too). Keep this env
  fully separate from the unrelated work project that pins Python 3.11 + py4j for
  Spark — different virtualenv, no overlap.
- **Ray** for the actor system; **Ray Serve** for serving/canary rollouts.
  - Chosen over **Akka/Pekko** to avoid Akka's BSL licensing entirely and to stay
    in the Python AI ecosystem (every LLM SDK, MCP, etc. is Python-native). Pekko is
    Apache 2.0 but keeps us on the JVM with a Java↔Python bridge we don't need.
  - Chosen over **Zig** because Zig is still pre-1.0 with a churning async model —
    the worst possible substrate for a self-modifying, runtime-evolving system right
    now. Revisit a compiled language (Rust first) only for proven hot paths, via FFI,
    once they're stable.
- **License: Apache 2.0** — permissive plus an explicit patent grant with a
  retaliation clause, which addresses the patent concern directly. Never copy Akka
  source; reimplementing decades-old actor *concepts* (Hewitt 1973, Erlang/OTP
  supervision) is clean.

## 3. Actors, roles & external subsystems

**Full role specification (responsibilities, relations, real-world connections):
see @ACTORS.md.** Summary below.

Organizational metaphor -> Ray primitive:
- **CEO / CTO / PM** -> Ray **named, detached** actors; hold goals, budget, and
  state, survive tasks, looked up by name cluster-wide.
- **SWE / QA / DevOps / Designer** -> currently named, detached actors too;
  `org.bootstrap()` centrally creates all seven roles with `get_if_exists=True`.
  Supervisor-owned child workers are the intended organization, not today's
  actor ownership graph.
- **Supervision** -> Ray restart/retry controls are available, but the spawning
  hierarchy and dynamic worker supervision are planned. Candidate timeouts and
  failure attribution are implemented by the gauntlet and sandbox workers.

**Coordination principle:** actors coordinate through durable artifacts —
Confluence (designs), Jira (plans/status/bugs), Git (code/PRs) — not just in-memory
messages. Those artifacts are simultaneously the work queue, the inter-actor message
bus, the audit trail, and the system's long-term memory. Every handoff is an artifact
state change (e.g., SWE -> "Ready for Review"), which keeps LLM "negotiation" bounded
and yields provenance for free.

### 3.1 Required external subsystems (ports & adapters)
The *capability* is required; the *product* is replaceable. Each sits behind an
interface with a default adapter, chosen for existing MCP connectors and
industry-standard status, and load-bearing because they are the actors' shared
coordination + memory substrate:
- **Document Store** — designs/specs + the intake channel where non-technical users
  drop new designs to be refined and implemented. Default: **Confluence** (Atlassian
  / Rovo MCP).
- **Work Tracker** — epics/stories/spikes/bugs + status workflow. Default: **Jira**.
- **Version Control & Review** — branches, commits, PRs, CI. Default: **GitHub**
  (GitHub MCP / `gh`).
- **Cloud/Infra** — compute, deploy targets. Default: **AWS**.
- **Telemetry** — logs, metrics, traces.
- **Notifier** — pages on trips and operational failures; AWS SNS with real
  adapters (OMNI-62).
- **Sensor** — `SimSensor` and EIA capture/replay, passed explicitly to a
  consumer. Untrusted observations enter through `sensor_input.sanitise`.
  See [`docs/SENSOR_RUNBOOK.md`](docs/SENSOR_RUNBOOK.md).

The engine's real adapters use REST/SDK calls. MCP/`gh` integrations are
contributor tools; they are not the runtime transport. Cloud's recording
adapters and the opt-in `ServeCloud` traffic backend are separate from AWS
provisioning in `infra/aws/`.

The same adapter mechanism later connects **domain** actors to the modeled world's
APIs (traffic sensors, building systems, …).

### 3.2 Self-Model & hardware-model (digital twin)
A dedicated named, detached actor (the **Registry / SelfModel**) keeps a live,
queryable model of the system itself and its substrate — the *first piece of the
world the server models*, using the same machinery it will later point at traffic /
a building / a factory.
- Implemented software records: actor name/role/parent/state/restart counter,
  contracts and PR-contract links, deploy slots/live version/pending PR, and
  provenance. Lifecycle records are updated by the application, not automatic
  Ray monitoring. The CEO owns budget and brake state.
- Implemented substrate snapshot: Ray cluster resource totals, Python version
  and platform (`SelfModel.substrate`).
- Planned: mailbox depth, automatic lifecycle monitoring, actor placement,
  per-node headroom, AWS inventory and dynamic role creation based on capacity.

## 4. Self-improvement control loop

Monitor → budget/goal gate → propose → validation gauntlet → Ray Serve canary →
promote or rollback → log outcome → circuit breaker. See the SVG for the picture.

- **Monitor & detect** — a sustained-breach trigger exists, but `main.py --loop`
  currently uses repeated demo intake. Wiring observations to autonomous domain
  or SLO-driven work remains planned.
- **Budget & goal gate** — supervisor actors decide whether an attempt is worth the
  LLM cost. Hard cap, not a soft "track."
- **Propose** — SWE builds a contract-derived prompt with prior attempt feedback.
  Passing optimization steps accumulate on a feature branch before one fixed-head
  PR; Class 2 opens its PR at the first passing step. Rejections remain feedback.
- **Validation gauntlet** — see §5.
- **Canary** — the recording backend executes no candidate code. Opt-in Serve
  runs live traffic, but is refused with any non-stub proposer (no override,
  until OMNI-48) and with Class 2. It uses shadow dispatch in the org flow.
  Atomic named-actor swaps for internal stateful targets remain planned.
- **Promote / rollback** — a human merges or closes the PR; `observe_merge()`
  applies that decision by promoting or releasing green. Serve promotion
  redeploys blue; recording adapters update deploy records. The loop never merges.
- **Brakes and outcomes** — counted failures contribute to the breaker and trips
  page a human. Neutral no-op/no-gain/inconclusive outcomes record spend without
  incrementing it; convergence stops politely. Cycle exceptions are recorded and
  charged. See `config.yml` for controls and `docs/KNOWN_ISSUES.md` for limitations.

## 5. Validation gauntlet (the compile-gate replacement)

`Contract.gate_profile()` chooses the ordered gates; `validate()` dispatches
them through one entry point. Candidate Python must be fully typed.

- **Optimization:** syntax → no-op → strict types → interface → acceptance →
  invariants → backtests → differential correctness and benchmark.
- **Feature construction:** syntax → strict types → interface → acceptance →
  invariants → backtests → optional latency SLO. There is no reference benchmark.

Undeclared invariants/backtests and absent SLOs have nothing to check. The
acceptance suite is the contract's trusted tests, not the entire repository's
CI suite. See [`docs/CLASS2_CONTRACT.md`](docs/CLASS2_CONTRACT.md).

**Execution boundary:** acceptance, invariant and backtest harnesses run on the
host and call a JSON proxy to `SandboxWorker`. The candidate never runs in the
process judging those gates. Benchmark candidate and baseline have separate
workers; reference, timing and verdict stay on the host. The SLO gate remains
in-process inside its sandbox, with the limitation recorded as OMNI-86.

Docker isolates candidate execution with no network, no credentials, a read-only
exam mount, separate scratch space and the host's non-root uid. Subprocess mode
is a soft guard that leaves host files readable and cannot enforce CPU affinity;
a real proposer requires Docker unless an explicit unsafe override is set.
Each gate has a timeout. Failed gates are followed by a trusted sandbox probe
so harness failures are attributed correctly, and trusted exam files are checked
for tampering after every gate.

Benchmarks use paired total cost over unused inputs, with an interval deciding
accept/reject/inconclusive. Timed batches have no per-call Python harness work;
Docker confines each worker to a different single CPU and swaps them halfway.
See [`docs/CODE_TOUR.md`](docs/CODE_TOUR.md) for execution details.

The worker also provides a hot-slot primitive for future deployment. It is not
yet the Serve canary backend: an ordinary Serve replica still has control-plane
access, so a passing offline candidate from a real proposer cannot be served
there. Every finished feature ends in a human-reviewed PR before promotion.


## 6. Guardrails (MCP + repo)

- **Never run untrusted code in the main process** (see §5 sandbox).
- **GitHub:** agent works only on feature branches; enforce with branch protection +
  required status checks + CODEOWNERS so it *cannot* touch `main`. Use signed/verified
  commits so agent commits are distinguishable. Restrict the GitHub MCP toolsets
  (`repos`, `issues`, `pull_requests`, `actions`) to only what the current sprint needs.
- **Atlassian (Jira/Confluence via Rovo MCP):** dedicated service account, scoped
  write tokens, never admin. Destructive actions (deleting issues, archiving pages)
  require human approval. Update Jira status only after a successful commit/deploy.
  Documentation follows fixed Confluence templates, not free-form pages.
- **Secrets:** gitignored `secrets.local.yml` locally; AWS Secrets Manager with
  an instance IAM role in the cloud (`sis/settings.py`). Never commit tokens.
- **Configuration:** every knob is declared once in `sis/config.py` and rendered
  into a committed `config.yml` (CLI flag > env var > file > built-in default).
  Both are guardrail code: the sandbox mode, the spend brakes and the policy's
  own target list are all reachable from that one file, so the loop may not
  write it at any tier — including via naming it as an optimisation target,
  which guardrail precedence makes inert. Secrets never go in it; it is committed.
- **Cost:** hard LLM budget cap + cost/benefit gate per cycle + the circuit breaker.

## 7. Metrics / SLOs to optimize against

Per-actor and per-endpoint latency (p50/p95/**p99**), throughput (msgs/sec, req/sec),
mailbox/queue depth and backpressure, error rate (exceptions, actor restarts, dead
letters), resource pressure (heap, GC pause, CPU, thread-pool saturation), and the
loop's own economics (tokens & $ per cycle, cost-per-accepted-improvement, rollback
rate). Define each as an SLO with an error budget.

## 8. Bootstrap skeleton (the first task)

Goal: a small set of files, runnable locally with Ray, that exercise the whole loop
on a trivial internal target — something to run and read while relearning Python.

This is **done**; the single-file sketch grew into the `sis/` package. The realized
shape, as of the *original bootstrap milestone* (this section is a historical
snapshot, not current documentation — `validate()`'s signature, its gate list,
and the fixed `pytest`-only correctness check below are all superseded; see
`CLAUDE.md`'s Hard Rules for the current per-contract gate profile and
`docs/KNOWN_ISSUES.md` for open gaps in it):

- `pyproject.toml` — Poetry-managed; deps: `ray[serve]`, `mypy`, `pytest`,
  optionally `anthropic` (`--with llm`) and the real adapters (`--with real`).
- `runtime/target.py` — the thing being optimized: a small, deliberately slow pure
  function with a baseline benchmark (a naive `sum_of_divisors` with an obvious
  faster form).
- `sis/roles.py` — the seven Ray actors (CEO/CTO/PM/SWE/QA/DevOps/Designer); the SWE
  runs the target, QA + the gauntlet review, DevOps handles deploy/rollback. Leadership
  (CEO/CTO/PM) plus `Workspace` + `SelfModel` are **named, detached** actors holding
  the SLO, cost budget, and shared state.
- `sis/org.py` — `bootstrap()` + `run_cycle()`: wires the actors and drives one cycle
  (the "supervisor" of the original sketch).
- `sis/gauntlet.py` — `validate(code_str, baseline_latency, *, baseline_source=None)
  -> Result`: runs `ast.parse` → no-op check → `mypy --strict` → `pytest` →
  differential correctness + benchmark, all inside a sandbox (`SIS_SANDBOX=subprocess`
  default, or kernel-enforced `docker`). Returns pass/fail + metrics.
- `sis/proposer.py` — `propose(current_source, baseline_latency) -> code_str`. Both
  milestones landed: the default stub returns a hand-written optimized variant (zero
  API calls), and `SIS_PROPOSER=claude` swaps in a real Claude call that gets the
  current source + benchmark and returns a typed, optimized version.
- `sis/episodic.py` — the append-only episodic log of every attempt (spec → diff →
  gauntlet verdict → outcome) behind a port: `jsonl` default, optional `duckdb`, or
  `none`.
- `main.py` — wires it: detect a simulated SLO breach → `propose` → `gauntlet.validate`
  → if it passes and beats baseline, promote the change; else keep baseline and log →
  record outcome → enforce the circuit breaker.

Acceptance (met): `poetry run python main.py` starts Ray locally, runs one full cycle,
and prints whether the proposed change was promoted or rolled back, with the
before/after benchmark and a line appended to the episodic log.

## 9. Open decisions (revisit later)

- Container/sandbox tech for §5 (gVisor, Firecracker, plain Docker with locked-down
  network?).
- Where episodic memory graduates from JSONL to a real store (vector DB vs Postgres).
- Whether to study Akka's agentic platform / Ray Serve patterns before scaling up.
