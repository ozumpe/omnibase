# omnibase Runbook — how to start it

Five levels, from "works right now with nothing" to "autonomous server on AWS."
Each level lists exactly what's required and the commands. Start at Level 0 and
climb only as far as you need.

> TL;DR: **Level 0 needs nothing** (`poetry install && poetry run python main.py`).
> The first *real* run is **Level 2** (your Confluence/Jira/GitHub), and its only
> real unknown is that the live adapters are untested against a real tenant.
> Running it as a continuous **autonomous server (Level 4)** is still
> development work, not just configuration.

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
the provenance graph, and the actor registry. The original single-actor
micro-loop is `poetry run python main.py --loop`.

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

---

## Level 2 — real Confluence / Jira / GitHub (first *real* run)

Required:
1. Copy and fill the secrets file (gitignored):
   ```bash
   cp secrets.example.yml secrets.local.yml
   ```
   Fill in: `atlassian.base_url`, `.email`, `.api_token`, `.cloud_id`,
   `.jira_project`; `github.token` (PAT, Contents: read/write), `.owner`, `.repo`.
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

⚠️ **The genuine unknown:** the real REST adapters in `sis/adapters_real.py` have
**not** been exercised against a live tenant. Do the first run against a
**scratch Jira project + throwaway repo**, and expect to fix edge cases
(transition names, Confluence storage-body quirks, GitHub Contents API). Each fix
should become a regression test.

What it does, end to end: drops a proposal page in Confluence → PM writes a spec
→ CTO creates a Jira epic/story → SWE opens a **PR** with the validated change on
a feature branch → QA verifies → DevOps records a green-slot canary. It **stops
at the human PR merge** — it never merges to `main` or promotes to live.

**On failure:** a gauntlet rollback or a QA rejection files a bug in the work
tracker automatically (`DevOps.file_bug`) — check there first, not just the
episodic log. Three consecutive failures trip the circuit breaker: it files a
second, distinctly-titled `CIRCUIT BREAKER OPEN` bug and every further cycle
returns `circuit_breaker_open` without spending anything. The breaker's state
lives in the CEO actor's memory, not on disk — a fresh `poetry run python
main.py` (a fresh local Ray cluster) clears it, unless you're connected to a
persistent one via `RAY_ADDRESS`.

---

## Level 3 — kernel-enforced sandbox (optional hardening)

Runs every gauntlet gate inside `docker run --network none --cap-drop ALL
--read-only`, only the temp dir mounted, no credentials. Recommended once a real
LLM is writing code.

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
| `SIS_PROPOSER` | `stub` | `claude` = real Claude API (needs `--with llm` + key) |
| `SIS_SANDBOX` | `subprocess` | `docker` = kernel-enforced (needs the image) |
| `SIS_SANDBOX_IMAGE` | `sis-gauntlet:latest` | image for docker mode |
| `SIS_GAUNTLET_TIMEOUT` | `120` | per-gate wall-clock cap (seconds) |
| `SIS_ADAPTERS` | `memory` | `real` = Confluence/Jira/GitHub/AWS (needs `--with real` + secrets) |
| `SIS_ENV` | `local` | `aws` = secrets from AWS Secrets Manager |
| `SIS_SECRETS_FILE` | `secrets.local.yml` | override local secrets path |
| `SIS_AWS_SECRET_ID` / `SIS_AWS_REGION` | — | Secrets Manager secret id + region |
| `SIS_TARGET_PATHS` | `runtime/target.py` | SOFT-tier paths the loop may optimize |
| `SIS_ALLOW_STRICT_CHANGES` | `0` | `1` lets the loop touch non-guardrail engine code (still needs approval + justification) |
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
