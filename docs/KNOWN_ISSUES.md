# Known issues & limitations

The canonical defect list. IDs (H/M/L) are stable: reference them in commits
and PRs. **Every entry starts with its Jira ticket**, open, won't-fix and
resolved alike; file the ticket first. `tests/test_known_issues.py` fails on an
entry without one, and on a ticket reference with no link target below.

When an issue is fixed, replace its entry with a one-line row under
"Resolved", add a CHANGELOG line, and leave the detail in the Jira ticket and
the PR. Entries resolved before 2026-11-10 were cut to one line then; their
full text is in git history (`git show 0a86c58:docs/KNOWN_ISSUES.md`).

## High

None open.

## Medium

None open.

## Low

- [OMNI-69] **L20** — The operator audit log (`runtime/operator_audit.jsonl`) path
  depends on the process's working directory, and entries don't record which
  operator made the edit. Fix: resolve the path against a fixed root; add the
  OAuth-authenticated login to each record.
- [OMNI-70] **L22** — PRs from QA-rejected, QA-inconclusive and canary-rejected cycles
  stay open with nothing tracking them; merging one later promotes nothing
  but leaves a merged-looking PR with no effect. Fix: close (not merge) the
  PR as part of recording the rejected outcome.
- [OMNI-72] **L26** — A live-canary rejection files two bugs for the same event. Fix:
  file one.
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
- [OMNI-76] **L31** — Promotion serves the source snapshotted at canary time, not
  necessarily what a human actually merged if the PR was amended after the
  canary started. Fix: re-fetch the merged source at `observe_merge` time and
  compare shas before promoting.
- [OMNI-77] **L32** — Canary backend routing silently falls back to the legacy
  in-memory path when `retire_canary` is called without a `pr_id`, or after a
  DevOps restart — leaving Serve's green attached, or "promoting" only in
  bookkeeping with nothing changing online. Fix: make the fallback loud.

- [OMNI-80] **L35** — `ConfluenceDocumentStore.create_page` overwrites any existing
  page with the same title, including a human-authored one, without
  approval. Fix: require the destructive-Confluence-action approval gate
  here too.
- [OMNI-81] **L36** — `scripts/aws_secret.py`'s routing guard checks the Jira project
  and GitHub repo but not the Confluence space, which defaults to the real
  `SD` space. Fix: add the same allowlist check for the Confluence space key.
- [OMNI-83] **L38** — `Dockerfile.gauntlet` installs `mypy`/`pytest`/`hypothesis`
  unpinned, so the docker sandbox's gate toolchain can drift from
  `poetry.lock` (and thus from what CI and the subprocess sandbox actually
  run) whenever the image is rebuilt. Fix: pin from an exported, hash-locked
  requirements file; check the pin at `ensure_sandbox_ready()` time.
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

- [OMNI-143] **L48** — The runbook's manual sync copies a `--loop` run a
  second time, under a folder named for the wrong time *(found 2026-09-29
  reviewing AWS run #5's artifacts)*. Since OMNI-140 the loop syncs itself to
  `runs/<start time>/`, yet the runbook still has the operator run
  `aws s3 sync runtime/ ".../runs/$(date +%Y%m%d-%H%M)/"` afterwards. Run #5
  left `runs/20260929-2230/` beside `-2218/` and `-2238/` beside `-2233/`,
  each identical but for one log line; `-2238` reads as a third run that never
  happened. Fix: keep the manual sync for a single `main.py` run only, or aim
  it at the folder the loop printed.
- [OMNI-144] **L49** — A run's S3 folder carries every earlier run on the box
  *(found 2026-09-29, same review)*. `sis/artifact_sync.py` uploads the
  episodic log, its state and `loop.log` whole, and all three accumulate across
  processes (carried spend is deliberate). Run #5's `sort` folder also holds
  the six `sum_of_divisors` cycles, and its state reports both runs' spend and
  acceptances. Nothing is lost, but reading a folder as one run double-counts.
  Fix: a per-folder `run.json` manifest (start, code version, contract,
  first/last `cycle_id`, spend at start and end), or upload this process's
  slice alongside the cumulative files.

- [OMNI-166] **L55** — Atomic writes leave the file readable by its owner
  only *(found 2026-10-25 by the weekly code review)*. `write_text_atomic`
  (`sis/atomic.py`) writes through `tempfile.mkstemp`, which creates mode 0600,
  and `os.replace` puts that file in place of the original. So the first
  operator edit through `sis.operator.save_edits`, or `python -m sis.config
  --write`, turns `config.yml` from 0644 into 0600, and every other file
  written this way ends the same. Git tracks only the executable bit and does
  not notice; a console or CI step under another uid then fails with an
  unrelated-looking `PermissionError`. Fix: keep the destination's mode
  (`0o666 & ~umask` for a new file), with a test that the mode survives a
  save; whether brake state should stay 0600 is decided per caller, not by
  `mkstemp`.
- [OMNI-172] **L57** — The scheduled-run principal can reach every run
  secret and launch any instance size *(found 2026-10-26 by the weekly code
  review)*. `infra/aws/scheduled_runner_policy.json` grants
  `ssm:SendCommand` with `AWS-RunShellScript` (root) on the tagged box, and
  `ec2:RunInstances` plus `iam:PassRole` for the instance role, which reads
  the run secret. So whoever holds the principal can start the box and read
  the Atlassian, GitHub and Anthropic credentials as root, although
  `scripts/aws_scheduled_run.py` never does and its docstring says the caller
  never handles them. `LaunchOnlyTheTaggedRunBox` also caps neither
  `ec2:InstanceType` nor the count; only the AWS Budget alarm would notice,
  afterwards. Fix: a custom SSM document that accepts only the runner's
  commands, an `ec2:InstanceType` condition matching `infra/aws`, and a check
  that `ec2:Owner` `"amazon"` cannot boot any Amazon-owned image.
- [OMNI-141] **L58** — `tests/test_adversarial.py::
  test_fast_on_typical_inputs_but_slower_in_total_is_rejected` once came out
  `benchmark inconclusive` instead of `no improvement` under `-n auto`
  *(seen 2026-09-29; passed 5/5 alone)*. Not a false accept, but inconclusive
  needs a point estimate that clears the margin, so under that load a
  candidate about 2x slower in total measured as looking faster. Find out
  whether oversubscription can move the total-cost ratio that far or the
  pairing broke, before deciding to accept inconclusive (as its sibling
  `test_memoised_naive_impl_cannot_game_a_replayed_workload` does) or to run
  it serially.
- [OMNI-174] **L59** — `tests/test_feature_cycles.py::
  test_a_feature_is_planned_once_and_carried_on_after_a_restart` failed once
  in a full `pytest -n auto` run *(seen 2026-10-26; the output was not
  kept)*. It did not fail again in three full parallel runs or six runs of its
  module with `-n 2`. Ordering and shared state are ruled out by reading the
  code: `feature.resumable_feature` picks deterministically, the in-memory VCS
  lists branches in insertion order, each xdist worker starts its own Ray
  cluster, and the suite runs with `episodic.store=none`. The suspect, not
  proven, is a sandboxed gate timing out under heavy load in the first cycle.
  Next time it fails, keep the assertion's `reason` before changing anything.
- [OMNI-181] **L66** — `scripts/aws_secret.py`'s `check_anthropic_key` ends
  in a traceback when the connection drops mid-response *(found 2026-10-31 by
  code review)*. It catches `URLError` and `TimeoutError`;
  `http.client.RemoteDisconnected` and `ConnectionResetError` escape. Nothing
  is uploaded either way. Fix: catch `OSError` and
  `http.client.HTTPException`.
- [OMNI-182] **L67** — An error in the proposal cut-off handler is blamed on
  the proposer *(found 2026-10-31 by code review; OMNI-175's follow-up)*.
  `SWE._implement` sets `paid["proposing"]` before `proposer.propose()` and
  clears it only on success, so if the Workspace call inside the
  `ProposalCutOff` branch raises, the error reads "in the proposer, while
  asking for the proposal". Only the label is wrong; cost and breaker count
  are kept. Fix: scope the flag to the `propose()` call itself.
- [OMNI-183] **L68** — Three small cleanups from OMNI-163 *(found 2026-10-31
  by code review; nothing wrong today)*: three sha256 helpers for one
  integrity check (`gauntlet.py` hashes inline beside its own `_digest`;
  `fixture_builder._digest`, `contract_author._sha256`); `last` stated in both
  `Question` and `Fixture` in `scripts/build_fixture.py`, with a placeholder
  `2020-01-06` that is always overwritten; and `private_backtests(root=)`,
  which no caller passes and the gate ignores, while its docstring says
  *index* is repo-relative and a test passes an absolute path.

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
  **Seen in CI (2026-10-08, PR #159):** on a loaded runner (`-n auto`) the gate
  accepted a candidate that is not at the margin. The naive sum written as a
  generator, which measures 1.06 to 1.09 times the baseline on a quiet
  machine, came out at 0.84 with its whole interval under 0.90. The same
  candidate under 18 busy processes on a Mac ranged from 0.92 to 1.21 over 60
  runs, with intervals about 0.22 wide, before and after OMNI-152 alike. So the
  harm is no longer limited to a near-margin candidate when the machine is
  heavily loaded. The AWS box runs one loop and little else (M28), and a human
  merge still follows every accept. The two tests whose candidates sat that
  close to the baseline for another reason (a forged verdict, a cache) now use
  a candidate half the baseline's speed; `test_correct_but_not_faster_is_rejected`
  keeps the near-baseline one, and is the test that fails when this happens.

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

## Resolved (Low)

- [OMNI-203] **L75** — the agent review's "no new I/O" rule was a denylist and missed `codecs`, `gzip`, `runpy` and reaching a builtin through `__builtins__`, a dunder attribute or `getattr`
- [OMNI-179] **L64** — `gauntlet._attribute` treated `"timed out"` anywhere in a reason as a timeout and skipped the sandbox probe
- [OMNI-186] **L69** — Four `tests/test_merge_observation.py` tests that run a real cycle came out `inconclusive` under `-n auto`
- [OMNI-180] **L65** — A backtest fixture that is not UTF-8 is reported as a generic gate error
- [OMNI-201] **L73** — `prediction_error.score` crashed on a predictor answer too large for a float
- [OMNI-185] **L72** — a PR merged while `--loop` ran left no trace in `loop.log` or the episodic log
- [OMNI-187] **L70** — `sis/fixture_builder.build` wrote a public fixture (`private=False`) from whatever series `question.inputs` named, and from every series in the trace…
- [OMNI-188] **L71** — `scripts/aws_scheduled_run.tail_commands` trusted `awk`'s `substr` and `tail -c` on a pipe to keep its output under the 24,000 characters SSM keeps
- [OMNI-177] **L62** — The AWS box never fetches `ca_price_wholesale`'s private fixtures
- [OMNI-176] **L61** — A malformed `private.json` stops the loop and every role actor, not just its contract
- [OMNI-178] **L63** — The default `ca_price_wholesale` build is refused on a trace captured after the release calendar ends
- [OMNI-73] **L27** — `episodic.gate_from_reason` matches `"timed out"`/`"timeout"` anywhere in the reject reason, including text a candidate itself printed, so a counted…
- [OMNI-82] **L37** — `scripts/check_connections.py` doesn't preflight the Anthropic API key/model; a bad `SIS_LLM_MODEL` or key only fails inside `SWE.implement`, after…
- [OMNI-127] **L46** — The proposer is not told which candidates were already rejected, so it resubmits identical code and is billed again
- [OMNI-145] **L50** — An inconclusive candidate counted toward "converged" and was never measured again
- [OMNI-65] **L16** — The OMNI-37 sandbox self-check (`probe_sandbox`) loaded `sitecustomize.py` from the same writable directory a failing candidate can also write to…
- [OMNI-173] **L60** — `tests/test_gauntlet.py:: test_docker_args_confine_to_a_cpu_only_when_asked` called `gauntlet._docker_args` without the `ordinary_user` fixture its…
- [OMNI-67] **L18** — `policy.classify()` was case-sensitive, so on a case-insensitive filesystem a different-case path to guardrail code (`SIS/policy.py`, `Specs/…`)…
- [OMNI-71] **L25** — Unknown/unpriced Anthropic model ids were silently billed at `claude-opus-4-8` rates in `cost.py`, which under-counts a pricier model's spend against…
- [OMNI-66] **L17** — `NaN` passed `config.py`'s number validation and silently disabled the spend cap and cost-per-accepted SLO (a threshold compared against `NaN` is…
- [OMNI-167] **L56** — "no real adapter may declare source `sim`" was documented on `SIMULATED_SOURCE` but not enforced: `SourceRules("sim", …)` constructed and `sanitise`…
- [OMNI-68] **L19** — `contract_author.promote()` copied whatever was in the loop-writable staging directory at promotion time, not necessarily the content a human…
- [OMNI-42] **L39** — `tests/test_contract_author.py:: test_a_drafted_skeleton_stages_without_touching_specs` had a second cause beyond the `specs/__pycache__` race: two…
- [OMNI-55] **L30** — An exception after the green deploy during a live canary leaves green attached and the PR pending with no verdict, bug, or spend recorded — the…
- [OMNI-79] **L34** — The real GitHub adapter's `_get_file` treats any error (including a transient 5xx) the same as "file absent" and silently falls back to the stale…
- [OMNI-148] **L51** — A Class-2 feature is proposed as "Speed up the <target> target"
- [OMNI-149] **L52** — A built feature's convergence check files a new plan and leaves its story In Progress
- [OMNI-150] **L53** — The episodic log names a model for cycles that made no model call
- [OMNI-64] **L15** — `validate()` can be made to raise instead of returning a `Result` (a gate script crash that escapes the harness's own exception handling), losing the…
- [OMNI-78] **L33** — `AnthropicClient.complete` never checks `stop_reason`; output truncated by `max_tokens` (8000, shared with adaptive thinking at `effort=high`) is…
- [OMNI-1] **L5 — The gauntlet is hardwired to `sum_of_divisors`.**

## Resolved

- [OMNI-200] **M36 — develop blue/green judged the PR's head file, not what `develop` serves after the merge**
- [OMNI-50] **M20 — The live canary's p95/p99 gate is close to a coin flip for targets where dispatch overhead dominates compute**
- [OMNI-50] **M21 — The live canary never judges error rate, and SHADOW mode drops any pair where green failed**
- [OMNI-48] **H3 — A Serve-canary candidate runs as a full Ray control-plane worker, not in any sandbox**
- [OMNI-53] **M13 — GitHub OAuth is never actually installed on the operator console; the server runs unauthenticated**
- [OMNI-171] **M35 — A scheduled AWS run lost track of its loop once the run's log passed 20 KB**
- [OMNI-165] **M33 — `contract_author promote` trusted the loop-writable manifest's name and could write outside `specs/<name>/`**
- [OMNI-59] **M23 — The test suite inherits real `SIS_*` environment variables from the developer's shell**
- [OMNI-58] **M22 — The Serve baseline (blue) is the local target file, not the merged base, and reverts to it whenever `ServeCloud` is rebuilt**
- [OMNI-170] **M34 — The prediction-error report could show a predictor the week it is scored on**
- [OMNI-164] **M32 — A fixture episode could show the model an earlier vintage of the week it must predict**
- [OMNI-52] **M12 — A `soft_` operator config edit can rewrite `forbidden_` keys through unescaped YAML rendering**
- [OMNI-54] **M14 — A worked example in a spec page can name any callable, so spec prose becomes executed code**
- [OMNI-162] **M31 — A held-out backtest's recorded answer reached the next proposer prompt through the rejection reason**
- [OMNI-161] **M30 — The backfilled `known_at` of a retail price was a day early in a holiday week**
- [OMNI-155] **H10 — A candidate could block the host past the worker timeout by not reading its input**
- [OMNI-156] **H11 — A candidate's stderr was stored and read back without a bound, on the host**
- [OMNI-157] **H12 — A failed brake-state save let a restart undo spend that was charged**
- [OMNI-158] **H13 — An admin command could overwrite newer brake state with an older snapshot**
- [OMNI-159] **M29 — Reading a sensor trace while a capture appended to it reported the trace as corrupt**
- [OMNI-160] **L54 — Two captures at the same instant left a restored sensor value looking withdrawn**
- [OMNI-55] **M16 — Any exception after the LLM call loses that call's spend from the CEO ledger and the episodic log, strands the branch/PR, and can kill `--loop`**
- [OMNI-154] **H9 — A candidate can run a timed batch on several cores, so the same algorithm measures twice as fast**
- [OMNI-153] **H8 — A candidate can make its own worker's reply encoding cheaper, so the same speed measures as faster**
- [OMNI-152] **H7 — The benchmark timed repeated inputs once its batches outgrew the oracle's input range, so a memoised candidate could pass**
- [OMNI-151] **M28 — A second loop can start on the same box, and the two do not share the spend cap**
- [OMNI-56] **M17 — A QA-stage rejection drops the gauntlet's reject reason**
- [OMNI-45] **H2 — The benchmark verdict could be forged from inside the measured process**
- [OMNI-45] **M8 — Every non-benchmark gate accepted exit code 0 as a pass, with no positive verdict token**
- [OMNI-45] **M11 — A candidate's own exception inside the invariant or backtest gate was filed as a harness fault**
- [OMNI-45] **M9 — The per-validation temp directory was shared, writable, and reused across every gate, so a candidate could rewrite the exam files later gates trust**
- [OMNI-138] **M27 — A converged target files bug tickets and trips the circuit breaker**
- [OMNI-137] **L47** — `tofu apply` with a new release tag stopped and started the box instead of installing the tag
- [OMNI-136] **M26 — A replaced box forgets the PR awaiting review, so the loop opens a second, conflicting PR**
- [OMNI-126] **M25 — A verified PR awaiting merge is forgotten when the process exits, so the next run proposes the same change again**
- [OMNI-57] **M18 — A PR a human closes without merging holds the canary — and, under `loop.serve(watch_merges=True)`, the whole loop — open indefinitely**
- [OMNI-125] **L45** — commit-lint reported a commit that had a key as keyless when its message was larger than the pipe buffer: under `pipefail`, `echo "$msg" | grep -q`…
- [OMNI-123] **M24 — `main.py` is silent where it matters**
- [OMNI-124] **L44** — The run box does not know its artifacts bucket: the runbook's sync step reads `s3://<artifacts-bucket>/…`, and in the first AWS run (2026-09-27) it…
- [OMNI-121] **H5 — The first AWS run optimised the default contract, and nothing showed or recorded which**
- [OMNI-122] **H6 — The pager preflight passed with 0 confirmed SNS subscriptions**
- [OMNI-62] **L24** — Budget exhaustion stops `--loop` silently; `loop.decide()`'s own comment says a human is paged, but nothing files anything
- [OMNI-61] **L21** — CEO brake-state persistence fails open: a corrupt, unwritable, or newly-switched state store silently resets `spent=0` and clears the breaker trip…
- [OMNI-61] **L23** — `CEO.reset_breaker()` has no caller anywhere outside tests; in practice the only reset is deleting the state file, which also zeroes spend
- [OMNI-51] **M15 — The real GitHub adapter and the SWE's policy check both hardcode `runtime/target.py`, so every non-default contract is judged against the wrong baseline on real adapters**
- [OMNI-49] **M19 — On the AWS run box, a `--canary serve` green replica can reach IMDS instance-role credentials and read other processes' environment via `/proc`**
- [OMNI-46] **H4 — An output type that overrides `__eq__`/`__ne__` defeats every correctness gate, offline and online**
- [OMNI-47] **M10 — Candidate and reference/baseline share mutable argument objects, so a candidate can sabotage its own comparison**
- [OMNI-41] **The benchmark gate measured a cache, not an algorithm — a memoised naive candidate passed every gate**
- [OMNI-37] **The docker sandbox could not read its own temp dir on native Linux — so it blamed every candidate**
- [OMNI-110] **The AWS box could not start the operator console**
- [OMNI-14] **Serve replica CPU reservation deadlocked CI — `serve.run()` blocked forever on a constrained runner**
- [OMNI-14] **The L5 noise floor, third appearance: Serve dispatch overhead swamps the target's own compute**
- [OMNI-111] **Flaky Serve tests: per-test app churn blew `serve.delete`'s own timeout**
- **Minor batch** — *(2026-08-05)* The six unnumbered items from the 2026-07-28 review, each with a regression test that fails without its fix: - [OMNI-112]…
  - [OMNI-112] **`candidate_sha` dropped on the policy-block path**
  - [OMNI-113] **A missing `tests/test_target.py` was blamed on the candidate.**
  - [OMNI-114] **Settings re-read per call.**
  - [OMNI-115] **Jira `children()` built JQL by f-string.**
  - [OMNI-116] **`transition()` was undone by a failed comment.**
  - [OMNI-117] **Duplicate gate numbering**
- [OMNI-92] [OMNI-93] **M2 + L9** *(2026-07-29)* Detached actors now share the `sis` Ray namespace and are created with …
- **L10–L14** — *(2026-07-28, one batch)* Five low-severity fixes, each with a regression test: - [OMNI-104] **L10** — `policy.target_paths()` used `lstrip("./")`…
  - [OMNI-104] **L10**
  - [OMNI-105] **L11**
  - [OMNI-106] **L12**
  - [OMNI-107] **L13**
  - [OMNI-108] **L14**
- [OMNI-97] **M6** — *(2026-07-28)* No HTTP timeouts on real-adapter calls — `requests` defaults to *no* timeout, so a wedged Confluence/Jira/GitHub API would hang a…
- [OMNI-96] **M5** — *(2026-07-28)* The CEO budget/brakes had no config knob — the docs said "set a tiny budget for the first run" but the only path was editing source,…
- [OMNI-118] *(2026-07-25, PR #32)* Confluence duplicate-title 400 crashed re-runs → `create_page` updates the existing …
- [OMNI-119] *(2026-07-25, PR #32)* Cross-space `parentId` 404 crashed the spec page → parent dropped, provenance in the…
- [OMNI-120] *(2026-07-25, PR #31)* Cycles baselined on the stale local file for the *proposer input* → `live_target_sou…
- [OMNI-90] **H1** — *(2026-07-25)* `gauntlet.validate()` benchmarked against the local `runtime/target.py` instead of the cycle's baseline → after a merge a no-op…
- [OMNI-94] **M3** — *(2026-07-25)* No identical-source short-circuit
- [OMNI-109] **"No change" was treated as a failure**
- [OMNI-91] **M1** — *(2026-07-25)* Subprocess sandbox let untrusted LLM code read host files
- [OMNI-95] **M4** — *(2026-07-25)* `SWE.implement` forked feature branches from a hardcoded `"main"` while `live_target_source` read `settings.default_base` —…
- [OMNI-100] **L3** — *(2026-07-25)* Verified `cost.py`'s `PRICING` against published rates: `claude-opus-4-8` $5/$25 (the model the loop prices spend with),…
- [OMNI-99] **L2** — *(2026-07-25)* The idempotent `create_page` fallback PUT a new version every run
- [OMNI-101] **L4** — *(2026-07-25)* `policy._rel()` used `lstrip("./")`, which strips `.`/`/` characters rather than a `"./"` prefix (mangling `"../x"` → `"x"`)
- [OMNI-102] **L7** — *(2026-07-25)* `measure_baseline()` fell back to `0.0` silently
- [OMNI-103] **L8** — *(2026-07-25)* Re-running a cycle for an existing story 422'd on `create_branch`
- [OMNI-98] **L1** — *(2026-07-25)* The labels the roles tag pages with (charter/spec/ proposal/outline) were never written

[OMNI-1]: https://olafzumpe.atlassian.net/browse/OMNI-1
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
[OMNI-121]: https://olafzumpe.atlassian.net/browse/OMNI-121
[OMNI-122]: https://olafzumpe.atlassian.net/browse/OMNI-122
[OMNI-123]: https://olafzumpe.atlassian.net/browse/OMNI-123
[OMNI-124]: https://olafzumpe.atlassian.net/browse/OMNI-124
[OMNI-125]: https://olafzumpe.atlassian.net/browse/OMNI-125
[OMNI-126]: https://olafzumpe.atlassian.net/browse/OMNI-126
[OMNI-127]: https://olafzumpe.atlassian.net/browse/OMNI-127
[OMNI-136]: https://olafzumpe.atlassian.net/browse/OMNI-136
[OMNI-137]: https://olafzumpe.atlassian.net/browse/OMNI-137
[OMNI-138]: https://olafzumpe.atlassian.net/browse/OMNI-138
[OMNI-141]: https://olafzumpe.atlassian.net/browse/OMNI-141
[OMNI-143]: https://olafzumpe.atlassian.net/browse/OMNI-143
[OMNI-144]: https://olafzumpe.atlassian.net/browse/OMNI-144
[OMNI-145]: https://olafzumpe.atlassian.net/browse/OMNI-145
[OMNI-148]: https://olafzumpe.atlassian.net/browse/OMNI-148
[OMNI-149]: https://olafzumpe.atlassian.net/browse/OMNI-149
[OMNI-150]: https://olafzumpe.atlassian.net/browse/OMNI-150
[OMNI-151]: https://olafzumpe.atlassian.net/browse/OMNI-151
[OMNI-152]: https://olafzumpe.atlassian.net/browse/OMNI-152
[OMNI-153]: https://olafzumpe.atlassian.net/browse/OMNI-153
[OMNI-154]: https://olafzumpe.atlassian.net/browse/OMNI-154
[OMNI-155]: https://olafzumpe.atlassian.net/browse/OMNI-155
[OMNI-156]: https://olafzumpe.atlassian.net/browse/OMNI-156
[OMNI-157]: https://olafzumpe.atlassian.net/browse/OMNI-157
[OMNI-158]: https://olafzumpe.atlassian.net/browse/OMNI-158
[OMNI-159]: https://olafzumpe.atlassian.net/browse/OMNI-159
[OMNI-160]: https://olafzumpe.atlassian.net/browse/OMNI-160
[OMNI-161]: https://olafzumpe.atlassian.net/browse/OMNI-161
[OMNI-162]: https://olafzumpe.atlassian.net/browse/OMNI-162
[OMNI-164]: https://olafzumpe.atlassian.net/browse/OMNI-164
[OMNI-165]: https://olafzumpe.atlassian.net/browse/OMNI-165
[OMNI-166]: https://olafzumpe.atlassian.net/browse/OMNI-166
[OMNI-167]: https://olafzumpe.atlassian.net/browse/OMNI-167
[OMNI-170]: https://olafzumpe.atlassian.net/browse/OMNI-170
[OMNI-171]: https://olafzumpe.atlassian.net/browse/OMNI-171
[OMNI-172]: https://olafzumpe.atlassian.net/browse/OMNI-172
[OMNI-173]: https://olafzumpe.atlassian.net/browse/OMNI-173
[OMNI-174]: https://olafzumpe.atlassian.net/browse/OMNI-174
[OMNI-176]: https://olafzumpe.atlassian.net/browse/OMNI-176
[OMNI-177]: https://olafzumpe.atlassian.net/browse/OMNI-177
[OMNI-178]: https://olafzumpe.atlassian.net/browse/OMNI-178
[OMNI-179]: https://olafzumpe.atlassian.net/browse/OMNI-179
[OMNI-180]: https://olafzumpe.atlassian.net/browse/OMNI-180
[OMNI-181]: https://olafzumpe.atlassian.net/browse/OMNI-181
[OMNI-182]: https://olafzumpe.atlassian.net/browse/OMNI-182
[OMNI-183]: https://olafzumpe.atlassian.net/browse/OMNI-183
[OMNI-185]: https://olafzumpe.atlassian.net/browse/OMNI-185
[OMNI-186]: https://olafzumpe.atlassian.net/browse/OMNI-186
[OMNI-187]: https://olafzumpe.atlassian.net/browse/OMNI-187
[OMNI-188]: https://olafzumpe.atlassian.net/browse/OMNI-188
[OMNI-200]: https://olafzumpe.atlassian.net/browse/OMNI-200
[OMNI-201]: https://olafzumpe.atlassian.net/browse/OMNI-201
[OMNI-203]: https://olafzumpe.atlassian.net/browse/OMNI-203
