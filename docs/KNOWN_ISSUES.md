# Known issues & limitations

Canonical list, from the 2026-07-25 full-project review (and a second pass on
2026-07-28 after the Level-3 validation — added **M5**, **M6**, **L10**–**L14**),
merged with the items previously tracked in `CLAUDE.md` / `README.md` / the
runbook / Confluence. IDs are stable — reference them in commits and PRs (e.g.
"Fix H1"). When an issue is fixed, move it to the "Resolved" section at the
bottom with the PR.

**Every entry starts with its Jira ticket** — open, won't-fix and resolved
alike. A new issue gets a ticket before it gets an entry here; the resolved
and won't-fix entries that predate that rule were backfilled on 2026-09-26
(label `backfilled`; won't-fix tickets stay open with label `wont-fix`).
`tests/test_known_issues.py` fails on an entry without one, and on a ticket
reference with no link target below.

> **Supersedes:** the "benchmark noise gate / timing jitter" item documented in
> v0.1.4 (CLAUDE.md next-steps, README roadmap, runbook Level 2, Confluence
> Risk 6). The jitter explanation was the wrong mechanism: post-merge no-op PRs
> passed the benchmark gate because of **H1** (stale baseline), not noise. Both
> **H1** and its coupled no-op short-circuit **M3** were fixed together (see
> Resolved).

> **2026-09-26 review.** H3–H4, M8–M23 and L15–L43 come from a multi-dimension
> review: six review lenses, an adversarial skeptic pass on every finding (two
> skeptics for high-severity ones), and a completeness critic. The reviewers
> produced 57 findings: 54 survived verification, and the 3 refuted ones are not
> listed. The critic added 3 more that no skeptic checked: H4 (reproduced
> independently afterwards), M11 (reproduced by the critic) and L41 (from reading
> the workflow file). Overlapping findings are merged into one entry.
> Severities are the skeptics' corrected ratings, usually lower than the
> reviewers' own, because several paths are opt-in (`--canary serve`) or latent
> (Class 2 isn't wired into the loop yet). Each entry says whether it was
> reproduced or only confirmed by reading the code.
>
> | ID | Jira |
> |---|---|
> | H2, M8, M9, M11 | [OMNI-45](https://olafzumpe.atlassian.net/browse/OMNI-45) (epic [OMNI-43](https://olafzumpe.atlassian.net/browse/OMNI-43)) |
> | H4 | [OMNI-46](https://olafzumpe.atlassian.net/browse/OMNI-46) (epic OMNI-43) |
> | M10 | [OMNI-47](https://olafzumpe.atlassian.net/browse/OMNI-47) (epic OMNI-43) |
> | H3 | [OMNI-48](https://olafzumpe.atlassian.net/browse/OMNI-48) (epic [OMNI-44](https://olafzumpe.atlassian.net/browse/OMNI-44)) |
> | M19 | [OMNI-49](https://olafzumpe.atlassian.net/browse/OMNI-49) (epic OMNI-44) — **blocks OMNI-29** |
> | M20, M21 | [OMNI-50](https://olafzumpe.atlassian.net/browse/OMNI-50) (epic OMNI-44) |
> | M15 | [OMNI-51](https://olafzumpe.atlassian.net/browse/OMNI-51) — **blocks OMNI-29** |
> | M12 | [OMNI-52](https://olafzumpe.atlassian.net/browse/OMNI-52) |
> | M13 | [OMNI-53](https://olafzumpe.atlassian.net/browse/OMNI-53) |
> | M14 | [OMNI-54](https://olafzumpe.atlassian.net/browse/OMNI-54) |
> | M16 (and L30) | [OMNI-55](https://olafzumpe.atlassian.net/browse/OMNI-55) |
> | M17 | [OMNI-56](https://olafzumpe.atlassian.net/browse/OMNI-56) — partly done by #108 (QA returns its reason; neutral verdicts routed) |
> | M18 | [OMNI-57](https://olafzumpe.atlassian.net/browse/OMNI-57) |
> | M22 | [OMNI-58](https://olafzumpe.atlassian.net/browse/OMNI-58) — blocked by OMNI-51 |
> | M23 | [OMNI-59](https://olafzumpe.atlassian.net/browse/OMNI-59) |
> | L21, L23 | [OMNI-61](https://olafzumpe.atlassian.net/browse/OMNI-61) (brake state fails closed; `sis.admin`) — **blocks OMNI-29** |
> | L24 | [OMNI-62](https://olafzumpe.atlassian.net/browse/OMNI-62) (Notifier port) — **blocks OMNI-29** |
> | M7 | [OMNI-88] — **won't fix** for now (label `wont-fix`); see the Won't fix section |
> | L15–L20, L22, L25–L29, L31–L38, L40–L43 | one ticket each, [OMNI-64]–[OMNI-87], on each entry below. Each is linked (Relates) in Jira to the ticket it should ship with. |
> | L30 | [OMNI-55], with M16 |
> | L39 | [OMNI-42] |

## High

- [OMNI-45] **H2 — The benchmark verdict can be forged from inside the measured
  process** *(found 2026-09-26 by a statistics-lens review of the merged
  OMNI-41 gate; reproduced)* — the Class-1 benchmark runs the candidate in the
  same Python process as the harness that times it and reports the verdict.
  OMNI-41 moved the verdict onto a duplicate of stdout, which stops a
  candidate's *prints* (incl. `atexit`), but a candidate can reach the
  duplicate itself: at import, write a fabricated `PAIRS`/`BASELINE`/`END` to
  `__main__._out` and `os._exit(0)`. The naive `sum_of_divisors` plus that
  forger passes **every** gate, reporting 1µs, and exits before the
  differential-correctness loop — so a *wrong* candidate passes too. Closing
  `_out` would not help (the dup'd fd is reachable with `os.write`), and any
  in-process secret is readable by code that runs at import. The same reach
  allows patching `time.perf_counter`, rebinding `base_fn`, or burning the GIL
  from a thread during baseline windows. **Not new in kind** — the pre-OMNI-41
  gate was forgeable with `print("1e-9 1.0"); os._exit(0)` — but OMNI-41's code
  and docs claimed a private channel, which was false. Mitigations today: the
  mandatory human PR merge, and (since this entry) a candidate that zeroes or
  NaNs the timings gets a counted failure (`benchmark unmeasurable`, or
  `benchmark output malformed` when the baseline is zeroed too) rather than the
  neutral inconclusive it could previously steer to. Fix: run the candidate (and baseline) in separate
  worker processes and time them from the harness, so candidate code never
  shares a process with the clock or the verdict channel. Pinned by the
  strict-xfail `test_a_candidate_cannot_forge_the_verdict_through_the_harness_itself`,
  which flips to a failure the day this is fixed.

- [OMNI-48] **H3 — A Serve-canary candidate runs as a full Ray control-plane worker, not
  in any sandbox** *(found 2026-09-26 by a multi-dimension review; confirmed by
  two independent skeptics reading the code, not run against a live cluster)* —
  `TargetDeployment._load` (`sis/serving.py:125`) `exec`s the candidate's source
  inside a Serve replica, which is an ordinary Ray worker on the same cluster as
  the detached, named `sis`-namespace actors. The OMNI-13 env scrub only
  blanks environment variables; it does nothing about cluster membership. Any
  worker can call `ray.get_actor(name, namespace="sis")` — the exact seam
  `tests/test_merge_observation.py::_mark_merged` already uses, with its own
  docstring warning "the agent may not" — and use `__ray_call__` to run
  arbitrary code inside `Workspace`, `CEO`, `SelfModel` or `DevOps`. A candidate
  could merge its own PR (`Workspace.vcs` holds an authenticated session under
  real adapters), promote itself once `observe_merge` sees that merge, reset
  the circuit breaker, or read tokens held in memory. The canary runs **before**
  the mandatory human PR merge, so that review does not cover this, and the
  offline gauntlet cannot catch a payload gated on `ray.is_initialized()`
  (never true inside the docker sandbox). Fix: never `exec` candidate source in
  a process attached to the control-plane cluster — a scrubbed subprocess or
  container fed candidate calls over stdio/a socket, with no Ray connection.
  Until that lands, refuse `canary.backend=serve` for a non-stub proposer, the
  same way `ensure_sandbox_allows_proposer` refuses the soft sandbox for M1.

- [OMNI-46] **H4 — An output type that overrides `__eq__`/`__ne__` defeats every
  correctness gate, offline and online** *(found 2026-09-26 by a completeness
  critic; reproduced)* — every gate compares a candidate's output with a plain
  `==`/`!=`, candidate value on the left, so Python calls the candidate's own
  `__eq__` first: the Class-1 differential loop (`cand_fn(*args) !=
  oracle.reference(*args)`), every acceptance-test assertion, the roman
  round-trip invariant, and the backtest comparators' exact-match branches.
  Reproduced against the default contract: `class _Liar(int)` with `__eq__` →
  `True`, `__ne__` → `False`, `__hash__` → `0`, returned from
  `sum_of_divisors`, type-checks under `mypy --strict` (the declared return
  type is `int`) and gets `"all gates passed"` — instantly, since the "answer"
  never has to be computed. The same shape passes the roman acceptance cases
  and round-trip law with a `str` subclass. Only the mandatory human PR review
  stands between this and a merge. Fix: canonicalise candidate output to a
  plain, hashable value built from only builtin `int`/`float`/`str`/`bool`/
  `None`/`list`/`tuple`/`dict` before any comparison, in every gate script
  (differential, acceptance conftest shim, invariant, backtest).

## Medium

- [OMNI-45] **M8 — Every non-benchmark gate accepts exit code 0 as a pass, with no
  positive verdict token** *(found 2026-09-26; reproduced)* — `_gate_interface`,
  `_gate_acceptance`, `_gate_invariant` and `_gate_backtest` only check
  `returncode != 0`. A Class-2 candidate that calls `os._exit(0)` at import
  ends each gate's subprocess with code 0 before any assertion runs.
  Reproduced against `roman`: `to_roman`/`from_roman` return wrong values and
  the module calls `os._exit(0)`; `validate()` returns `"all gates passed"`.
  **Not reachable through the loop today** — `roman`/Class-2 contracts aren't
  in `DEFAULT_CONTRACTS` (`sis/contract.py` says wiring them in is its own
  change) — so this is latent, not live, but it must close before that wiring
  lands. Fix: require a positive, host-verified completion signal per gate
  (e.g. pytest `--junitxml` with the expected test count and zero failures; a
  per-run nonce for interface/invariant/backtest), never a bare exit code.

- [OMNI-45] **M9 — The per-validation temp directory is shared, writable, and reused
  across every gate, so a candidate can rewrite the exam files later gates
  trust** *(found 2026-09-26; reproduced)* — `validate()` writes `oracle.py`,
  `baseline.py` and `sitecustomize.py` once, then hands the same directory
  (on `PYTHONPATH`, mounted `:rw` in docker, owned by the host user in both
  sandbox modes) to every gate. The candidate runs first, in the interface
  gate, and can overwrite those files before the differential, benchmark,
  invariant and backtest gates read them. Reproduced against
  `sum_of_divisors`: a candidate using the naive (wrong-above-9000) algorithm
  appends matching wrong behaviour to `oracle.py` and a `sleep`-padded wrapper
  to `baseline.py` at import; `validate()` returns `"all gates passed"`
  reporting a fabricated 45µs. **Distinct from H2** — nothing here touches the
  harness process, only files on disk, so H2's proposed worker-process fix
  would not close it by itself. Fix: mount trusted files read-only (docker
  `:ro`; `chmod 0444` in subprocess mode) in a directory separate from the
  candidate's own writable scratch space, which should sit off `sys.path`
  ahead of stdlib; verify a hash of every trusted file before each gate.

- [OMNI-47] **M10 — Candidate and reference/baseline share mutable argument objects, so
  a candidate can sabotage its own comparison** *(found 2026-09-26;
  reproduced)* — the differential loop calls `cand_fn(*args)` before
  `oracle.reference(*args)` on the same list objects; the paired benchmark
  passes the same `batch` lists to `timed(cand_fn, batch)` and
  `timed(base_fn, batch)`. Reproduced against `sort`: a candidate that returns
  `[]` for any list longer than 5 (clearing its input first) passes every
  gate; so does a plain, no-faster bubble sort that appends to the shared list
  so the baseline side looks slower. `specs/sort/tests.py::
  test_does_not_mutate_its_input` only exercises a 3-element list, so the size
  threshold slips past it, and the sort contract declares no invariants or
  backtest to catch it another way. Fix: `copy.deepcopy` each side's
  arguments, computed outside the timed window, in both the differential and
  benchmark scripts.

- [OMNI-45] **M11 — A candidate's own exception inside the invariant or backtest gate is
  filed as a harness/sandbox fault, not a candidate failure** *(found
  2026-09-26 by a completeness critic; reproduced)* — `sis/invariant.py`'s
  property wrapper catches only `AssertionError` around the candidate's call;
  `sis/backtest.py` catches nothing at all around it. Any other exception
  (e.g. an `IndexError` on an input the acceptance tests don't cover) makes
  the gate script exit non-zero for a reason that isn't the counted
  violation, and `_gate_invariant`/`_gate_backtest` map any other non-zero
  exit to `"harness: ... crashed"` — precisely the misattribution **OMNI-37**
  exists to prevent, in two gates it didn't reach. Reproduced against
  `roman`: a candidate whose `to_roman` raises `IndexError` for
  `2000 <= value < 3000` (outside the acceptance range) gets
  `"harness: the invariant script crashed"`, and
  `episodic.gate_from_reason` records `"harness"` — an operator would debug a
  healthy sandbox, and reject-by-gate analytics under-count invariant
  failures. Fix: wrap only the candidate's own call inside each gate script
  and turn any exception into a counted violation (e.g. re-raise as an
  `AssertionError` naming the candidate's exception, so Hypothesis can shrink
  it and the seed still rides in the reason).

- [OMNI-52] **M12 — A `soft_` operator config edit can rewrite `forbidden_` keys through
  unescaped YAML rendering** *(found 2026-09-26; reproduced end to end
  through the real `operator.save_edits`)* — `_render_scalar`
  (`sis/config.py`) wraps string values in `f'"{value}"'` with no escaping;
  `contracts.default` is a `soft_` `OPT_STR` key with no `choices`, so
  `parse_value` accepts arbitrary text for it; `save_edits` never re-parses
  the file it just wrote to check it matches what was intended. PyYAML keeps
  the *last* of two duplicate top-level keys. Reproduced: one
  `contracts.default` edit containing an embedded `brakes:`/`policy:` block
  raised the effective spend cap to $1,000,000, enabled STRICT changes, and
  widened `target_paths` — while `runtime/operator_audit.jsonl` recorded only
  the harmless `soft_` edit. A lone stray `"` in the value makes `config.yml`
  fail to parse (every engine process then refuses to start). Fix: render
  every scalar with `json.dumps` (valid YAML, escapes quotes/newlines/
  backslashes) or `yaml.safe_dump`; in `save_edits`, re-parse the rendered
  result and refuse the write unless it round-trips to exactly the intended
  values; write via a temp file + `os.replace`.

- [OMNI-53] **M13 — GitHub OAuth is never actually installed on the operator console;
  the server runs unauthenticated** *(found 2026-09-26; reproduced against
  the installed Panel version, without starting Ray)* — `_install_oauth`
  (`sis/frontend.py`) only sets `pn.config.oauth_provider`/etc. and returns
  `{}`; `serve()` calls `pn.serve(...)` without passing `oauth_provider=` as
  an argument, and Panel's `get_server` only builds an auth provider when
  that *argument* (not the `pn.config` attribute) is set. Reproduced: the
  server's `auth_provider` comes back `NullAuth`, `sign_sessions=False`.
  Browser requests are refused with a 403 (the `authorize` callback receives
  `user_info=None` and raises on `.get`), but the Bokeh websocket path
  accepts an unsigned session token without ever calling the authorize
  callback, so a non-browser client can open a full session and save edits —
  including, combined with M12, `forbidden_` ones. Mitigated today only by
  the loopback-only bind (OMNI-29); this fails as soon as anything binds
  `SIS_FRONTEND_BIND` beyond loopback believing OAuth protects it. Fix: pass
  `oauth_provider`, `oauth_key`/`oauth_secret` and a `cookie_secret` to
  `pn.serve`; enable signed sessions; add a test asserting the built server's
  `auth_provider` is not `NullAuth`.

- [OMNI-54] **M14 — A worked example in a spec page can name any callable, so spec
  prose becomes executed code** *(found 2026-09-26; reproduced)* —
  `_worked_example_source` (`sis/contract_author.py`) emits
  `` assert {entry}(*args) == expected `` for any `\w+` identifier, never
  checking `entry` against the contract's `public_api`; builtins such as
  `exec`, `eval`, `open` and `__import__` resolve in the generated test
  module. Reproduced: a spec bullet `` `exec("...")` -> `None` `` produces a
  test that executes the quoted string, and `untranscribed_examples` reports
  nothing wrong with it. `check_discrimination` runs this draft before any
  human sees it; under the default (stub proposer) configuration that runs in
  the subprocess sandbox, which leaves the host filesystem readable (M1 only
  requires docker for a non-stub *proposer*, not for staging a document-store
  draft). Fix: transcribe an example only when `entry in public_api` (the
  caller already has `public_api` in hand); report anything else through
  `untranscribed_examples` as "names a function outside the contract's public
  API".

- [OMNI-51] **M15 — The real GitHub adapter and the SWE's policy check both hardcode
  `runtime/target.py`, so every non-default contract is judged against the
  wrong baseline on real adapters** *(found 2026-09-26 from three independent
  angles by the same review; confirmed by reading the code, not run against
  live GitHub)* — `TARGET_REPO_PATH = "runtime/target.py"`
  (`sis/adapters_real.py`) is used by `live_target_source`, `open_pr` and
  `get_pr` regardless of which contract is active; `SWE.implement`'s policy
  check (`sis/roles.py`) authorizes that same constant instead of
  `spec.target_path`. `SIS_ADAPTERS=real --contract sort` would give
  `measure_baseline` the `sum_of_divisors` module as "the current source", so
  the benchmark's baseline call raises `AttributeError` (misattributed to the
  candidate as a benchmark crash); for a Class-2 contract needing no
  baseline, the resulting PR would overwrite `runtime/target.py` with
  unrelated code, and the policy check approves that write because it
  matches the path it was told to authorize. **Matters directly for OMNI-29**
  if the run uses any contract other than the default. Fix: thread
  `spec.target_path` through the `VersionControl` port (`live_target_source`,
  `open_pr`, `get_pr`) and through `policy.authorize_change`, dropping the
  hardcoded constant.

- [OMNI-55] **M16 — Any exception after the LLM call loses that call's spend from the
  CEO ledger and the episodic log, strands the branch/PR, and can kill
  `--loop`** *(found 2026-09-26; confirmed by reading the code — the
  triggering case is the already-documented L6 403)* — `run_cycle`
  (`sis/org.py`) has no exception handling around the role calls; the cost is
  known to the driver only via each method's return value, so a later raise
  (an under-scoped PAT's 403 at `open_pr`, a transient Jira 500 on a status
  transition, or a Serve error mid-canary) never reaches
  `CEO.report_outcome`/`record_neutral`. Re-running repeats the same
  untracked spend, so the hard cap never sees it, and `run_loop`
  (`sis/loop.py`) has no per-cycle exception handling either — the whole
  process exits. Fix: charge spend as soon as it's incurred (or recover it
  via `try/finally`); wrap each `run_cycle` stage so an exception becomes a
  recorded, breaker-counted `error` outcome — and, if a canary was live,
  retires it — instead of an unhandled exception.

- [OMNI-56] **M17 — A QA-stage rejection drops the gauntlet's reject reason** *(found
  2026-09-26; confirmed by reading the code)* — when QA's own re-run of the
  gauntlet rejects a candidate, the episodic record loses `reject_gate`, the
  `slo` failure-weight discount, and the OMNI-37 harness/candidate
  distinction that the SWE-stage rejection path already carries. Fix: thread
  the gauntlet `Result` through QA's rejection path the same way `SWE.
  implement`'s does.

- [OMNI-57] **M18 — A PR a human closes without merging holds the canary — and, under
  `loop.serve(watch_merges=True)`, the whole loop — open indefinitely**
  *(found 2026-09-26 from two angles, `sis/loop.py` and `sis/roles.py`;
  confirmed by reading the code)* — `observe_merge` and the poll loop only
  check for a merge; there is no "closed, not merged" terminal state, so a
  human declining a change leaves green attached and the next cycle blocked
  forever rather than resuming. Fix: poll the PR's actual state, not just
  mergedness, and retire the canary + resume the loop on `closed`.

- [OMNI-49] **M19 — On the AWS run box, a `--canary serve` green replica can reach IMDS
  instance-role credentials and read other processes' environment via
  `/proc`** *(found 2026-09-26 from two angles; confirmed by reading the
  infra config, not run on a live EC2 instance)* — `build_candidate`
  (`sis/serving.py`) only blanks `runtime_env.env_vars`; the replica is an
  ordinary host process, so IMDSv2 (whose hop-limit-1 default only stops
  containers behind a bridge, not host processes) and
  `/proc/<raylet-or-driver-pid>/environ` (readable by any same-uid process)
  are both open to it, and the instance role can call
  `secretsmanager:GetSecretValue` on the one secret holding the Atlassian,
  GitHub and Anthropic tokens (`infra/aws/main.tf`). `docs/AWS_RUN.md`
  currently states the opposite — that the gauntlet's `--network none` is
  what stands between candidate code and IMDS — which is true for the
  gauntlet but not for the Serve canary. Fix: land alongside H3's isolation
  redesign; until then, refuse `canary.backend=serve` when `SIS_ENV=aws` or
  the proposer isn't the stub, and correct the doc.

- [OMNI-50] **M20 — The live canary's p95/p99 gate is close to a coin flip for targets
  where dispatch overhead dominates compute** *(found 2026-09-26; simulated
  through the real `evaluate_canary`)* — Gate 4 compares nearest-rank p95/p99
  of two **unpaired** marginal latency arrays (~150 samples) at
  `max_latency_ratio=1.0` with no allowance on p95 (only p99 got the #107
  10% allowance). `tests/test_live_canary.py` already documents "there is no
  candidate that reliably passes here." Simulated (lognormal σ=0.35, n=150,
  2000 trials) through `evaluate_canary` itself: an equal-speed candidate is
  rejected **~56%** of the time (mostly on p95); a candidate ~3% slower still
  passes **~44%** of the time. Every false reject counts in full toward the
  circuit breaker and files a bug. Fix: store paired `(blue, green)`
  latencies per shadow sample (the router already has both in hand; it
  throws the pairing away when it records each side separately) and decide
  the way `gauntlet.benchmark_decision` does — a paired-bootstrap accept /
  reject / inconclusive, inconclusive neutral.

- [OMNI-50] **M21 — The live canary never judges error rate, and SHADOW mode drops any
  pair where green failed** *(found 2026-09-26 from three independent
  reviews; simulated through the real `evaluate_canary`)* — `_canary_live`
  (`sis/roles.py`) discards both error counts that `live_window` returns;
  `evaluate_canary` (`sis/canary.py`) has no error-rate parameter at all; and
  `CanaryRouter.route` only records a `LiveSample` when *both* sides
  succeeded, so a request where green raised never reaches the agreement
  gate (survivorship bias) — yet green's failed calls still land in its
  latency array, and because failures return fast, they pull green's
  percentiles *down*. Simulated: 105 paired samples plus 45 fast green
  errors (dropped from pairing, counted in latency) still returns
  `passed=True`. The evidence floor (`min(100, 150)=100`) leaves room for up
  to a third of live traffic to fail on green and still certify it. Fix:
  pass both error counts into `evaluate_canary` and reject when green's error
  rate exceeds blue's; in SHADOW, record a blue-ok/green-error pair as a
  disagreement rather than dropping it.

- [OMNI-58] **M22 — The Serve baseline (blue) is the local target file, not the merged
  base, and reverts to it whenever `ServeCloud` is rebuilt** *(found
  2026-09-26; confirmed by reading the code)* — unlike the offline gauntlet
  (fixed under H1 to benchmark against `live_target_source()`), the Serve
  canary's blue deployment is seeded from the local `runtime/*.py` file, so a
  rebuilt `ServeCloud` (e.g. after a DevOps restart) can silently regress
  blue to stale code. Fix: seed blue from `live_target_source()` too.

- [OMNI-59] **M23 — The test suite inherits real `SIS_*` environment variables from the
  developer's shell** *(found 2026-09-26; confirmed by reading
  `tests/conftest.py`)* — nothing clears `SIS_*`/adapter-credential
  variables before the suite runs, so a shell configured for a real run
  (real adapters, the real Claude proposer, or the real episodic store)
  silently changes what `pytest` exercises, with no visible signal that it
  happened. Fix: snapshot and clear the relevant env vars in a session-scoped
  autouse fixture, with an explicit opt-in fixture for the handful of tests
  that intentionally want a real backend.

## Low

- [OMNI-64] **L15** — `validate()` can be made to raise instead of returning a
  `Result` (a gate script crash that escapes the harness's own exception
  handling), losing the episodic record, the breaker increment, and the
  accounted spend for that cycle. Fix: wrap `validate()`'s gate dispatch in a
  catch-all that returns a `harness:` `Result` instead of propagating.
- [OMNI-65] **L16** — The OMNI-37 sandbox self-check (`probe_sandbox`) loads
  `sitecustomize.py` from the same writable directory a failing candidate can
  also write to (see M9), so a candidate that wants its own rejection blamed
  on the harness can corrupt the probe too. Fix: give the probe its own
  read-only directory, not the candidate's.
- [OMNI-66] **L17** — `NaN` passes `config.py`'s number validation and silently
  disables the spend cap and cost-per-accepted SLO (a threshold compared
  against `NaN` is never true). Fix: reject non-finite floats in
  `parse_value`.
- [OMNI-67] **L18** — `policy.classify()` is case-sensitive, so on a case-insensitive
  filesystem a different-case path to guardrail code classifies as STRICT
  rather than FORBIDDEN. Fix: normalise case via the resolved path.
- [OMNI-68] **L19** — `contract_author.promote()` copies whatever is currently in the
  loop-writable staging directory at promotion time, not necessarily the
  content a human reviewed if the draft was rewritten in between. Fix: hash
  the draft at review time and refuse `promote()` on a mismatch.
- [OMNI-69] **L20** — The operator audit log (`runtime/operator_audit.jsonl`) path
  depends on the process's working directory, and entries don't record which
  operator made the edit. Fix: resolve the path against a fixed root; add the
  OAuth-authenticated login to each record.
- [OMNI-61] **L21** — CEO brake-state persistence fails open: a corrupt, unwritable, or
  newly-switched state store silently resets `spent=0` and clears the
  breaker trip rather than refusing to start. Fix: an unparseable-but-present
  state file should trip the breaker with a `state_unreadable` reason, not
  reset it.
- [OMNI-70] **L22** — PRs from QA-rejected, QA-inconclusive and canary-rejected cycles
  stay open with nothing tracking them; merging one later promotes nothing
  but leaves a merged-looking PR with no effect. Fix: close (not merge) the
  PR as part of recording the rejected outcome.
- [OMNI-61] **L23** — `CEO.reset_breaker()` has no caller anywhere outside tests; in
  practice the only reset is deleting the state file, which also zeroes
  spend. Fix: expose it through an admin entry point that resets the trip
  without touching spend.
- [OMNI-62] **L24** — Budget exhaustion stops `--loop` silently; `loop.decide()`'s own
  comment says a human is paged, but nothing files anything. Fix: route it
  through the same alerting path as a breaker trip.
- [OMNI-71] **L25** — Unknown/unpriced Anthropic model ids are silently billed at
  `claude-opus-4-8` rates in `cost.py`, which can undercount a pricier
  model's actual spend against the hard cap. Fix: fail loudly (or price at
  the most expensive known tier) on an unrecognised model id.
- [OMNI-72] **L26** — A live-canary rejection files two bugs for the same event. Fix:
  file one.
- [OMNI-73] **L27** — `episodic.gate_from_reason` matches `"timed out"`/`"timeout"`
  anywhere in the reject reason, including text a candidate itself printed,
  so a counted correctness failure can be mislabelled as an infrastructure
  timeout. Fix: match only the harness's own timeout sentinel.
- [OMNI-74] **L28** — Candidate return values are unpickled by value inside the
  (unscrubbed) router and DevOps processes during a live canary — a second,
  more roundabout way for candidate code to run outside any sandbox, lower
  severity than H3/M19 because it needs a return type whose deserialisation
  itself runs code. Fix: deserialise live-canary responses into a
  restricted, data-only representation.
- [OMNI-75] **L29** — A live canary has no per-call timeout, and SHADOW mode awaits
  green fully before answering the caller, so a slow or hung candidate stalls
  every live client and can wedge DevOps. Fix: bound the green call with its
  own timeout, independent of the client's.
- [OMNI-55] **L30** — An exception after the green deploy during a live canary leaves
  green attached and the PR pending with no verdict, bug, or spend recorded
  — the live-path sibling of M16. Fix: the same accounting fix as M16,
  applied to `_canary_live`.
- [OMNI-76] **L31** — Promotion serves the source snapshotted at canary time, not
  necessarily what a human actually merged if the PR was amended after the
  canary started. Fix: re-fetch the merged source at `observe_merge` time and
  compare shas before promoting.
- [OMNI-77] **L32** — Canary backend routing silently falls back to the legacy
  in-memory path when `retire_canary` is called without a `pr_id`, or after a
  DevOps restart — leaving Serve's green attached, or "promoting" only in
  bookkeeping with nothing changing online. Fix: make the fallback loud.
- [OMNI-78] **L33** — `AnthropicClient.complete` never checks `stop_reason`; output
  truncated by `max_tokens` (8000, shared with adaptive thinking at
  `effort=high`) is silently treated as complete, and any resulting gate
  failure is blamed on the candidate. Fix: check `stop_reason`; retry or fail
  loudly on `max_tokens`.
- [OMNI-79] **L34** — The real GitHub adapter's `_get_file` treats any error
  (including a transient 5xx) the same as "file absent" and silently falls
  back to the stale local baseline. Fix: distinguish 404 from other errors;
  let a real error retry or fail the cycle loudly.
- [OMNI-80] **L35** — `ConfluenceDocumentStore.create_page` overwrites any existing
  page with the same title, including a human-authored one, without
  approval. Fix: require the destructive-Confluence-action approval gate
  here too.
- [OMNI-81] **L36** — `scripts/aws_secret.py`'s routing guard checks the Jira project
  and GitHub repo but not the Confluence space, which defaults to the real
  `SD` space. Fix: add the same allowlist check for the Confluence space key.
- [OMNI-82] **L37** — `scripts/check_connections.py` doesn't preflight the Anthropic
  API key/model; a bad `SIS_LLM_MODEL` or key only fails inside
  `SWE.implement`, after Jira/Confluence artifacts already exist for the
  cycle. Fix: add an Anthropic check to `--deep`.
- [OMNI-83] **L38** — `Dockerfile.gauntlet` installs `mypy`/`pytest`/`hypothesis`
  unpinned, so the docker sandbox's gate toolchain can drift from
  `poetry.lock` (and thus from what CI and the subprocess sandbox actually
  run) whenever the image is rebuilt. Fix: pin from an exported, hash-locked
  requirements file; check the pin at `ensure_sandbox_ready()` time.
- [OMNI-42] **L39** — `tests/test_contract_author.py::
  test_a_drafted_skeleton_stages_without_touching_specs` (OMNI-42) has a
  second, independent cause beyond the `specs/__pycache__` race already
  ticketed: another test in the suite writes a real directory into `specs/`
  during the run. Fix: locate and fix that test alongside OMNI-42.
- [OMNI-84] **L40** — CI never installs the `analytics` (duckdb) or `ui` (panel)
  dependency groups, so the tests behind those groups are silently skipped
  rather than run and reported. Fix: install both groups in CI.
- [OMNI-85] **L41** — `commit-lint` exempts any commit whose subject starts with
  `fixup!`/`squash!` from needing an OMNI key or `No-Ticket:` trailer, on the
  assumption they're rebased away before merge — nothing enforces that
  assumption, so a `--no-verify` commit titled `fixup! ...` can reach
  `develop` with neither. Fix: fail on a `fixup!`/`squash!` subject reaching
  CI instead of exempting it.
- [OMNI-86] **L42** — The SLO gate (`sis/slo.py`) replays a fixed workload the same way
  the pre-OMNI-41 benchmark did, and has the same memoisation hole (noted in
  the OMNI-41 Resolved entry below, previously without an ID). No shipped
  contract declares an SLO yet, so this is latent. Fix: apply the OMNI-41
  fresh-input design here too before any contract ships an SLO.
- [OMNI-87] **L43** — CLAUDE.md's Hard Rules describe "cost/brakes" as one FORBIDDEN
  unit, but only `sis/cost.py` (spend accounting) actually is; the
  breaker/threshold decision logic (`evaluate_brakes`, `failure_weight`,
  `CEO.report_outcome`) lives in `sis/roles.py`, which is STRICT — a test
  pins that classification deliberately. A STRICT change (human-approved)
  can therefore weaken the circuit breaker without touching anything the doc
  calls FORBIDDEN. Fix: word CLAUDE.md precisely, and consider extracting the
  pure brake-decision functions into their own FORBIDDEN module so
  safety-critical logic doesn't share a STRICT file with ordinary actor code.

## Resolved (Low)

- [OMNI-1] **L5 — The gauntlet is hardwired to `sum_of_divisors`.** **RESOLVED
  2026-08-06** (OMNI-1: OMNI-4/5/6/7). Nothing in `sis/` knows any target by
  name. Four changes, in order:
  - **Policy ([OMNI-4]).** `GUARDRAIL_DIRS` guards whole trees by path segment, so
    `specs/` is FORBIDDEN — the implementer cannot edit its own exam. `_rel()`
    now resolves against the project root, without which a traversal path
    escaped the guard from any other working directory.
  - **Contract ([OMNI-5]).** `sis/contract.py` carries the entry point, paths,
    margin and trial count; `specs/<name>/oracle.py` carries the reference,
    benchmark inputs and input generator as a **module the gauntlet copies into
    the sandbox**, rather than literals interpolated into its bench script. New
    interface gate; SelfModel is the contract registry.
  - **Proposer ([OMNI-6]).** The system prompt named a target
    (`"preserve sum_of_divisors(n: int) -> int"`), so any other target would
    have had the LLM instructed to write the *wrong function* — L5 was a
    gauntlet **and** proposer problem. The prompt is now contract-derived
    (signature and reference via `inspect` on the oracle; required API via the
    acceptance tests verbatim) and names no target.
  - **Second target ([OMNI-7]).** `runtime/sort_target.py` + `specs/sort/`. Both
    targets run the full loop end-to-end on the same unmodified engine:
    sort `0.000880s → 0.000173s`, sum-of-divisors `0.000249s → 0.000002s`.
    A third target is a new `specs/` directory and a registry entry, not an
    engine change.

  Two things worth carrying forward, both found by testing rather than reasoning:

  - **Anti-gaming is only as good as the input distribution.** A probe candidate
    reading `return v if len(v) > 500 else sorted(v)` — silently unsorted on
    large inputs — passed *every* gate, because the sort oracle's
    `random_input` drew only 60–120 elements and the broken branch was never
    reached. The oracle now spans tiny/medium/large lengths. Any new contract
    must ask what its distribution fails to cover.
  - **Env vars don't reach role actors.** Contract selection was first built as
    `SIS_CONTRACT`, and the acceptance test passed *while silently running
    `sum_of_divisors`*: detached Ray actors are separate processes that inherit
    the driver's environment when **created**, so anything exported after
    `bootstrap()` (including `monkeypatch.setenv`) is invisible to them.
    Selection is now an explicit `run_cycle(contract_name=...)` argument
    threaded to both SWE and QA; `SIS_CONTRACT`/`--contract` still work when set
    before launch. Any future per-cycle configuration has the same trap.

  The noise-floor field evidence below stands and is **not** addressed by L5 —
  it is the argument for the live-traffic canary (OMNI-2), which replaces a
  fixed-input microbenchmark with real percentiles. Original report follows.

  The independent
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
  - ~~**`Cloud` Protocol break.**~~ **Fixed in the doc.** ([OMNI-9]) `shift_traffic`/
    `live_metrics` would've broken the `@runtime_checkable Cloud` Protocol for
    `InMemoryCloud` (`sis/adapters.py:165`) *and* `RealCloud`
    (`sis/adapters_real.py:459`), not just the one the doc called out. Sequencing
    step 7 and the `ServeCloud` section now say both adapters need a stub in the
    same step.
  - ~~**`specs/` isn't actually FORBIDDEN yet.**~~ **Fixed in the doc** ([OMNI-4]), and a
    sharper gap than first written: `classify()` matches `GUARDRAIL_PATHS` by
    *exact* string equality, not directory prefix, so even adding a bare
    `"specs/"` entry would silently protect nothing. `CLASS2_CONTRACT.md` now
    states this as two required changes for L5 Layer 1 — add the contract paths
    *and* teach `classify()` directory-prefix matching (or enumerate contract
    modules individually) — instead of claiming present-tense enforcement.
  - ~~**`DevOps.canary()`'s signature doesn't stretch to this design.**~~ **Fixed
    in the doc.** ([OMNI-14]) Sequencing step 10 now says "rework," not "wire," and spells out
    that today's one-scalar `canary(pr_id, candidate_latency)` needs a `Contract` +
    live samples + latency arrays instead, plus a PR/target→`Contract` lookup that
    doesn't exist yet in `Workspace`/`SelfModel`.
  - ~~**`CanaryVerdict` field mismatch.**~~ **Fixed in the doc** ([OMNI-8]) — the dataclass
    sketch now uses `baseline_p95`/`candidate_p95` (matching the gate 2 prose)
    instead of `p50`.
  - ~~**No stated concurrency rule once `serve_breach()` lands (step 11).**~~
    **Fixed in the doc.** ([OMNI-10]) New rule: the impure wrapper around `serve_breach()`
    (`loop.serve()`/`run_loop()`, not the pure function itself) checks
    `SelfModel`'s existing green-slot state before calling `propose()` again — a
    canary already in flight holds the next cycle rather than starting one
    concurrently. Reuses existing slot-tracking state; no new field.
  - ~~**Doesn't reconcile with the "atomic actor swap" path.**~~ **Fixed in the
    doc.** ([OMNI-2]) New "Scope" section: this doc covers only the Ray-Serve/HTTP-fronted
    half of `DESIGN.md` §4; the shadow-run-then-atomic-handle-swap path for
    internal, never-served actors is out of scope here, not superseded, and has
    no design doc yet — `evaluate_canary()`'s two gates are reusable for it,
    only the traffic-splitting/promote mechanics differ. A decision rule picks
    the mechanism per target (Serve/HTTP → this doc; actor-to-actor only → the
    not-yet-written atomic-swap doc).

**Minor (noted in the 2026-07-28 review):** all six fixed 2026-08-05 — see
the **Minor batch** entry under Resolved ([OMNI-112]–[OMNI-117]).

## Sequencing for the first real-life test

The first real-life test = **real Claude proposer + real adapters + docker
sandbox** on the scratch tenant — the first run where untrusted generated code
and real money meet.

> **✅ Passed (2026-07-28).** All five steps below are done. Real Claude
> proposer (`claude-opus-4-8`) + real adapters + kernel-enforced docker
> sandbox, on the scratch tenant. Cycle `cb4f6fe13ed7`: Claude proposed the
> O(√n) `isqrt` form against the naive O(n) baseline (re-seeded on
> `testrun/main` for the demo), the full gauntlet passed **inside the docker
> sandbox** (192.4µs → 1.6µs, 99.2% faster), and it filed real artifacts —
> Confluence spec `6356994`, Jira `TES-20`, GitHub `testrun` PR #4 — then
> stopped at `verified_awaiting_human_merge`. Episodic-logged cost
> **$0.014375**, reconciled against the Anthropic console. Every stage of the
> loop fired end-to-end against live systems with real money for the first
> time.

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

- [OMNI-88] **M7 — The benchmark's false-accept rate at the margin is a few times the
  nominal** *(found 2026-09-26 by the statistics-lens review of the OMNI-41
  gate, like H2; verified by simulation)* —
  `benchmark_decision` bootstraps a ratio of raw wall-clock sums. Scheduler
  stalls are not symmetric noise: a stall landing in a few pairs moves the sum,
  and a percentile bootstrap over ~109 heavy-tailed pairs undercovers, so a
  candidate sitting exactly at the margin is accepted ~3–4x more often than the
  nominal 2.5%. The human PR merge still follows every accept. Fix direction:
  read process CPU time alongside `perf_counter` per timing and re-draw a pair
  whose wall-minus-CPU gap marks a stall (keep the verdict on wall time, since
  `thread_time` alone is gameable by offloading work), or use a trimmed /
  Winsorized ratio. Natural to do together with H2, which rebuilds the
  measurement anyway.
  **Disposition (2026-09-26):** not fixed for now. The harm is bounded — a
  near-margin candidate is still correct and genuinely faster, and it still
  goes through the canary and human review — and the obvious fix (trimming
  the ratio) would reopen the size-conditional gaming hole OMNI-41 closed.
  Re-measure once [OMNI-45] rebuilds the measurement, before deciding to fix.

- [OMNI-89] **L6 — Preflight doesn't verify the PAT's Pull-requests scope.** Not fixable
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

- [OMNI-41] **The benchmark gate measured a cache, not an algorithm — a memoised naive
  candidate passed every gate** *(found and fixed 2026-09-26 under OMNI-41,
  while chasing what looked like a flaky test)* — the Class-1 benchmark timed
  five repetitions over the same fixed `oracle.BENCH_INPUTS` and kept the best.
  A candidate that is the naive O(n) `sum_of_divisors` under
  `@functools.cache` is correct, fully typed, agrees with the reference on
  every random differential trial, and after the first repetition is all cache
  hits: it measured **25ns** and passed, 3 of 3 runs, with no speedup at all.
  The same gate also decided on a single block-vs-block comparison with no
  notion of confidence, which is why `test_correct_but_not_faster_is_rejected`
  flaked under `-n auto`. Fix: candidate and baseline are timed back-to-back on
  the same **fresh** seeded input (never reused; 99 pairs, plus each
  `BENCH_INPUTS` entry once for shape coverage), and
  `gauntlet.benchmark_decision` decides on **total cost** with a
  paired-bootstrap interval — accept / reject / **inconclusive**, the last
  neutral at both SWE and QA stage (`episodic.neutral_status`) and reachable
  only by a candidate whose estimate clears the margin. The harness moves the
  verdict off the candidate's stdout — which stops its prints, not a candidate
  that reaches into the harness on purpose (**H2**, still open). **The first cut of this fix was itself broken by an
  adversarial pre-merge review** and reworked before merge: deciding on the
  *median* per-input ratio accepted a candidate fast on the typical 70% of
  inputs and 4x slower on the rest (~2x slower in total, accepted ~98% of
  seeds); a candidate could steer the neutral *inconclusive*; one `atexit`
  print forged the verdict through a last-line-wins parser; and a
  candidate-caused missing output self-labelled as `harness:`, skipping the
  OMNI-37 probe. The memoised candidate now measures its real ~190µs.
  Regression tests: `test_memoised_naive_impl_cannot_game_a_replayed_workload`,
  `test_fast_on_typical_inputs_but_slower_in_total_is_rejected`,
  `test_a_candidate_cannot_forge_the_benchmark_verdict_from_stdout`,
  `tests/test_benchmark_decision.py`, and the inconclusive param of
  `tests/test_org_no_change.py`. Lessons: **a fixed benchmark workload is a
  gaming surface just like a fixed test set** — the L5 lesson ("anti-gaming is
  only as strong as the input distribution") applies to timing, not just
  correctness; and **a larger timing window makes pairing worse**, because
  shared drift cancels only as far as the two halves of a pair are adjacent in
  time. Still open, both pre-existing rather than introduced here: **(1)** the
  candidate runs in the same process as the timing harness, so it can in
  principle tamper with the measurement (monkeypatch `time`, read the seed
  from `sys.orig_argv` or clone the harness's RNG to precompute upcoming
  inputs, burn the GIL from a thread during baseline windows) — the sandbox
  contains it from the *host*, not from the measurement; the durable fix is
  feeding inputs from the parent one at a time. **(2)** the SLO gate
  (`sis/slo.py`) still replays a fixed workload and has the same memoisation
  hole; no shipped contract declares an SLO yet, so it is latent. (Point (1)
  is **H2**; point (2) is now **L42**.)

- [OMNI-37] **The docker sandbox could not read its own temp dir on native Linux — so
  it blamed every candidate** *(found and fixed 2026-09-23, rehearsing the
  OMNI-29 box on a local Ubuntu 24.04 container)* — each validation's temp dir
  comes from `tempfile`, so it is `0700` and owned by the host user, and the
  container ran as `Dockerfile.gauntlet`'s `sandbox` user (uid 10001). On
  native Linux that uid cannot open the candidate: `Permission denied`, and the
  mypy gate — the first to execute in the sandbox — reported **`mypy --strict
  failed`**, attributing a harness fault to the candidate. On the EC2 box every
  real Claude proposal would have been rejected as badly typed, billed, filed
  as a `TES` bug, and tripped the breaker after three cycles, all looking like
  the model's fault. Every earlier docker run — including the 2026-07-28
  first real-life test — was on Docker Desktop, whose file sharing ignores
  ownership, so it never showed. Fix: the container runs as the host user's
  `uid:gid` (`sis.gauntlet._container_user`), and refuses root, where that
  mapping would make generated code root inside the container. Lessons: **a
  sandbox verified only on Docker Desktop has not been verified on Linux** —
  `scripts/rehearse_aws_run.sh` now rehearses on a real Linux daemon; and a
  gate that cannot tell "the harness failed" from "the candidate failed" will
  report the harness's faults as the candidate's (a docker-daemon error on
  the mypy gate still reads as a type error — a follow-up worth doing).

- [OMNI-110] **The AWS box could not start the operator console** *(found and fixed
  2026-09-23, same rehearsal)* — `docs/AWS_RUN.md` starts `sis.frontend` on
  the box, but `scripts/aws_bootstrap.sh` installed only `--with real --with
  llm`, so it died with `ModuleNotFoundError: panel`. The bootstrap now
  installs `--with ui` too.

- [OMNI-14] **Serve replica CPU reservation deadlocked CI — `serve.run()` blocked
  forever on a constrained runner** *(found and fixed 2026-08-09, during
  OMNI-14's first CI run, which hung for 2h+ inside pytest)* — Ray Serve
  reserves **1 whole CPU per replica at scheduling time** by default, the same
  default the org's nine detached role actors already consume. On a
  workstation with cores to spare the competition is invisible; on a CI
  runner (~4 vCPUs) the green canary replica could never be scheduled, and
  `serve.run()` waits for it **with no timeout** — not a failure, a silent
  hang. Reproduced deterministically under `ray.init(num_cpus=2)`: blue
  deployed fine, adding green hung until killed. Fix: `num_cpus=0` on every
  Serve replica (`_LIGHTWEIGHT_REPLICA` in `sis/serving.py`) — they are I/O-
  and GIL-bound and need no reserved core. Lessons: **a Ray deployment that
  works locally can deadlock, not just slow down, on a smaller machine — test
  scheduling assumptions under `ray.init(num_cpus=2)` before CI does it for
  you**; and anything that blocks on cluster scheduling needs a timeout,
  because "waiting for resources that will never come" is indistinguishable
  from progress.

- [OMNI-14] **The L5 noise floor, third appearance: Serve dispatch overhead swamps the
  target's own compute** *(2026-08-09, OMNI-14 tests)* — two `test_live_canary`
  tests asserted a live canary must *pass*, picking candidates with "real"
  offline margin (a merge sort; then O(√n) vs O(n) `sum_of_divisors`). Both
  flaked: at these input sizes the function's compute (µs) is dwarfed by Ray
  Serve's per-request dispatch (tens of ms), so even a genuine 100x algorithmic
  win doesn't reliably clear `evaluate_canary`'s p95 gate — the live analogue
  of the offline benchmark jitter L5 already documented. Fixed by removing the
  pass/fail assertion from tests whose actual subject (routing, caching) holds
  regardless of verdict. Lesson: **a live-canary verdict is only meaningful
  when per-request work dominates transport overhead** — which is exactly why
  the sort (freely scalable input size) was chosen as the served target, and
  why `sum_of_divisors(10_000)` makes a fine gauntlet target but a poor served
  one.

- [OMNI-111] **Flaky Serve tests: per-test app churn blew `serve.delete`'s own timeout**
  *(found and fixed 2026-08-09, PR #78; shipped briefly via #77's merge)* —
  `tests/test_serve_cloud.py` stood a Serve application up and tore it down
  around *each* test (~20 `serve.run`/`serve.delete` pairs); under that churn
  `serve.delete` intermittently exceeded its internal 60s timeout, failing a
  **different test each run** whenever another Serve module ran first — so a
  green run proved nothing. Verified the adapter itself was sound before
  touching the tests (a second, identically-configured `ServeCloud` gets a
  genuinely fresh router — no stale state), then restructured to one
  module-scoped deployment mutated through the control plane, which is also how
  a real canary is driven. General lesson, same family as the noise-floor
  items: **a test that passes intermittently for environmental reasons is worse
  than a failing one, because it converts luck into confidence.** Serve
  app create/delete is expensive and control-plane mutation is cheap — prefer
  the latter inside a test module.

- **Minor batch** *(2026-08-05)* The six unnumbered items from the 2026-07-28
  review, each with a regression test that fails without its fix:
  - [OMNI-112] **`candidate_sha` dropped on the policy-block path** — `SWE.implement`'s
    gauntlet-fail and success returns carried it, the policy-block return did
    not, so a policy-blocked cycle was logged with `candidate_sha=None`: the one
    field tying that episode to the exact diff, missing from precisely the
    rejection you most want to audit. Covered structurally by
    `tests/test_roles_contract.py` (every exit path must carry the key —
    reaching that branch for real needs a cluster plus a mispointed target).
  - [OMNI-113] **A missing `tests/test_target.py` was blamed on the candidate.** The gate
    fell through to `pytest <a directory that was never created>`, which exits
    non-zero and surfaced as `"pytest failed"` — a valid candidate rejected with
    a reason pointing at the wrong side of the fence. Now fails closed (an unrun
    correctness gate is not a pass) with a `harness:` reason naming the real
    cause, and `gate_from_reason()` maps it to a new `harness` gate *before* the
    gate-name substring checks — otherwise a broken harness reads in the
    analytics as "candidates keep failing pytest".
  - [OMNI-114] **Settings re-read per call.** `space_keys()`/`version_control_base()` are
    called several times per cycle from inside the Ray actors, and each call
    re-read and re-parsed the whole secrets source — a redundant file read
    locally, a redundant **Secrets Manager round-trip** under `SIS_ENV=aws`. New
    `settings.cached_settings()` (+ `reset_settings_cache()` for tests) caches
    the **no-argument** path only; `load_settings(source)` still reads every
    time, so explicit-source callers are unaffected.
  - [OMNI-115] **Jira `children()` built JQL by f-string.** `/search/jql` takes JQL as a
    string with no parameter binding. Only internal keys reach it today — but
    that is a property of the callers, not an enforced one, and Confluence
    intake exists to let outside text into the org. `parent_id` is now validated
    against Jira's key grammar at the boundary, before any request goes out.
  - [OMNI-116] **`transition()` was undone by a failed comment.** The comment POST was
    chained onto the transition with `raise_for_status()`, so a 500 on the
    *comment* raised after the transition had already been applied and could not
    be rolled back. The caller saw the whole transition fail and retried, but
    the issue had moved — the retry found no matching transition and hard-failed
    the cycle. Now best-effort (mirroring `_apply_labels`), emitting
    `issue.comment_failed`: an audit note must not cost the state change it
    annotates.
  - [OMNI-117] **Duplicate gate numbering** in `gauntlet.py` — the no-op check and mypy
    were both commented "Gate 2". The no-op check is now "Gate 1b", keeping the
    rest aligned with `DESIGN.md` §5.
- [OMNI-92] [OMNI-93] **M2 + L9** *(2026-07-29)* Detached actors now share the `sis` Ray namespace and
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
  - [OMNI-104] **L10** — `policy.target_paths()` used `lstrip("./")` (strips `.`/`/`
    *characters*, mangling `.github/x` → `github/x`); now `removeprefix("./")`,
    matching the L4 fix in `_rel()`.
  - [OMNI-105] **L11** — a same-story retry 422'd at `open_pr` (L8's sibling); it now finds
    and reuses the existing open PR for the head (emits `pr.exists`).
  - [OMNI-106] **L12** — a timed-out gate was misreported as that gate's generic failure;
    `validate()` now detects returncode 124 and returns a timeout reason, and
    `gate_from_reason()` checks timeout first so `reject_gate="timeout"` is
    reachable.
  - [OMNI-107] **L13** — the soft-sandbox network guard now also blocks UDP
    (`sendto`/`sendmsg`) and DNS (`getaddrinfo`), not just TCP connect.
  - [OMNI-108] **L14** — `_put_file` now requires the path be **SOFT** (refusing STRICT
    engine code too, not only FORBIDDEN) — defence in depth at the write boundary.
- [OMNI-97] **M6** *(2026-07-28)* No HTTP timeouts on real-adapter calls — `requests`
  defaults to *no* timeout, so a wedged Confluence/Jira/GitHub API would hang a
  whole cycle with no breaker/bug/log. Fixed: `_session()` now returns a
  `_TimeoutHTTP` wrapper that applies a default `timeout` (30s, override
  `SIS_HTTP_TIMEOUT`; a bad value fails loudly) to every get/post/put, while an
  explicit per-call `timeout=` still wins. Covered by `tests/test_adapters_real.py`.
- [OMNI-96] **M5** *(2026-07-28)* The CEO budget/brakes had no config knob — the docs said
  "set a tiny budget for the first run" but the only path was editing source, so
  the L3 run used the hardcoded $5 cap. Fixed: `roles.ceo_config_from_env()` (a
  pure, unit-tested helper) reads `SIS_BUDGET_USD`, `SIS_BREAKER_THRESHOLD`,
  `SIS_MAX_COST_PER_ACCEPTED_USD`, `SIS_SLO_MIN_SPEND_USD` (defaults unchanged;
  an unparseable/negative value fails loudly), threaded through `org.bootstrap()`
  into the CEO. Documented in the env tables. (A detached CEO on a persistent
  cluster still ignores new args — tied to M2.)
- [OMNI-118] *(2026-07-25, PR #32)* Confluence duplicate-title 400 crashed re-runs →
  `create_page` updates the existing page in place.
- [OMNI-119] *(2026-07-25, PR #32)* Cross-space `parentId` 404 crashed the spec page →
  parent dropped, provenance in the SelfModel.
- [OMNI-120] *(2026-07-25, PR #31)* Cycles baselined on the stale local file for the
  *proposer input* → `live_target_source()` pulls the merged target. (The
  gauntlet-internal half of this was **H1**, fixed below.)
- [OMNI-90] **H1** *(2026-07-25)* `gauntlet.validate()` benchmarked against the local
  `runtime/target.py` instead of the cycle's baseline → after a merge a no-op
  candidate passed every gate. Fixed: `validate()` takes an explicit
  `baseline_source` (the merged target), passed by the SWE and QA; falls back
  to the local file only for direct callers/tests.
- [OMNI-94] **M3** *(2026-07-25)* No identical-source short-circuit. Fixed alongside H1:
  a candidate byte-identical to the baseline is rejected up front as
  `no change` (episodic `reject_gate="noop"`), before the µs-scale benchmark
  race. Local in-memory demo still promotes; the H1/M3 behaviour is covered by
  new regression tests in `tests/test_gauntlet.py`.
- [OMNI-109] **"No change" was treated as a failure** *(2026-07-25)* Once M3 lands, a
  cycle against an already-optimal target ended in `rolled_back` — filing a bug
  and counting toward the circuit breaker, so three "nothing to improve" cycles
  falsely paged a human. Fixed: `run_cycle` returns a benign `no_change` status
  (no bug, no breaker increment); the CEO's new `record_neutral` records spend
  (so the hard spend cap + cost-per-accepted SLO still apply) but leaves the
  failure/accept counters untouched. Covered by `tests/test_org_no_change.py`.
- [OMNI-91] **M1** *(2026-07-25)* Subprocess sandbox let untrusted LLM code read host
  files. Fixed: `gauntlet.ensure_sandbox_allows_proposer()` raises when a
  non-stub `SIS_PROPOSER` runs without `SIS_SANDBOX=docker` — enforced fail-fast
  in `run_cycle` (before any spend/artifacts) and as a backstop in `validate()`.
  Loud, explicit override `SIS_ALLOW_UNSANDBOXED_LLM=1`. The stub (trusted,
  hand-written candidate) still runs in the subprocess sandbox. Covered by
  `tests/test_gauntlet.py`.
- [OMNI-95] **M4** *(2026-07-25)* `SWE.implement` forked feature branches from a
  hardcoded `"main"` while `live_target_source` read `settings.default_base` —
  inconsistent on a repo whose default branch isn't `main`. Fixed: the new
  `settings.version_control_base()` (mirrors `space_keys()`) is the single
  source; the SWE forks from it. In-memory path still forks from `"main"`.
  Covered by `tests/test_settings.py`.
- [OMNI-100] **L3** *(2026-07-25)* Verified `cost.py`'s `PRICING` against published rates:
  `claude-opus-4-8` $5/$25 (the model the loop prices spend with),
  `claude-sonnet-4-6` $3/$15, `claude-haiku-4-5` $1/$5, cache 1.25×/0.1× — all
  correct. Added `claude-sonnet-5` ($3/$15 standard, the conservative choice
  over the intro rate). Guarded by `tests/test_cost.py`.
- [OMNI-99] **L2** *(2026-07-25)* The idempotent `create_page` fallback PUT a new version
  every run. Fixed: `_update_body` now fetches the stored body and skips the
  write (and version bump) when unchanged, emitting `page.unchanged`. Covered
  by `tests/test_adapters_real.py`.
- [OMNI-101] **L4** *(2026-07-25)* `policy._rel()` used `lstrip("./")`, which strips
  `.`/`/` characters rather than a `"./"` prefix (mangling `"../x"` → `"x"`).
  Fixed with `removeprefix("./")`. Covered by `tests/test_policy.py`.
- [OMNI-102] **L7** *(2026-07-25)* `measure_baseline()` fell back to `0.0` silently. Fixed:
  it now prints a warning to stderr (returncode + stderr) before returning the
  advisory `0.0`. Covered by `tests/test_gauntlet.py`.
- [OMNI-103] **L8** *(2026-07-25)* Re-running a cycle for an existing story 422'd on
  `create_branch`. Fixed: on "Reference already exists" the real GitHub adapter
  reuses the branch (emits `branch.exists`). Covered by
  `tests/test_adapters_real.py`.
- [OMNI-98] **L1** *(2026-07-25)* The labels the roles tag pages with (charter/spec/
  proposal/outline) were never written. Fixed (the write half): `create_page`
  now attaches them on both the create and update-in-place paths via the v1
  content-label endpoint (v2 has no label write), best-effort so a label
  failure never breaks a cycle (`page.labels_applied` / `page.labels_failed`).
  The `list_pages` `label` filter stays a no-op — v2 has no label filter and no
  caller uses it (documented in the adapter). Covered by
  `tests/test_adapters_real.py`.

<!-- Ticket link targets. -->
[OMNI-1]: https://olafzumpe.atlassian.net/browse/OMNI-1
[OMNI-2]: https://olafzumpe.atlassian.net/browse/OMNI-2
[OMNI-4]: https://olafzumpe.atlassian.net/browse/OMNI-4
[OMNI-5]: https://olafzumpe.atlassian.net/browse/OMNI-5
[OMNI-6]: https://olafzumpe.atlassian.net/browse/OMNI-6
[OMNI-7]: https://olafzumpe.atlassian.net/browse/OMNI-7
[OMNI-8]: https://olafzumpe.atlassian.net/browse/OMNI-8
[OMNI-9]: https://olafzumpe.atlassian.net/browse/OMNI-9
[OMNI-10]: https://olafzumpe.atlassian.net/browse/OMNI-10
[OMNI-14]: https://olafzumpe.atlassian.net/browse/OMNI-14
[OMNI-37]: https://olafzumpe.atlassian.net/browse/OMNI-37
[OMNI-41]: https://olafzumpe.atlassian.net/browse/OMNI-41
[OMNI-42]: https://olafzumpe.atlassian.net/browse/OMNI-42
[OMNI-45]: https://olafzumpe.atlassian.net/browse/OMNI-45
[OMNI-46]: https://olafzumpe.atlassian.net/browse/OMNI-46
[OMNI-47]: https://olafzumpe.atlassian.net/browse/OMNI-47
[OMNI-48]: https://olafzumpe.atlassian.net/browse/OMNI-48
[OMNI-49]: https://olafzumpe.atlassian.net/browse/OMNI-49
[OMNI-50]: https://olafzumpe.atlassian.net/browse/OMNI-50
[OMNI-51]: https://olafzumpe.atlassian.net/browse/OMNI-51
[OMNI-52]: https://olafzumpe.atlassian.net/browse/OMNI-52
[OMNI-53]: https://olafzumpe.atlassian.net/browse/OMNI-53
[OMNI-54]: https://olafzumpe.atlassian.net/browse/OMNI-54
[OMNI-55]: https://olafzumpe.atlassian.net/browse/OMNI-55
[OMNI-56]: https://olafzumpe.atlassian.net/browse/OMNI-56
[OMNI-57]: https://olafzumpe.atlassian.net/browse/OMNI-57
[OMNI-58]: https://olafzumpe.atlassian.net/browse/OMNI-58
[OMNI-59]: https://olafzumpe.atlassian.net/browse/OMNI-59
[OMNI-61]: https://olafzumpe.atlassian.net/browse/OMNI-61
[OMNI-62]: https://olafzumpe.atlassian.net/browse/OMNI-62
[OMNI-64]: https://olafzumpe.atlassian.net/browse/OMNI-64
[OMNI-65]: https://olafzumpe.atlassian.net/browse/OMNI-65
[OMNI-66]: https://olafzumpe.atlassian.net/browse/OMNI-66
[OMNI-67]: https://olafzumpe.atlassian.net/browse/OMNI-67
[OMNI-68]: https://olafzumpe.atlassian.net/browse/OMNI-68
[OMNI-69]: https://olafzumpe.atlassian.net/browse/OMNI-69
[OMNI-70]: https://olafzumpe.atlassian.net/browse/OMNI-70
[OMNI-71]: https://olafzumpe.atlassian.net/browse/OMNI-71
[OMNI-72]: https://olafzumpe.atlassian.net/browse/OMNI-72
[OMNI-73]: https://olafzumpe.atlassian.net/browse/OMNI-73
[OMNI-74]: https://olafzumpe.atlassian.net/browse/OMNI-74
[OMNI-75]: https://olafzumpe.atlassian.net/browse/OMNI-75
[OMNI-76]: https://olafzumpe.atlassian.net/browse/OMNI-76
[OMNI-77]: https://olafzumpe.atlassian.net/browse/OMNI-77
[OMNI-78]: https://olafzumpe.atlassian.net/browse/OMNI-78
[OMNI-79]: https://olafzumpe.atlassian.net/browse/OMNI-79
[OMNI-80]: https://olafzumpe.atlassian.net/browse/OMNI-80
[OMNI-81]: https://olafzumpe.atlassian.net/browse/OMNI-81
[OMNI-82]: https://olafzumpe.atlassian.net/browse/OMNI-82
[OMNI-83]: https://olafzumpe.atlassian.net/browse/OMNI-83
[OMNI-84]: https://olafzumpe.atlassian.net/browse/OMNI-84
[OMNI-85]: https://olafzumpe.atlassian.net/browse/OMNI-85
[OMNI-86]: https://olafzumpe.atlassian.net/browse/OMNI-86
[OMNI-87]: https://olafzumpe.atlassian.net/browse/OMNI-87
[OMNI-88]: https://olafzumpe.atlassian.net/browse/OMNI-88
[OMNI-89]: https://olafzumpe.atlassian.net/browse/OMNI-89
[OMNI-90]: https://olafzumpe.atlassian.net/browse/OMNI-90
[OMNI-91]: https://olafzumpe.atlassian.net/browse/OMNI-91
[OMNI-92]: https://olafzumpe.atlassian.net/browse/OMNI-92
[OMNI-93]: https://olafzumpe.atlassian.net/browse/OMNI-93
[OMNI-94]: https://olafzumpe.atlassian.net/browse/OMNI-94
[OMNI-95]: https://olafzumpe.atlassian.net/browse/OMNI-95
[OMNI-96]: https://olafzumpe.atlassian.net/browse/OMNI-96
[OMNI-97]: https://olafzumpe.atlassian.net/browse/OMNI-97
[OMNI-98]: https://olafzumpe.atlassian.net/browse/OMNI-98
[OMNI-99]: https://olafzumpe.atlassian.net/browse/OMNI-99
[OMNI-100]: https://olafzumpe.atlassian.net/browse/OMNI-100
[OMNI-101]: https://olafzumpe.atlassian.net/browse/OMNI-101
[OMNI-102]: https://olafzumpe.atlassian.net/browse/OMNI-102
[OMNI-103]: https://olafzumpe.atlassian.net/browse/OMNI-103
[OMNI-104]: https://olafzumpe.atlassian.net/browse/OMNI-104
[OMNI-105]: https://olafzumpe.atlassian.net/browse/OMNI-105
[OMNI-106]: https://olafzumpe.atlassian.net/browse/OMNI-106
[OMNI-107]: https://olafzumpe.atlassian.net/browse/OMNI-107
[OMNI-108]: https://olafzumpe.atlassian.net/browse/OMNI-108
[OMNI-109]: https://olafzumpe.atlassian.net/browse/OMNI-109
[OMNI-110]: https://olafzumpe.atlassian.net/browse/OMNI-110
[OMNI-111]: https://olafzumpe.atlassian.net/browse/OMNI-111
[OMNI-112]: https://olafzumpe.atlassian.net/browse/OMNI-112
[OMNI-113]: https://olafzumpe.atlassian.net/browse/OMNI-113
[OMNI-114]: https://olafzumpe.atlassian.net/browse/OMNI-114
[OMNI-115]: https://olafzumpe.atlassian.net/browse/OMNI-115
[OMNI-116]: https://olafzumpe.atlassian.net/browse/OMNI-116
[OMNI-117]: https://olafzumpe.atlassian.net/browse/OMNI-117
[OMNI-118]: https://olafzumpe.atlassian.net/browse/OMNI-118
[OMNI-119]: https://olafzumpe.atlassian.net/browse/OMNI-119
[OMNI-120]: https://olafzumpe.atlassian.net/browse/OMNI-120
