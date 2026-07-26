# Design Brief — Self-Improving Server (Python + Ray)

This is the detailed companion to `CLAUDE.md`. The control-loop diagram lives in
`ray_self_improving_control_loop.svg` in this folder.

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
- **SWE / QA / DevOps** (and PM's **Designer** helpers) -> Ray actors the supervisors
  spawn and own.
- **Supervision** -> Ray's `max_restarts` / `max_task_retries`: a crashing
  implementation actor restarts or surfaces as a failure without taking down its
  parent — the QA->rollback spine without hand-rolling one.

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

The same adapter mechanism later connects **domain** actors to the modeled world's
APIs (traffic sensors, building systems, …).

### 3.2 Self-Model & hardware-model (digital twin)
A dedicated named, detached actor (the **Registry / SelfModel**) keeps a live,
queryable model of the system itself and its substrate — the *first piece of the
world the server models*, using the same machinery it will later point at traffic /
a building / a factory.
- Software/self: actor registry (role, state, parent/child, mailbox depth, restarts,
  placement), blue/green deploy slots & live version, active cycles, provenance graph.
- Hardware/runtime: Ray cluster nodes, CPU/RAM/GPU/disk/network headroom, AWS
  instance types/region/autoscaling, OS & runtime versions.
- Why load-bearing: error collection (map an exception to actor+node+version),
  blue/green handling (which slot is live; capacity for a green env), building
  additional roles (reason over its own org chart to add e.g. a SecurityReviewer),
  and starting/monitoring actors (source of truth for supervision & placement).

## 4. Self-improvement control loop

Monitor → budget/goal gate → propose → validation gauntlet → Ray Serve canary →
promote or rollback → log outcome → circuit breaker. See the SVG for the picture.

- **Monitor & detect** — Ray worker actors emit metrics. Trigger a cycle only on a
  *sustained* SLO breach over a rolling window, never a single spike.
- **Budget & goal gate** — supervisor actors decide whether an attempt is worth the
  LLM cost. Hard cap, not a soft "track."
- **Propose** — an LLM actor builds a prompt from system state and generates Python
  on a feature branch.
- **Validation gauntlet** — see §5.
- **Ray Serve canary** — deploy the new version alongside the old, shift weighted
  traffic, compare against baseline. For an internal (non-served) actor, the
  equivalent is: spin up the new actor version, shadow-run real traffic, then
  atomically swap the named-actor handle.
- **Promote / rollback** — promote shifts 100% of traffic; rollback kills the new
  version. Both outcomes (including failed diffs) are logged to episodic memory.
- **Circuit breaker** — N consecutive failed/regressed attempts freezes the loop and
  pages a human.

## 5. Validation gauntlet (the compile-gate replacement)

Python runs anything you hand it, so the gauntlet has to be strict. Order matters —
cheapest, safest checks first:

1. **`ast.parse`** — instant syntax check. The nearest thing to "does it compile."
2. **`mypy --strict`** (or pyright) — the only static type safety net you have.
   Generated code MUST be fully type-annotated; require this in the prompt template.
3. **`pytest`** — the existing suite must pass. Every bug ever found becomes a
   permanent regression test (the suite is the moat).
4. **Benchmark** — must beat the current baseline by a defined margin, or it's a
   regression.
5. **Sandboxed run** — execute the candidate in an isolated Ray task with its own
   `runtime_env`, ideally inside a locked-down container: **no network egress, no
   credential mounts.** Steps 3 and 4 run *inside* this sandbox so an infinite loop
   or malicious diff is contained.
6. **Human PR** — mandatory manual review before anything merges.

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
- **Secrets:** `.env` in `.gitignore` for local dev only; AWS Secrets Manager / SSM
  Parameter Store with IAM roles in the cloud. Never commit tokens.
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
shape:

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
