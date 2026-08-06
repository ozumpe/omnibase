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

*(none open)*

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

  *Stronger field evidence — non-deterministic verdicts (2026-07-28, runs 4 & 5
  on `testrun`).* Over five real cycles the loop compounded four genuine
  improvements (naive → O(√n) → prime-factorisation → 6k±1-wheel), driving the
  target to sub-microsecond. Then the noise floor flipped the gate's **decision**,
  not just its magnitude:
  - **Run 4** (`c177ddc71747`) — same merged factorisation target, benchmarked at
    ~**1.0µs**; the candidate couldn't clear ≥10% → `rolled_back` (`reject_gate=
    benchmark`), which filed bug `TES-36`.
  - **Run 5** (`6778371f9fd7`) — the *same* target benchmarked at **0.76µs** (a
    ~30% swing from pure jitter); a candidate at 0.58µs scored "23.5% faster" →
    **accepted** (PR #9).

  So on a target within measurement noise of optimal the gate both **false-rejects**
  and **accepts on an untrustworthy magnitude** — its verdict is now partly a coin
  flip, and because an accept resets the consecutive-failure counter the circuit
  breaker may never trip on a converged target. This is the decisive argument that
  a fixed-input wall-clock benchmark cannot be the correctness oracle past a point;
  the target contract needs per-target inputs sized to stay above the timer's
  resolution (and/or an operation-count / statistical-significance gate).

  *Design-review findings on [`docs/SERVE_CANARY.md`](SERVE_CANARY.md) (2026-08-05,
  design sketch — nothing here is implemented yet), checked against the current
  `sis/ports.py` / `sis/roles.py` / `sis/policy.py` / `sis/adapters.py`. First four
  fixed same-day by correcting the design text (still nothing to implement — no
  `Contract`/`Cloud.shift_traffic`/etc. exist in code yet); last two still open:*
  - ~~**`Cloud` Protocol break.**~~ **Fixed in the doc.** `shift_traffic`/
    `live_metrics` would've broken the `@runtime_checkable Cloud` Protocol for
    `InMemoryCloud` (`sis/adapters.py:165`) *and* `RealCloud`
    (`sis/adapters_real.py:459`), not just the one the doc called out. Sequencing
    step 7 and the `ServeCloud` section now say both adapters need a stub in the
    same step.
  - ~~**`specs/` isn't actually FORBIDDEN yet.**~~ **Fixed in the doc**, and a
    sharper gap than first written: `classify()` matches `GUARDRAIL_PATHS` by
    *exact* string equality, not directory prefix, so even adding a bare
    `"specs/"` entry would silently protect nothing. `CLASS2_CONTRACT.md` now
    states this as two required changes for L5 Layer 1 — add the contract paths
    *and* teach `classify()` directory-prefix matching (or enumerate contract
    modules individually) — instead of claiming present-tense enforcement.
  - ~~**`DevOps.canary()`'s signature doesn't stretch to this design.**~~ **Fixed
    in the doc.** Sequencing step 10 now says "rework," not "wire," and spells out
    that today's one-scalar `canary(pr_id, candidate_latency)` needs a `Contract` +
    live samples + latency arrays instead, plus a PR/target→`Contract` lookup that
    doesn't exist yet in `Workspace`/`SelfModel`.
  - ~~**`CanaryVerdict` field mismatch.**~~ **Fixed in the doc** — the dataclass
    sketch now uses `baseline_p95`/`candidate_p95` (matching the gate 2 prose)
    instead of `p50`.
  - ~~**No stated concurrency rule once `serve_breach()` lands (step 11).**~~
    **Fixed in the doc.** New rule: the impure wrapper around `serve_breach()`
    (`loop.serve()`/`run_loop()`, not the pure function itself) checks
    `SelfModel`'s existing green-slot state before calling `propose()` again — a
    canary already in flight holds the next cycle rather than starting one
    concurrently. Reuses existing slot-tracking state; no new field.
  - ~~**Doesn't reconcile with the "atomic actor swap" path.**~~ **Fixed in the
    doc.** New "Scope" section: this doc covers only the Ray-Serve/HTTP-fronted
    half of `DESIGN.md` §4; the shadow-run-then-atomic-handle-swap path for
    internal, never-served actors is out of scope here, not superseded, and has
    no design doc yet — `evaluate_canary()`'s two gates are reusable for it,
    only the traffic-splitting/promote mechanics differ. A decision rule picks
    the mechanism per target (Serve/HTTP → this doc; actor-to-actor only → the
    not-yet-written atomic-swap doc).

**Minor (noted in the 2026-07-28 review; not separately tracked):** all six
fixed 2026-08-05 — see the **Minor batch** entry under Resolved.

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

- **Minor batch** *(2026-08-05)* The six unnumbered items from the 2026-07-28
  review, each with a regression test that fails without its fix:
  - **`candidate_sha` dropped on the policy-block path** — `SWE.implement`'s
    gauntlet-fail and success returns carried it, the policy-block return did
    not, so a policy-blocked cycle was logged with `candidate_sha=None`: the one
    field tying that episode to the exact diff, missing from precisely the
    rejection you most want to audit. Covered structurally by
    `tests/test_roles_contract.py` (every exit path must carry the key —
    reaching that branch for real needs a cluster plus a mispointed target).
  - **A missing `tests/test_target.py` was blamed on the candidate.** The gate
    fell through to `pytest <a directory that was never created>`, which exits
    non-zero and surfaced as `"pytest failed"` — a valid candidate rejected with
    a reason pointing at the wrong side of the fence. Now fails closed (an unrun
    correctness gate is not a pass) with a `harness:` reason naming the real
    cause, and `gate_from_reason()` maps it to a new `harness` gate *before* the
    gate-name substring checks — otherwise a broken harness reads in the
    analytics as "candidates keep failing pytest".
  - **Settings re-read per call.** `space_keys()`/`version_control_base()` are
    called several times per cycle from inside the Ray actors, and each call
    re-read and re-parsed the whole secrets source — a redundant file read
    locally, a redundant **Secrets Manager round-trip** under `SIS_ENV=aws`. New
    `settings.cached_settings()` (+ `reset_settings_cache()` for tests) caches
    the **no-argument** path only; `load_settings(source)` still reads every
    time, so explicit-source callers are unaffected.
  - **Jira `children()` built JQL by f-string.** `/search/jql` takes JQL as a
    string with no parameter binding. Only internal keys reach it today — but
    that is a property of the callers, not an enforced one, and Confluence
    intake exists to let outside text into the org. `parent_id` is now validated
    against Jira's key grammar at the boundary, before any request goes out.
  - **`transition()` was undone by a failed comment.** The comment POST was
    chained onto the transition with `raise_for_status()`, so a 500 on the
    *comment* raised after the transition had already been applied and could not
    be rolled back. The caller saw the whole transition fail and retried, but
    the issue had moved — the retry found no matching transition and hard-failed
    the cycle. Now best-effort (mirroring `_apply_labels`), emitting
    `issue.comment_failed`: an audit note must not cost the state change it
    annotates.
  - **Duplicate gate numbering** in `gauntlet.py` — the no-op check and mypy
    were both commented "Gate 2". The no-op check is now "Gate 1b", keeping the
    rest aligned with `DESIGN.md` §5.
- **M2 + L9** *(2026-07-29)* Detached actors now share the `sis` Ray namespace and
  are created with atomic `get_if_exists=True`, so a persistent/AWS cluster reuses
  the one CEO/Workspace/SelfModel across runs instead of duplicating them into
  fresh anonymous namespaces. The CEO's brake/spend state is persisted to the
  episodic store (new `save_state`/`load_state` on the port + jsonl/duckdb/null
  backends) after every cycle and rehydrated on a *fresh* bootstrap — so the spend
  cap and breaker survive a cluster/actor restart (L9). A `CEO.reset_breaker()`
  admin RPC clears the trip **without** resetting spend (no budget bypass). Design:
  [`docs/BRAKE_STATE_AND_ORACLE.md`](BRAKE_STATE_AND_ORACLE.md); covered by
  `tests/test_ceo_state.py` + `tests/test_episodic.py`. *Deferred* (next steps in
  that doc): the breaker-cause split (goal-exhaustion vs quality) and the
  oracle-hashed auto-reset, which need the L5 target contract to hash against.
- **L10–L14** *(2026-07-28, one batch)* Five low-severity fixes, each with a
  regression test:
  - **L10** — `policy.target_paths()` used `lstrip("./")` (strips `.`/`/`
    *characters*, mangling `.github/x` → `github/x`); now `removeprefix("./")`,
    matching the L4 fix in `_rel()`.
  - **L11** — a same-story retry 422'd at `open_pr` (L8's sibling); it now finds
    and reuses the existing open PR for the head (emits `pr.exists`).
  - **L12** — a timed-out gate was misreported as that gate's generic failure;
    `validate()` now detects returncode 124 and returns a timeout reason, and
    `gate_from_reason()` checks timeout first so `reject_gate="timeout"` is
    reachable.
  - **L13** — the soft-sandbox network guard now also blocks UDP
    (`sendto`/`sendmsg`) and DNS (`getaddrinfo`), not just TCP connect.
  - **L14** — `_put_file` now requires the path be **SOFT** (refusing STRICT
    engine code too, not only FORBIDDEN) — defence in depth at the write boundary.
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
