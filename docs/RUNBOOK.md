# omnibase Runbook — how to start it

Five levels, from "works right now with nothing" to "autonomous server on AWS."
Each level lists exactly what's required and the commands. Start at Level 0 and
climb only as far as you need.

> TL;DR: **Level 0 needs nothing** (`poetry install && poetry run python main.py`).
> The first *real* run is **Level 2** (your Confluence/Jira/GitHub) — validated
> end-to-end against a live tenant, including re-runs. Running it as a continuous
> **autonomous server (Level 4)** is still development work, not just
> configuration.

---

## Prerequisites (all levels)

- **Python 3.14** — standard CPython, *not* free-threaded. Ray ships `cp314`
  wheels but not `cp314t`, so a `python3.14t` env can't install Ray.
- **Poetry** (`uv` also works).
- `poetry install` — core deps (Ray, etc.).

---

## Level 0 — local demo (nothing missing)

Runs the full actor org through one intake→deploy cycle, in-memory, no
credentials. The proposer is the offline stub; "GitHub/Jira/Confluence" are
in-memory artifact stores.

```bash
poetry install
poetry run python main.py
```

Expect: `cycle status: verified_awaiting_human_merge`, a before/after benchmark,
the provenance graph, and the actor registry.

**Verify the project health:**
```bash
poetry run pytest                                   # full suite
poetry run mypy --strict sis/ main.py scripts/      # type gate
poetry run ruff check .                             # lint gate
```

**Optimise the other target.** Two contracts ship; the engine names neither:

```bash
poetry run python main.py --contract sort   # runtime/sort_target.py
poetry run python main.py                   # runtime/target.py (default)
```

---

## Level 0a — the operator console (Panel)

View system state and edit configuration in a browser. Independent of
everything below it: the console never starts the engine, so "is it running?"
stays a question it can answer honestly.

```bash
poetry install --with ui
SIS_FRONTEND_AUTH=none poetry run python -m sis.frontend   # http://127.0.0.1:8080
```

`auth=none` is fine here and only here: it is **refused on any non-loopback
bind**. Local development needs it because registering a GitHub OAuth app for
`localhost` is busywork — and GitHub exempts `localhost` from its HTTPS
redirect-URI rule, so nothing is being weakened that would otherwise apply.

For the authenticated form, register an OAuth app, put its client id/secret in
`secrets.local.yml` under `frontend:`, and list the logins that may sign in:

```bash
poetry run python -m sis.frontend --frontend-port 8080   # config.yml: forbidden_auth: "github"
```

What the page enforces, and what it merely displays:

- **Displays** the tier badge and disables `forbidden_` widgets.
- **Enforces**, in `sis/operator.py`, that a `forbidden_` key is never written
  and a `strict_` one needs the confirmation checkbox — so driving the page
  directly gains nothing.
- **Warns** when a key is *shadowed*: `config.yml` is the third of four layers,
  so editing a key that a `SIS_*` variable currently supplies will save and
  still not take effect. Unset the variable, or use the environment layer.

Edits apply on **restart**. A running loop keeps the configuration it started
with, deliberately — see `sis/config.py`'s `file_layer()`.

> **Exposure note.** TLS terminates in front of Panel, never in it; the design
> and the Caddy config are in `docs/OPERATOR_FRONTEND.md`. Before putting this
> on a public address at all, consider a tunnel or Tailscale with the loopback
> bind: this console edits the settings of a system that executes generated
> code, and not being reachable beats any cipher suite.

---

## Level 0b — serve the target over HTTP (Ray Serve)

The target behind a real endpoint, so a canary has somewhere to send traffic.
Blue and green run **simultaneously with different code** — that is the whole
point; blue owns the plain route, green a suffixed one.

```bash
poetry run python -m sis.serving          # blue on /sort, green on /sort-green
```

Call it:

```bash
curl -s localhost:8000/sort       -X POST -d '{"args": [[3,1,2]]}'
curl -s localhost:8000/sort-green -X POST -d '{"args": [[3,1,2]]}'
# -> {"result": [1,2,3], "version": "...", "slot": "blue"|"green"}
```

The wire format is `{"args": [...]}` — the entry function's positional
arguments. That matches the oracle's `random_input`/`BENCH_INPUTS`, which are
already args tuples, so generated load needs no translation.

Each response carries its own `version` and `slot`, so a canary attributes a
sample to a version from the response rather than inferring it from routing —
under a weighted split the caller doesn't choose.

> **Sandbox note.** A replica *executes the source it is handed*. Serving the
> merged target is ordinary trusted code; serving a **candidate** means running
> LLM-generated code in a Ray worker, and a Serve replica is **not** the
> gauntlet sandbox (no scrubbed env, no egress block, no per-call timeout).
> That is the intended shape of a canary, but the guarantee there is procedural
> rather than kernel-enforced — see `sis/serving.py`'s module docstring.

---

## Level 0c — generate load against the served target

The canary needs traffic to judge a candidate on, and nothing external calls
omnibase. With the server from Level 0b running:

```bash
poetry run python -m sis.loadgen --url http://127.0.0.1:8000/sort -n 200 -c 8
# 200 requests, 8 concurrent, 51 req/s over 3.95s
#   p50=155.00ms  p95=237.90ms  p99=283.57ms  error_rate=0.0%
```

Point it at `/sort-green` to measure the candidate, and compare.

Inputs come from the **contract's oracle** (`random_input`) — the same trusted
generator the offline differential gate draws from, so there is one definition of
"a valid input for this target" rather than two that can drift.

**Concurrency is the point.** The gauntlet times one call at a time in a quiet
sandbox, so queueing, lock contention and GC pauses are invisible to it. Latency
here is measured client-side end to end, *including* time spent queued, because
that is what a caller experiences and what a p99 SLO is written against.

> Expect the advantage to look *smaller* here than in the offline benchmark.
> Measured runs of the two commands above: the merge-sort candidate is **~5x**
> faster in isolation (`main.py --contract sort`: 0.88ms → 0.17ms) but only
> **~30%** faster at p95 under 8-way concurrent load (237.90ms → 167.78ms).
> CPU-bound Python serialises on the GIL, so both versions become queue-bound.
> That gap between the two numbers is exactly why the canary exists — and why a
> promotion decision should rest on the live one.

---

## Level 0d — one canary end to end (serve → load → verdict)

Levels 0b and 0c stood the pieces up separately. This runs the whole online path
in one command: blue and a candidate behind a weighted/shadow router, real
concurrent load through it, and the live window fed to `evaluate_canary`.

```bash
poetry run python -m sis.serve_cloud -n 300 -c 8
# blue=blue-live green=green-candidate mode=shadow weight=1.0
# driving 300 requests at concurrency 8 → http://127.0.0.1:8000/sort
#
#   blue   p50=  47.86ms  p95=  88.99ms  p99= 126.57ms  errors=0.0%  n=300
#   green  p50=  32.86ms  p95=  63.14ms  p99=  72.90ms  errors=0.0%  n=300
#   client-side: 40 req/s over 7.57s
#
# verdict: PASS — canary passed on 300 live samples
#   samples=300 disagreements=0 blue_errors=0 green_errors=0
```

`--mode split` for a plain weighted split (`--weight 0.05` to ramp), `--contract`
to pick a target.

**Shadow is the default, and it is what makes this a paired comparison.** Every
request goes to *both* versions and only blue's answer is returned to the caller
— so the candidate can be arbitrarily wrong without a client ever seeing it, and
both latencies come from the same request at the same instant, which removes the
traffic-mix confounder a weighted split can't avoid. Both columns show `n=300`
for that reason: 300 requests, each answered twice.

**Passing is eligibility, not promotion.** The human PR merge is what promotes —
the loop *observes* the merge (`DevOps.observe_merge`, polled by
`loop.serve(watch_merges=True)`) and only then applies it: blue is re-run with
the candidate's source and green is released. The agent cannot manufacture the
condition, because `merge_pr()` raises `RequiresHumanApproval` in every adapter.

Two guarantees worth knowing, both verified by tests rather than asserted:

- **Deploying a canary does not restart blue.** Green is a separate Serve
  application the router attaches to, precisely so adding one never re-runs the
  blue graph. The one-application version of this cycled the blue replica.
- **The green replica's environment is scrubbed.** Candidate code cannot read
  `ANTHROPIC_API_KEY`, `AWS_*` or any other env-carried credential. Network
  egress stays open by construction — a replica exists to answer HTTP — so this
  is a credential boundary, **not** the gauntlet's sandbox.

---

## Level 0e — the actor org drives its own live canary

Levels 0b–0d stood the canary up and drove it by hand. This is the automatic
version: one cycle through the whole actor org, with `DevOps.canary()` itself
deploying behind Ray Serve, filling the window, and deciding.

```bash
poetry run python main.py --contract sort --canary serve
# [main] cycle status: verified_awaiting_human_merge
#   live canary: PASS — canary passed on 150 live samples
```

`--canary serve` (or `SIS_CANARY=serve` exported before launch) is the only
difference from Level 0's plain `main.py --contract sort` — same cycle, same
PR, but now DevOps judges the candidate against real dispatched traffic instead
of recording a deploy nobody measured. `--loop --canary serve` runs this
continuously (`sis.loop.serve`'s `canary_backend` parameter).

**A live rejection changes the cycle's outcome.** Before this, QA approval
alone decided success; now a candidate that passes the offline gauntlet and QA
can still be rejected here — exactly the failure mode a canary exists to catch
(real concurrency/queueing the sandboxed benchmark cannot see, per Level 0c's
measurement). A rejection rolls the candidate back, files a bug, and reports
`canary_rejected` rather than `verified_awaiting_human_merge`.

**Forced shadow mode, not a default.** Neither shipped target has invariants
yet (that's Class 2 / OMNI-18), so a weighted split would have no live
correctness signal at all — only a speed comparison, which could silently wave
through a fast, wrong candidate. `DevOps.canary()` forces `SHADOW` regardless
of configuration until invariants exist.

The legacy in-memory recording (no traffic, no verdict) stays the default —
`--canary serve` is opt-in, and `poetry run python main.py` with no flags is
still Level 0's zero-setup path, unchanged.

---

## Level 1 — real Claude writes the optimization (cheap)

The LLM proposes the diff; the gauntlet still gates it, and the CEO spend brakes
(hard cap + cost-per-accepted SLO) apply automatically.

Required:
```bash
poetry install --with llm
export ANTHROPIC_API_KEY=sk-ant-...
export SIS_PROPOSER=claude
poetry run python main.py
```

Model is `claude-opus-4-8` (adaptive thinking, cached system prompt). Default
without the flag stays the offline stub.

⚠️ With a real LLM writing the code, the **docker sandbox (Level 3) is
required**, not just recommended: `SIS_PROPOSER=claude` now refuses to run
unless `SIS_SANDBOX=docker`. The default `subprocess` sandbox scrubs
credentials from the env and blocks egress, but candidate code can still *read*
host files like `secrets.local.yml` (M1 in `docs/KNOWN_ISSUES.md`); docker
mounts only the temp dir, so there is nothing to read. So Level 1 in practice
means Level 1 **+ Level 3**:

```bash
docker build -t sis-gauntlet:latest -f Dockerfile.gauntlet .   # once
export SIS_PROPOSER=claude SIS_SANDBOX=docker ANTHROPIC_API_KEY=sk-ant-...
export SIS_BUDGET_USD=0.25   # tiny cap for a first real run — brakes trip early
poetry run python main.py
```

The guard has a loud, explicit escape hatch — `SIS_ALLOW_UNSANDBOXED_LLM=1` —
for a quick throwaway test where you accept that untrusted code can read local
files. Never use it against anything with real credentials.

---

## Level 2 — real Confluence / Jira / GitHub (first *real* run)

Required:
1. Copy and fill the secrets file (gitignored):
   ```bash
   cp secrets.example.yml secrets.local.yml
   ```
   Fill in: `atlassian.base_url`, `.email`, `.api_token`, `.cloud_id`,
   `.jira_project`; `github.token`, `.owner`, `.repo`.

   The GitHub PAT should be **fine-grained, scoped to the single target repo**,
   with **Contents: read/write** (branch + file writes) *and* **Pull requests:
   read/write** (`open_pr`/`get_pr`). Contents alone is not enough — opening the
   PR will 403. `merge_pr` always raises `RequiresHumanApproval`, so the token
   can never merge regardless of its scopes.

   The target repo must be **non-empty**: `create_branch` resolves its base SHA
   via `GET /git/ref/heads/<default_base>`, which 404s on a freshly-created repo
   with no commits. Seed it with an initial commit first. (`runtime/target.py`
   does *not* need to pre-exist — the adapter creates it.)
2. Install the real adapters and switch them on:
   ```bash
   poetry install --with real
   export SIS_ADAPTERS=real
   ```
3. **Preflight (read-only) before any real cycle:**
   ```bash
   poetry run python scripts/check_connections.py --deep
   ```
   `--deep` confirms the Jira project's workflow has the statuses the org
   transitions to (`To Do`, `In Progress`, `Ready for Review`, `TBD`, `Done`) —
   the likeliest first-run failure.
4. Run a cycle: `poetry run python main.py`

The real REST adapters in `sis/adapters_real.py` **have been validated against a
live tenant** (scratch Jira project + throwaway repo), including repeated runs.
Still use a **scratch project and throwaway repo** — the cycle creates real pages,
issues, branches, and PRs. Live-tenant edge cases found so far are handled and
covered by regression tests:
- **Re-runs are idempotent for fixed-title pages.** Confluence enforces unique
  titles per space; on the duplicate-title 400, `create_page` finds the existing
  page and updates it in place (the charter/spec pages keep their IDs across runs).
- **Cross-space parents are dropped.** Confluence can't parent a page across
  spaces (spec page ← proposal page); the adapter retries without the parent and
  emits `page.parent_dropped` (provenance stays in the SelfModel).

What it does, end to end: drops a proposal page in Confluence → PM writes a spec
→ CTO creates a Jira epic/story → SWE opens a **PR** with the validated change on
a feature branch → QA verifies → DevOps records a green-slot canary. It **stops
at the human PR merge** — it never merges to `main` or promotes to live.

**After you merge the cycle's PR**, the next cycle pulls the target as merged on
the base branch (`live_target_source`), and the gauntlet benchmarks the
candidate against *that* — not the stale local file (this was H1, now fixed).
Once the target is already optimal, the stub re-proposes identical code and the
cycle ends benignly as **`no_change`** — no PR, no bug ticket, no circuit-breaker
increment. So a stub run against an already-optimised target is a clean no-op,
and to demo a *successful* cycle you need a target with real headroom (a fresh
repo seeded with the naive `runtime/target.py`).

**On failure:** a gauntlet rollback (wrong/slower/untyped candidate) or a QA
rejection files a bug in the work tracker automatically (`DevOps.file_bug`) —
check there first, not just the episodic log. (A `no_change` outcome is *not* a
failure: it files no bug and doesn't count toward the breaker.) Three
consecutive real failures trip the circuit breaker: it files a
second, distinctly-titled `CIRCUIT BREAKER OPEN` bug and every further cycle
returns `circuit_breaker_open` without spending anything. The breaker + spend
state is **persisted to the episodic store** and rehydrated on bootstrap (unless
`SIS_EPISODIC_STORE=none`), so it survives a restart and spans a persistent/AWS
cluster (detached actors share the `sis` Ray namespace). To clear a trip
deliberately, call the CEO's `reset_breaker()` RPC — it clears the failure streak
but **not** the accumulated spend (the hard cap can't be bypassed by a reset). See
`docs/BRAKE_STATE_AND_ORACLE.md`.

---

## Level 3 — kernel-enforced sandbox (optional hardening)

Runs every gauntlet gate inside `docker run --network none --cap-drop ALL
--read-only`, only the temp dir mounted, no credentials. Recommended once a real
LLM is writing code.

> **✅ Validated end-to-end (2026-07-28).** A full real cycle (`claude-opus-4-8` +
> real adapters + this sandbox) proposed and verified an O(√n) optimisation on the
> scratch tenant, filing Confluence/Jira/`testrun` PR #4 and stopping at the human
> merge gate. See `docs/KNOWN_ISSUES.md` (Resolved) for the run details.

Required: a **running Docker daemon**, then:
```bash
docker build -t sis-gauntlet:latest -f Dockerfile.gauntlet .
export SIS_SANDBOX=docker
poetry run python main.py
```
Default `SIS_SANDBOX=subprocess` is the portable soft sandbox (scrubbed env +
in-process egress block).

---

## Level 4 — autonomous self-improving server on AWS (NOT yet built)

This is development work, not configuration. Currently missing:

- **A real monitor trigger.** The long-running loop exists — `poetry run python
  main.py --loop` runs cycles via `sis/loop.py` (pure `decide()` policy +
  injectable trigger, graceful SIGINT/SIGTERM stop, `SIS_LOOP_MAX_CYCLES` /
  `SIS_LOOP_INTERVAL`). What's still simulated is the *trigger*: it runs on a
  demo `repeat()`/`once()` intake, not a **sustained-SLO-breach detector over a
  rolling metric window** — which needs a served endpoint producing real
  metrics (the Ray Serve gap below).
- ~~**Ray Serve canary.**~~ **Done** — `DevOps.canary(canary_backend="serve")`
  deploys the candidate behind a real Serve router, drives real traffic
  through it, and lets `evaluate_canary()` decide (OMNI-14; Level 0e). Opt-in
  (`--canary serve` / `SIS_CANARY=serve`); the in-memory recording stays the
  default. The **atomic actor swap**, for internal never-served actors, is
  still unbuilt — a different mechanism (DESIGN.md §4), no design doc yet.
- **AWS provisioning.** `SIS_ENV=aws` + `SIS_AWS_SECRET_ID` switch secrets to
  Secrets Manager (needs the secret + an IAM role with
  `secretsmanager:GetSecretValue`), but there is no infra/deploy code — the
  DevOps "provisions AWS" role is a stub.
- **Error-driven cycles.** Fixing exceptions (vs. optimizing speed) isn't wired;
  the STRICT-tier policy path exists but nothing triggers it.

When built, the cloud switch is: `export SIS_ENV=aws SIS_ADAPTERS=real
SIS_AWS_SECRET_ID=sis/prod/credentials` (region via `SIS_AWS_REGION`).

---

## Configuration reference

Every knob lives in **[`config.yml`](../config.yml)** — one file, generated from
the schema in `sis/config.py`, carrying each key's default, its documentation,
its environment variable, and its CLI flag. Read it there rather than from a
table here: this section used to *be* that table, and it had drifted (its
`SIS_TARGET_PATHS` default still listed one path when the engine had three).

```bash
poetry run python main.py --show-config     # every value + which layer set it
```

```
CLI flag  >  environment variable  >  config.yml  >  built-in default
```

Which means the `export SIS_…` lines throughout this runbook still work exactly
as written — they are the environment layer — and each now has two other
spellings if you prefer them:

```bash
export SIS_SANDBOX=docker                    # environment: applies to this shell
poetry run python main.py --sandbox-mode docker   # CLI: applies to this run
# or edit sandbox.forbidden_mode in config.yml    # file: applies to every run
```

Three notes that matter operationally:

- **`--show-config` is the first thing to run when something behaves
  unexpectedly.** It reports the *source* of each value, so a stale
  `export SIS_BUDGET_USD` in your shell profile that is quietly overriding
  `config.yml` shows up as `[from env]` instead of staying invisible.
- **Bad values fail loudly, including typos in a choice.** `SIS_SANDBOX=dcoker`
  is now rejected; it used to compare unequal to `"docker"` and silently run
  untrusted code in the soft subprocess sandbox.
- **`config.yml` is read once per process, at start-up.** Editing it mid-run
  cannot change a running loop half-way through a cycle; restart to apply.

**Not in `config.yml`:** credentials. `ANTHROPIC_API_KEY` and the
Confluence/Jira/GitHub tokens are secrets and live in `secrets.local.yml` (or
AWS Secrets Manager) — see Level 2. `config.yml` is committed, so anything in it
is public by construction.

---

## Inspecting the episodic log

Every cycle records its outcome (spec → diff → gauntlet verdict → outcome,
including rejected diffs and the gate that caught them) via
`SIS_EPISODIC_STORE`.

- **Default (`jsonl`):** `runtime/episodic.jsonl` — one JSON object per cycle.
- **DuckDB:** `poetry install --with analytics`, `export SIS_EPISODIC_STORE=duckdb`,
  then query `runtime/episodic.duckdb`:
  ```python
  from sis.episodic import get_episodic_store
  store = get_episodic_store("duckdb")
  print(store.summary())                          # rollups: reject-rate by gate, cost/accepted
  store.sql("SELECT reject_gate, count(*) FROM episodes GROUP BY reject_gate")
  ```
- **Disable:** `export SIS_EPISODIC_STORE=none`.

---

## Troubleshooting

- **`pyproject.toml changed significantly since poetry.lock`** → run `poetry lock`
  after any `pyproject.toml` edit (CI fails on a stale lock).
- **Ray won't install** → you're on free-threaded `python3.14t`; use standard
  CPython 3.14.
- **`SIS_SANDBOX=docker` errors "docker not found / daemon"** → start Docker and
  build the image (`Dockerfile.gauntlet`), or unset for the subprocess sandbox.
- **Jira `No transition to 'X' available`** → your workflow lacks that status
  name; run `check_connections.py --deep` to see the gap.
- **`check_connections.py` shows all SKIP** → `secrets.local.yml` isn't filled in
  or `SIS_ADAPTERS` isn't `real`.
