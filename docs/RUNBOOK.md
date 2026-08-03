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

- **A long-running loop.** `main.py` runs **one** cycle and exits; the SLO breach
  is *simulated*. A real server needs a monitor/scheduler loop fed by real
  metrics over a rolling window.
- **Ray Serve canary + atomic actor swap.** `DevOps.canary` records a green-slot
  deploy; there's no real weighted rollout or promote/swap yet.
- **AWS provisioning.** `SIS_ENV=aws` + `SIS_AWS_SECRET_ID` switch secrets to
  Secrets Manager (needs the secret + an IAM role with
  `secretsmanager:GetSecretValue`), but there is no infra/deploy code — the
  DevOps "provisions AWS" role is a stub.
- **Error-driven cycles.** Fixing exceptions (vs. optimizing speed) isn't wired;
  the STRICT-tier policy path exists but nothing triggers it.

When built, the cloud switch is: `export SIS_ENV=aws SIS_ADAPTERS=real
SIS_AWS_SECRET_ID=sis/prod/credentials` (region via `SIS_AWS_REGION`).

---

## Environment-variable reference

| Variable | Default | Effect |
|---|---|---|
| `SIS_PROPOSER` | `stub` | non-stub = a real LLM via `sis.llm` (needs `--with llm` + a provider key) |
| `SIS_LLM_PROVIDER` / `SIS_LLM_MODEL` | `anthropic` / provider default | which LLM backs the proposer (adapters in `sis/llm.py`) |
| `SIS_SANDBOX` | `subprocess` | `docker` = kernel-enforced (needs the image); **required** when `SIS_PROPOSER` is not `stub` |
| `SIS_ALLOW_UNSANDBOXED_LLM` | `0` | `1` = let a real proposer run in the soft subprocess sandbox (loud warning; unsafe — see M1) |
| `SIS_SANDBOX_IMAGE` | `sis-gauntlet:latest` | image for docker mode |
| `SIS_SANDBOX_MEMORY` / `SIS_SANDBOX_CPUS` | `1g` / `2` | per-container resource caps (docker mode) |
| `SIS_GAUNTLET_TIMEOUT` | `120` | per-gate wall-clock cap (seconds); docker mode also kills the container |
| `SIS_ADAPTERS` | `memory` | `real` = Confluence/Jira/GitHub/AWS (needs `--with real` + secrets) |
| `SIS_HTTP_TIMEOUT` | `30` | per-request timeout (s) for real-adapter calls, so a wedged tenant API can't hang a cycle; a bad value fails loudly |
| `SIS_ENV` | `local` | `aws` = secrets from AWS Secrets Manager |
| `SIS_SECRETS_FILE` | `secrets.local.yml` | override local secrets path |
| `SIS_AWS_SECRET_ID` / `SIS_AWS_REGION` | — | Secrets Manager secret id + region |
| `SIS_TARGET_PATHS` | `runtime/target.py` | SOFT-tier paths the loop may optimize |
| `SIS_ALLOW_STRICT_CHANGES` | `0` | `1` lets the loop touch non-guardrail engine code (still needs approval + justification) |
| `SIS_BUDGET_USD` | `5.0` | CEO hard spend cap (USD) — set **tiny** for a first real run so the brakes trip early |
| `SIS_BREAKER_THRESHOLD` / `SIS_MAX_COST_PER_ACCEPTED_USD` / `SIS_SLO_MIN_SPEND_USD` | `3` / `2.0` / `0.50` | the other CEO brakes; a bad value fails loudly |
| `SIS_EPISODIC_STORE` | `jsonl` | provenance log backend: `jsonl` \| `duckdb` (needs `--with analytics`) \| `none` |
| `ANTHROPIC_API_KEY` | — | required when `SIS_PROPOSER=claude` |

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
