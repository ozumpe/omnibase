# Known issues & limitations

Canonical list, from the 2026-07-25 full-project review (and a second pass on
2026-07-28 after the Level-3 validation — added **M5**, **M6**, **L10**–**L14**),
merged with the items previously tracked in `CLAUDE.md` / `README.md` / the
runbook / Confluence. IDs are stable — reference them in commits and PRs (e.g.
"Fix H1"). When an issue is fixed, move it to the "Resolved" section at the
bottom with the PR.

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

## Low

- **L5 — The gauntlet is hardwired to `sum_of_divisors`.** The independent
  reference and benchmark inputs are baked into the bench script, so widening
  `SIS_TARGET_PATHS` is illusory — any other target fails the benchmark gate.
  Fine for bootstrap; a real constraint before omnitrack (a target-contract
  redesign, not a quick fix). L5 is the Class-1 (optimization) slice of a larger
  "contract" idea — see [`docs/CLASS2_CONTRACT.md`](CLASS2_CONTRACT.md) for how the
  same abstraction extends to verifying built *features*, and the suggested
  sequencing (do L5 first).
  *Field evidence (2026-07-28, cycle `e86cf568c524`):* the second real cycle
  benchmarked **1.73µs vs 0.67µs** — the fixed 10-input workload is now at the
  timer's noise floor, where the ≥10% margin approaches jitter (the target has
  outgrown the benchmark). Separately, Claude edited the `benchmark()` harness
  itself (hoisting `perf_counter`) — inert only because the gauntlet drives
  `sum_of_divisors` with its own timer and ignores the candidate's `benchmark()`.
  Both are concrete, observed-in-production arguments for the target-contract
  redesign (a per-target reference + inputs, not a hardwired one).
- **L9 — Breaker/budget state is in-memory per CEO lifetime** (documented in
  the runbook): a fresh local run clears it. Acceptable locally; revisit
  together with M2 for persistent clusters.
- **L10 — L4 was only half-applied: `policy.target_paths()` still uses
  `lstrip("./")`.** The exact bug fixed in `_rel()` (2026-07-25, L4) survives one
  screen above it in the `SIS_TARGET_PATHS` parser: `lstrip("./")` strips `.`/`/`
  *characters*, not a `"./"` prefix, so `.github/x` → `github/x` and `../x` → `x`.
  The L4 regression test covers `_rel` only. Same one-word fix (`removeprefix`).
- **L11 — L8's sibling: a same-story retry still 422s at `open_pr`.**
  `create_branch` now reuses an existing branch (L8), but `POST /pulls` for a
  head that already has an open PR returns 422 "A pull request already exists" —
  unhandled in `open_pr`. The retry scenario L8 fixed dies one step later. Fix:
  on that 422, find and return the existing open PR for the head branch.
- **L12 — Episodic `reject_gate="timeout"` is unreachable.** A timed-out gate
  returns returncode 124 with the "timed out" text in `stderr`, but `validate()`
  reports the gate's generic reason (`"mypy --strict failed"` / `"pytest failed"`
  / `"benchmark script crashed"`) and puts the timeout text in `errors` — which
  `gate_from_reason()` never inspects. So a timeout is misattributed to the gate
  it timed out in, and the documented `timeout` value can never appear in the
  dataset the loop learns from. Fix: detect returncode 124 in `validate()` and
  set an explicit timeout reason.
- **L13 — The soft-sandbox network guard misses UDP + DNS.** `_NETWORK_GUARD`
  patches `connect`/`connect_ex`/`create_connection`, leaving `socket.sendto()`/
  `sendmsg()` (connectionless UDP) and `getaddrinfo` open. Docker mode
  (`--network none`) blocks all egress in the kernel, and the stub is trusted —
  so this only bites under `SIS_ALLOW_UNSANDBOXED_LLM=1`. Cheap defence-in-depth:
  patch the UDP send methods too.
- **L14 — `_put_file` refuses only FORBIDDEN, not STRICT.** The GitHub adapter's
  last-line write guard blocks only guardrail (FORBIDDEN) paths, so a future
  caller passing a STRICT engine path (e.g. `sis/org.py`) would be written —
  STRICT enforcement lives solely in the SWE's `authorize_change`. Defence in
  depth: require SOFT (or run the full `authorize_change`) at the write boundary.

**Minor (noted in the 2026-07-28 review; not separately tracked):** the policy
block path in `SWE.implement` omits `candidate_sha` from its return dict
(episodic gets `None`); a missing `tests/test_target.py` fails candidates with a
misleading "pytest failed"; `space_keys()`/`version_control_base()` re-read the
secrets file on every call; Jira `children()` builds JQL by f-string (internal
keys only); `transition()` raises if the *comment* POST fails after the
transition already succeeded; two gates are both commented "Gate 2" in
`gauntlet.py`.

## Sequencing for the first real-life test

The first real-life test = **real Claude proposer + real adapters + docker
sandbox** on the scratch tenant — the first run where untrusted generated code
and real money meet.

> **✅ Passed (2026-07-28).** All five steps below are done — see the Resolved
> entry for the validated cycle (`cb4f6fe13ed7`).

1. ~~Fix **H1** (+ **M3**) with regression tests.~~ **Done** — see Resolved.
2. ~~Enforce **M1** (docker sandbox with a real proposer).~~ **Done** — see
   Resolved. Image built (`docker build -t sis-gauntlet:latest
   -f Dockerfile.gauntlet .`) and the kernel sandbox smoke-tested with the stub.
3. ~~Fix **M4** (one-liner).~~ **Done** — see Resolved.
4. ~~Verify **L3**.~~ **Done** — the `PRICING` table matches published rates
   (see Resolved).
5. ~~Run `SIS_ADAPTERS=real SIS_PROPOSER=claude SIS_SANDBOX=docker` after a
   `--deep` preflight; compare the episodic log against the Anthropic bill.~~
   **Done** — see Resolved.

**M2** can wait until the AWS step (persistent-cluster problem); do it before
any long-lived cluster exists.

## Won't fix

- **L6 — Preflight doesn't verify the PAT's Pull-requests scope.** Not fixable
  in our code. `check_connections.py::check_github` confirms repo access
  (`GET /repos/{owner}/{repo}`), but a real cycle needs two distinct
  fine-grained-PAT permissions — **Contents: read/write** (`_put_file`) and
  **Pull requests: read/write** (`POST /pulls`) — and GitHub gives no reliable,
  read-only way to check them: the classic `X-OAuth-Scopes` header isn't
  populated for fine-grained tokens, and the `permissions` block on
  `GET /repos` reports only coarse `admin/push/pull` booleans that don't map to
  the Contents-vs-PR split. The only definitive test is to attempt a write,
  which a side-effect-free preflight must not do (no branches/commits/PRs). A
  probe-write hack (e.g. a deliberately-invalid `POST /pulls` and discriminating
  403-vs-422) is more fragile than the failure it guards against. **Disposition:**
  leave it — an under-scoped PAT fails loudly at `open_pr` with a 403 that
  points straight at the token, once, on first setup. Mitigation stays in the
  runbook: grant the PAT **Contents: read/write + Pull requests: read/write** up
  front.

## Resolved

- **M6** *(2026-07-28)* No HTTP timeouts on real-adapter calls — `requests`
  defaults to *no* timeout, so a wedged Confluence/Jira/GitHub API would hang a
  whole cycle with no breaker/bug/log. Fixed: `_session()` now returns a
  `_TimeoutHTTP` wrapper that applies a default `timeout` (30s, override
  `SIS_HTTP_TIMEOUT`; a bad value fails loudly) to every get/post/put, while an
  explicit per-call `timeout=` still wins. Covered by `tests/test_adapters_real.py`.
- **M5** *(2026-07-28)* The CEO budget/brakes had no config knob — the docs said
  "set a tiny budget for the first run" but the only path was editing source, so
  the L3 run used the hardcoded $5 cap. Fixed: `roles.ceo_config_from_env()` (a
  pure, unit-tested helper) reads `SIS_BUDGET_USD`, `SIS_BREAKER_THRESHOLD`,
  `SIS_MAX_COST_PER_ACCEPTED_USD`, `SIS_SLO_MIN_SPEND_USD` (defaults unchanged;
  an unparseable/negative value fails loudly), threaded through `org.bootstrap()`
  into the CEO. Documented in the env tables. (A detached CEO on a persistent
  cluster still ignores new args — tied to M2.)
- **First real-life test — PASSED** *(2026-07-28)* Real Claude proposer
  (`claude-opus-4-8`) + real adapters + kernel-enforced docker sandbox, on the
  scratch tenant. Cycle `cb4f6fe13ed7`: Claude proposed the O(√n) `isqrt` form
  against the naive O(n) baseline (re-seeded on `testrun/main` for the demo),
  the full gauntlet passed **inside the docker sandbox** (192.4µs → 1.6µs,
  99.2% faster), and it filed real artifacts — Confluence spec `6356994`, Jira
  `TES-20`, GitHub `testrun` PR #4 — then stopped at
  `verified_awaiting_human_merge`. Episodic-logged cost **$0.014375**,
  reconciled against the Anthropic console. Every stage of the loop fired
  end-to-end against live systems with real money for the first time.
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
- **M4** *(2026-07-25)* `SWE.implement` forked feature branches from a
  hardcoded `"main"` while `live_target_source` read `settings.default_base` —
  inconsistent on a repo whose default branch isn't `main`. Fixed: the new
  `settings.version_control_base()` (mirrors `space_keys()`) is the single
  source; the SWE forks from it. In-memory path still forks from `"main"`.
  Covered by `tests/test_settings.py`.
- **L3** *(2026-07-25)* Verified `cost.py`'s `PRICING` against published rates:
  `claude-opus-4-8` $5/$25 (the model the loop prices spend with),
  `claude-sonnet-4-6` $3/$15, `claude-haiku-4-5` $1/$5, cache 1.25×/0.1× — all
  correct. Added `claude-sonnet-5` ($3/$15 standard, the conservative choice
  over the intro rate). Guarded by `tests/test_cost.py`.
- **L2** *(2026-07-25)* The idempotent `create_page` fallback PUT a new version
  every run. Fixed: `_update_body` now fetches the stored body and skips the
  write (and version bump) when unchanged, emitting `page.unchanged`. Covered
  by `tests/test_adapters_real.py`.
- **L4** *(2026-07-25)* `policy._rel()` used `lstrip("./")`, which strips
  `.`/`/` characters rather than a `"./"` prefix (mangling `"../x"` → `"x"`).
  Fixed with `removeprefix("./")`. Covered by `tests/test_policy.py`.
- **L7** *(2026-07-25)* `measure_baseline()` fell back to `0.0` silently. Fixed:
  it now prints a warning to stderr (returncode + stderr) before returning the
  advisory `0.0`. Covered by `tests/test_gauntlet.py`.
- **L8** *(2026-07-25)* Re-running a cycle for an existing story 422'd on
  `create_branch`. Fixed: on "Reference already exists" the real GitHub adapter
  reuses the branch (emits `branch.exists`). Covered by
  `tests/test_adapters_real.py`.
- **L1** *(2026-07-25)* The labels the roles tag pages with (charter/spec/
  proposal/outline) were never written. Fixed (the write half): `create_page`
  now attaches them on both the create and update-in-place paths via the v1
  content-label endpoint (v2 has no label write), best-effort so a label
  failure never breaks a cycle (`page.labels_applied` / `page.labels_failed`).
  The `list_pages` `label` filter stays a no-op — v2 has no label filter and no
  caller uses it (documented in the adapter). Covered by
  `tests/test_adapters_real.py`.
