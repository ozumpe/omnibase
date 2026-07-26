# Code Tour — a reading guide for omnibase

A walkthrough of the codebase aimed at someone comfortable programming but new
to **modern Python** and **Ray**. It explains *why* the code looks the way it
does, points out the language and framework features as they appear, and traces
one full cycle end to end. Read it top to bottom once; after that it's a map.

> New to the project? Read [`README.md`](../README.md) first for what the system
> *does*; this doc is about how the *code* is built.

---

## 1. The 10,000-foot view

omnibase is a control loop that improves a piece of code, gated hard so it can't
break things. An **actor org** (CEO/PM/CTO/SWE/QA/DevOps/Designer) drives the
loop by reading and writing **artifacts** (Confluence pages, Jira issues, GitHub
PRs), and a **SelfModel** actor keeps a live picture of the running system.

The single most important idea: **the LLM is never trusted — the deterministic
gauntlet is.** Everything else exists to feed that gate and to act safely on its
verdict.

Two layers, deliberately kept apart:

```
omnibase/                # repo root
├── sis/                 # THE ENGINE — immutable code. ("sis" = the omnibase engine)
└── runtime/             # MUTABLE STATE the engine reads/writes at run time
```

The engine never edits itself; it edits files under `runtime/`. That split is
the whole reason the project can "rewrite its own code" without the rewrite
touching the rewriter.

---

## 2. Ray in five minutes (the concepts this code uses)

[Ray](https://docs.ray.io) turns a plain Python class into a distributed
**actor**: an object that lives in its own process, holds state, and is reached
by sending it messages. omnibase uses a small, specific slice of Ray — here's
all of it, mapped to where you'll see it.

| Concept | What it means | Where in the code |
|---|---|---|
| `@ray.remote` on a class | Makes it an **actor** — each instance runs in its own worker process | `sis/roles.py`, `sis/self_model.py`, `sis/workspace.py` |
| `Actor.remote(...)` | Construct an actor instance (returns a **handle**, not the object) | `org.bootstrap()` |
| `handle.method.remote(args)` | Call a method **asynchronously** — returns an `ObjectRef` (a future), doesn't block | everywhere a role does work |
| `ray.get(ref)` | **Block** and fetch the actual return value from an `ObjectRef` | everywhere we need the result |
| named + `lifetime="detached"` | Register an actor under a cluster-wide **name** so any process can look it up, and keep it alive independent of who created it | `Workspace`, `SelfModel`, the leadership roles |
| `ray.get_actor(name)` | Look up an existing named actor by name | `get_workspace()`, `get_self_model()` |
| `ray.init(...)` | Start (or connect to) a local Ray cluster | `org.bootstrap()`, `main.py` |

**The mental model:** an actor handle is like a phone number. `.remote()` places
a call and immediately hands you a "I'll call you back" ticket (`ObjectRef`).
`ray.get()` waits on hold until the answer comes. You'll see the pattern

```python
result = ray.get(some_actor.some_method.remote(arg))
```

constantly — that's "call this actor's method and wait for the answer."

**Why named + detached for the leadership and shared state?** Because the role
actors run in *separate processes* and must all see the *same* artifacts. If
each held its own copy of the data, there'd be no shared bus. Instead, one
`Workspace` actor owns the data and everyone looks it up by name — see
`sis/workspace.py`. This is the key Ray design decision in the project.

> **Gotcha you'll hit when learning Ray here:** state lives *inside* an actor.
> Mutating a Python object you passed *into* an actor doesn't change the actor's
> copy — you have to call a method on the actor. That's why all artifact changes
> go through `Workspace` methods, never by poking a returned dataclass.

---

## 3. Modern Python features you'll see (and why)

This codebase is a tour of current Python on purpose — you said you're learning.

- **`from __future__ import annotations`** (top of most modules) — makes all type
  hints lazy strings, so you can annotate with types that aren't imported at
  runtime and avoid circular-import pain. Standard in new code.
- **Type hints everywhere + `mypy --strict`** — every function is annotated and
  checked. This isn't decoration: type-correctness is a *gauntlet gate*, so
  generated code must pass it too. Run `poetry run mypy --strict sis/`.
- **`typing.Protocol`** (`sis/ports.py`) — *structural* interfaces. A class
  satisfies a `Protocol` just by having the right methods; it doesn't subclass
  anything. This is how the in-memory adapters and the real Confluence/Jira/
  GitHub adapters are interchangeable — see §6.
- **`@dataclass`** (`sis/ports.py`, `sis/self_model.py`) — auto-generates
  `__init__`/`__repr__`/`__eq__` from typed fields. `field(repr=False)` +
  a custom `__repr__` in `sis/settings.py` is how secrets stay out of logs.
- **`enum.Enum`** (`IssueType`, `IssueStatus`) — closed sets of values instead of
  magic strings.
- **Context managers** (`with tempfile.TemporaryDirectory()`) — guaranteed
  cleanup; the gauntlet builds a throwaway sandbox dir and it's removed on exit.
- **`importlib.util`** (`sis/gauntlet.py`) — load a Python file *by path* at
  runtime. That's how the gauntlet executes a candidate it just wrote to disk.
- **`subprocess`** (`sis/gauntlet.py`) — run each gate in a child process so a
  bad candidate can't corrupt or hang the main process.
- **Lazy imports** (`import anthropic` *inside* a function) — optional
  dependencies (`anthropic`, `boto3`, `requests`, `yaml`) are imported only when
  actually used, so the default path needs none of them installed.

---

## 4. Where to start reading

Read in this order — each builds on the last:

1. **`runtime/target.py`** — the thing being optimised. A deliberately slow
   `sum_of_divisors`. Tiny; start here.
2. **`sis/gauntlet.py`** — `validate(code, baseline) -> Result`. The moat. Read
   this closely; it's the heart of the safety story (§5).
3. **`sis/proposer.py`** — `propose(...) -> code`. Stub by default; real Claude
   behind a flag. Small.
4. **`sis/ports.py` + `sis/adapters.py`** — the ports/adapters pattern (§6).
5. **`sis/workspace.py` + `sis/self_model.py`** — your first Ray actors: the
   shared, named substrate every role reads and writes through.
6. **`sis/roles.py`** — the seven role actors. Each is small and focused.
7. **`sis/org.py`** — wires it together and runs one cycle. The conductor.
8. **`sis/episodic.py`** — the provenance/episodic store (§7b): what every cycle
   tried and why it was accepted or rejected.
9. **`main.py`** — the entry point.

Also worth a look: **`sis/policy.py`** (§7a, what the loop may change) and
**`sis/cost.py`** (LLM pricing feeding the CEO's spend brakes).

The tests under `tests/` are also excellent documentation — each shows a piece
in isolation. `tests/test_adversarial.py` in particular shows *what the gauntlet
refuses*, which is the clearest statement of the safety contract.

---

## 5. The gauntlet, gate by gate (`sis/gauntlet.py`)

`validate()` runs cheapest-first; the first failure short-circuits.

1. **`ast.parse`** — does it even parse? (No code runs yet.)
2. **No-op check** — a candidate byte-identical to the baseline is rejected up
   front as `no_change` (episodic `reject_gate="noop"`). It's *not* a failure —
   nothing to improve — so it files no bug and doesn't count toward the breaker
   (`run_cycle` returns a benign `no_change`; the CEO's `record_neutral` still
   books spend). The baseline it compares against is the source the caller
   passes (`baseline_source` — the target as merged on the base branch), not the
   local file.
3. **`mypy --strict`** — fully type-checked. Generated code must be annotated.
4. **`pytest`** — the target's test suite must pass (fixed correctness cases).
5. **Differential correctness + benchmark** — the candidate is run against an
   *independent* reference on **random** inputs (catches code that special-cases
   the known test inputs but is wrong elsewhere — "benchmark gaming"), then timed
   against that same `baseline_source`; it must be ≥10% faster.

Two cross-cutting protections wrap every gate that runs candidate code:

- **Sandbox** (`SIS_SANDBOX`): `subprocess` (default — credential-scrubbed env +
  an injected `sitecustomize.py` that blocks network sockets) or `docker`
  (kernel-enforced `--network none`, no creds, only the temp dir mounted). A
  real proposer (`SIS_PROPOSER=claude`) writes untrusted code and **requires**
  `docker` — the loop refuses the soft sandbox otherwise (override
  `SIS_ALLOW_UNSANDBOXED_LLM=1`).
- **Timeout** (`SIS_GAUNTLET_TIMEOUT`) — an infinite loop is killed, not left to
  hang.

Everything the gauntlet needs is written into one temp dir (the candidate, a
copy of the baseline, the test, an injected `sitecustomize.py`), so the sandbox
is fully self-contained — nothing reaches the host.

**The invariant: the gauntlet is the *only* place candidate or target code
runs.** Not just `validate()` — the baseline measurement (`measure_baseline()`,
used by the SWE before proposing) and the DevOps canary reuse the sandbox or the
already-sandbox-measured latency. No role ever `exec`s a module in the main
actor process (where credentials live). If you add a code path that runs a
generated or target module, route it through the sandbox — this is the rule the
whole safety model rests on. (Two review passes were needed to make it true
everywhere: the candidate-execution sites *and* the baseline path.)

> Learning note: read `_run()` to see the subprocess/docker branch and the
> `TimeoutExpired` handling, `_docker_args()` for exactly how the container is
> locked down, and `measure_baseline()` for the sandboxed baseline.

---

## 6. Ports & adapters — swapping fake for real (`sis/ports.py`, `adapters.py`, `adapters_real.py`)

This is the pattern that lets the whole org run locally with zero credentials
*and* talk to real Confluence/Jira/GitHub later, with the roles unchanged.

- **`ports.py`** defines five `Protocol`s — `DocumentStore`, `WorkTracker`,
  `VersionControl`, `Cloud`, `Telemetry` — the *capabilities* the org needs.
- **`adapters.py`** implements them in memory (the default — an artifact bus
  that needs nothing external).
- **`adapters_real.py`** implements the same Protocols against real REST APIs
  (validated live at runbook Level 2). Two live-tenant behaviours worth knowing:
  `create_page` is idempotent across runs (Confluence rejects duplicate titles
  per space, so it updates the existing page in place) and drops a cross-space
  `parentId` rather than fail (Confluence can't represent it; provenance is in
  the SelfModel).
- **`workspace.py`** picks which set to use based on config (`SIS_ADAPTERS`).

Because a `Protocol` is satisfied structurally, the roles just call
`workspace.create_issue(...)` and neither know nor care whether that's an
in-memory dict or a live Jira call. Swapping is a config flag, not a rewrite.
The destructive operations (delete, merge, promote) raise `RequiresHumanApproval`
in *both* implementations — the guardrail lives in the port contract.

---

## 7. One cycle, end to end (`sis/org.py :: run_cycle`)

Follow the data through a single run. Each step is `ray.get(actor.method.remote(...))`:

```
CEO.approve_budget(estimate)         # gate: is this worth the spend?
  └─ Workspace.create_page(proposal) # intake: a "user" drops a request
PM.refine_proposal(proposal)         # → a spec page (Confluence)
Designer.outline(spec)               # → a design outline
CTO.plan(spec)                       # → a Jira epic + stories
SWE.implement(story)                 # → propose() + gauntlet.validate()
  │                                  #   source = live_target_source() (the target
  │                                  #   as merged on the base branch), falling
  │                                  #   back to the local runtime/target.py —
  │                                  #   so cycles build on merged improvements
  │                                  #   on pass: branch + commit + PR
  └─ (records cost from the proposer)
QA.review(story, pr)                 # → re-runs the gauntlet, verifies
DevOps.canary(pr)                    # → green-slot deploy (never promotes)
PM.accept(spec) ; CEO.report_outcome(success, cost)   # drives the brakes
```

Two things to notice:

- **It stops at the human merge.** The validated change rides in a PR and a green
  canary; the agent never merges to `main` or promotes to live. That's gauntlet
  step 6 (human review) enforced structurally.
- **Provenance is recorded at every step** in `SelfModel` (spec → epic → story →
  branch → pr → canary → outcome). The returned dict includes the full graph.
- **Failure is wired to an artifact, not just a log line.** If `SWE.implement`
  fails the gauntlet (wrong/slower/untyped), or QA rejects, `run_cycle` calls
  `DevOps.file_bug` with the story + reason. Three consecutive failures trip
  `CEO.report_outcome`'s circuit breaker, which files a second, distinct
  `CIRCUIT BREAKER OPEN` bug — the "page a human" from `ACTORS.md` made concrete
  rather than left as dead code. (A `no_change` no-op is *not* a failure — no
  bug, no breaker increment.)
  `org.bootstrap()` also has the CEO call `set_charter` once, so provenance
  roots at a goal instead of starting cold at the first spec.

`CEO` holds three brakes (`sis/roles.py`): a hard spend cap, a consecutive-failure
breaker, and a cost-per-accepted-improvement SLO. The brake decision is a *pure
function* (`evaluate_brakes`) so it's unit-testable without Ray — a useful pattern
to copy: keep the decision logic pure, keep the I/O in the actor.

---

## 7a. What the loop may change — the policy (`sis/policy.py`)

The loop rewrites code, so *which* code it may touch is a safety question, not a
detail. `sis/policy.py` answers it with three tiers, checked before any change
is proposed or written:

| Tier | Paths | Loop may modify? | Bar |
|---|---|---|---|
| **FORBIDDEN** | guardrail/safety code | **Never — no override** | — |
| **STRICT** | all other engine code | Off-limits by default | when enabled (`SIS_ALLOW_STRICT_CHANGES=1`): pass checks **+ human-approved + justified** (exception or human request) |
| **SOFT** | the designated target(s) | Yes | pass checks; review via QA + human merge |

The **FORBIDDEN** set (`GUARDRAIL_PATHS`) includes the gauntlet, the spend
brakes, secret handling, the adapter guardrails, the sandbox image — **and
`policy.py` itself**, so the loop cannot loosen its own rules. There is no
approval that unlocks FORBIDDEN; that's the load-bearing invariant (a rewriter
that can edit its own validator can validate away its constraints).

Two enforcement points:

- `SWE.implement` (`sis/roles.py`) calls `authorize_change(TARGET_PATH, …)` after
  the gauntlet passes and before opening a PR — a mis-pointed target rolls the
  cycle back with `Policy blocked: …`.
- `GitHubVersionControl._put_file` (`sis/adapters_real.py`) refuses to write any
  FORBIDDEN path with `RequiresHumanApproval`, regardless of caller — a hard
  belt-and-suspenders stop.

**Widening scope** is a deliberate, reviewed change: set `SIS_TARGET_PATHS`
(comma-separated, repo-relative) to add SOFT targets. Guardrail paths always
win over that list (`classify` checks `GUARDRAIL_PATHS` first), so you can never
accidentally make safety code writable by listing it as a target.

> Pattern to copy: like `evaluate_brakes`, the whole policy is **pure functions
> over data** (`classify`, `authorize_change`) — no Ray, no I/O — so it's
> exhaustively unit-tested in `tests/test_policy.py`.

## 7b. The episodic store — what it learns from (`sis/episodic.py`)

The single most valuable output isn't the optimised code; it's the record of
*what was proposed, why, and what happened* — including **every rejected diff and
the gate that caught it**. That's the dataset a self-improving system learns
from, so it's a first-class, queryable output rather than a log file.

`org.run_cycle` writes one `EpisodicEvent` per outcome (guarded — episodic
logging can never break a cycle). The store is a **port** with swappable backends
chosen by `SIS_EPISODIC_STORE`:

- `jsonl` (default) — append-only, zero-dependency, durable.
- `duckdb` — embedded SQL analytics (`summary()` rollups + an `sql()` escape
  hatch); optional `poetry install --with analytics`.
- `none` — disabled.

The schema (`EpisodicEvent`) is the point — `outcome`, `reject_gate`,
`candidate_sha`, `cost_usd`, before/after latency. A shared pure `summarize()`
makes rollups identical across backends, and the DuckDB column schema is
*asserted* against the dataclass so it can't drift. Postgres + pgvector can drop
in later (multi-node cluster / embedding retrieval) without touching the loop —
the same ports/adapters move as Confluence/Jira/GitHub.

## 8. How to run and poke at it

```bash
poetry install
poetry run python main.py        # one full org cycle (in-memory, no creds)
poetry run pytest                # the full suite
poetry run mypy --strict sis/ main.py scripts/   # the type gate
```

Good first experiments to build intuition:

- Make `runtime/target.py` already fast (paste the O(√n) version in) and run a
  cycle — watch it roll back with "no improvement."
- Add a bug to a candidate in `tests/test_adversarial.py` and watch which gate
  catches it.
- Set `SIS_PROPOSER=claude` (with `--with llm` + a key) and read the prompt in
  `sis/proposer.py` — then watch the same gauntlet judge a real LLM's diff.
- Run a few cycles with `SIS_EPISODIC_STORE=duckdb` (`--with analytics`), then
  query the log: `get_episodic_store("duckdb").sql("SELECT reject_gate,
  count(*) FROM episodes GROUP BY reject_gate")`.

---

## 9. Glossary

- **omnibase** — this project / engine (the `sis/` package).
- **omnitrack** — the future end product (the external-world-modeling layer).
- **actor** — a Ray object living in its own process, reached via a handle.
- **artifact** — a durable record (page/issue/PR) the org coordinates through.
- **the gauntlet** — the deterministic validation pipeline; the safety moat.
- **provenance** — the recorded chain spec → … → outcome (live in `SelfModel`,
  persisted by the episodic store, `sis/episodic.py`).
- **episodic store** — the pluggable backend (jsonl/duckdb/none) that persists
  each cycle's outcome as the dataset to learn from.
