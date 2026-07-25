# Known issues & limitations

Canonical list, from the 2026-07-25 full-project review, merged with the items
previously tracked in `CLAUDE.md` / `README.md` / the runbook / Confluence.
IDs are stable — reference them in commits and PRs (e.g. "Fix H1"). When an
issue is fixed, move it to the "Resolved" section at the bottom with the PR.

> **Supersedes:** the "benchmark noise gate / timing jitter" item documented in
> v0.1.4 (CLAUDE.md next-steps, README roadmap, runbook Level 2, Confluence
> Risk 6). The jitter explanation was the wrong mechanism: post-merge no-op PRs
> passed the benchmark gate because of **H1** (stale baseline), not noise. Both
> **H1** and its coupled no-op short-circuit **M3** were fixed together (see
> Resolved).

## High

*(none open)*

## Medium

- **M2 — Detached actors live in an anonymous Ray namespace.** Harmless
  locally (the cluster dies with the process), but on a persistent cluster
  each run creates a new namespace: `ray.get_actor` finds nothing, so
  CEO/Workspace/SelfModel duplicate every run and accumulate forever, and
  breaker/budget state silently resets. **Fix before any AWS/persistent
  cluster:** `ray.init(namespace="sis")` + a deliberate decision on
  breaker-state lifetime.
- **M4 — SWE hardcodes the branch base.** `SWE.implement` calls
  `create_branch(branch, "main")` while `live_target_source()` reads
  `settings.default_base` — a target repo with a different default branch
  would read one branch and fork from another. Use `default_base` in both.

## Low

- **L1 — Confluence labels are never written.** `create_page` accepts labels
  and returns them on the `Page`, but they're not sent to the API (create or
  update), and the real `list_pages` ignores its `label` filter. Nothing in
  the cycle relies on labels today.
- **L2 — Idempotent `create_page` bumps a version even when unchanged.** The
  duplicate-title fallback PUTs a new version every run; skip the update when
  the body is identical to avoid version churn on fixed-title pages.
- **L3 — `cost.py` pricing is hardcoded.** The CEO spend brakes are only as
  good as the `PRICING` table; verify against currently published API rates
  before any run with real spend.
- **L4 — `policy._rel()` uses `lstrip("./")`,** which strips characters, not
  a prefix (`"../x"` → `"x"`). Benign today because `_put_file` only receives
  the fixed `TARGET_REPO_PATH`; tighten before paths become dynamic.
- **L5 — The gauntlet is hardwired to `sum_of_divisors`.** The independent
  reference and benchmark inputs are baked into the bench script, so widening
  `SIS_TARGET_PATHS` is illusory — any other target fails the benchmark gate.
  Fine for bootstrap; a real constraint before omnitrack.
- **L6 — Preflight doesn't verify the PAT's Pull-requests scope.**
  `check_connections.py` confirms repo access, but the
  "Contents-only token 403s on `open_pr`" failure the runbook warns about
  would still only surface mid-cycle.
- **L7 — `measure_baseline()` fails silently to 0.0,** which then flows into
  proposer prompts and the episodic log as a plausible-looking number.
- **L8 — Re-running a cycle for an existing story 422s on `create_branch`**
  (branch already exists on the remote). Only matters for retry flows.
- **L9 — Breaker/budget state is in-memory per CEO lifetime** (documented in
  the runbook): a fresh local run clears it. Acceptable locally; revisit
  together with M2 for persistent clusters.

## Sequencing for the first real-life test

The first real-life test = **real Claude proposer + real adapters + docker
sandbox** on the scratch tenant — the first run where untrusted generated code
and real money meet.

1. ~~Fix **H1** (+ **M3**) with regression tests.~~ **Done** — see Resolved.
2. ~~Enforce **M1** (docker sandbox with a real proposer).~~ **Done** — see
   Resolved. Still: build the image once (`docker build -t sis-gauntlet:latest
   -f Dockerfile.gauntlet .`) before the first real run.
3. Fix **M4** (one-liner).
4. Verify **L3** and set a deliberately tiny CEO budget for the first run to
   watch the brakes trip rather than trusting them.
5. Run: `SIS_ADAPTERS=real SIS_PROPOSER=claude SIS_SANDBOX=docker` after a
   `--deep` preflight; then compare the episodic log against the Anthropic
   console bill.

**M2** can wait until the AWS step (persistent-cluster problem); do it before
any long-lived cluster exists.

## Resolved

- *(2026-07-25, PR #32)* Confluence duplicate-title 400 crashed re-runs →
  `create_page` updates the existing page in place.
- *(2026-07-25, PR #32)* Cross-space `parentId` 404 crashed the spec page →
  parent dropped, provenance in the SelfModel.
- *(2026-07-25, PR #31)* Cycles baselined on the stale local file for the
  *proposer input* → `live_target_source()` pulls the merged target. (The
  gauntlet-internal half of this was **H1**, fixed below.)
- **H1** *(2026-07-25)* `gauntlet.validate()` benchmarked against the local
  `runtime/target.py` instead of the cycle's baseline → after a merge a no-op
  candidate passed every gate. Fixed: `validate()` takes an explicit
  `baseline_source` (the merged target), passed by the SWE and QA; falls back
  to the local file only for direct callers/tests.
- **M3** *(2026-07-25)* No identical-source short-circuit. Fixed alongside H1:
  a candidate byte-identical to the baseline is rejected up front as
  `no change` (episodic `reject_gate="noop"`), before the µs-scale benchmark
  race. Local in-memory demo still promotes; the H1/M3 behaviour is covered by
  new regression tests in `tests/test_gauntlet.py`.
- **"No change" was treated as a failure** *(2026-07-25)* Once M3 lands, a
  cycle against an already-optimal target ended in `rolled_back` — filing a bug
  and counting toward the circuit breaker, so three "nothing to improve" cycles
  falsely paged a human. Fixed: `run_cycle` returns a benign `no_change` status
  (no bug, no breaker increment); the CEO's new `record_neutral` records spend
  (so the hard spend cap + cost-per-accepted SLO still apply) but leaves the
  failure/accept counters untouched. Covered by `tests/test_org_no_change.py`.
- **M1** *(2026-07-25)* Subprocess sandbox let untrusted LLM code read host
  files. Fixed: `gauntlet.ensure_sandbox_allows_proposer()` raises when a
  non-stub `SIS_PROPOSER` runs without `SIS_SANDBOX=docker` — enforced fail-fast
  in `run_cycle` (before any spend/artifacts) and as a backstop in `validate()`.
  Loud, explicit override `SIS_ALLOW_UNSANDBOXED_LLM=1`. The stub (trusted,
  hand-written candidate) still runs in the subprocess sandbox. Covered by
  `tests/test_gauntlet.py`.
