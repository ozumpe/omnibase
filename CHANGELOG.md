# Changelog

What each release of omnibase carried, newest first. A release is a git tag on
`main`, cut from `develop` through a release PR; the AWS box runs the latest
tag, never `develop`. `OMNI-N` is a Jira key
([board](https://olafzumpe.atlassian.net/browse/OMNI)); H/M/L IDs are defects
in [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md), which has the full write-up
of each. Until 2026-10-31 this history lived in `CLAUDE.md`'s "Current status"
(OMNI-184).

When a change merges to `develop`, add a line under **Unreleased**; cutting a
release renames that heading to the new tag.

## Unreleased

- **OMNI-163: the first gated omnitrack contract.** `ca_price_wholesale`
  predicts next week's California retail price and must beat persistence: a
  `BacktestSkill` on the backtest gate requires a mean absolute error at least
  10% below the baseline's over the held-out episodes, with the baseline run
  in the trusted harness on its own copy of the arguments. `EIASensor` also
  captures the Los Angeles spot price, which is licensed: a fixture that shows
  it is private, its episodes kept in the artifacts bucket and checked by the
  sha256 in a committed `specs/<name>/private.json`. The contract registers
  once that index is promoted.
- OMNI-175: a proposer API error (a 401, say) is named as the proposer's, not
  as "in the SWE's step, after the proposal".
- OMNI-73 (L27): only the harness's own words (`<gate> gate timed out`) make a
  timeout; a candidate's text that says "timed out" no longer does.
- `scripts/aws_secret.py` checks the Anthropic key against the API before it
  uploads the run secret, and a scheduled run's log shows the key's last four
  characters.
- OMNI-184: this changelog; the 2026-10-31 code and documentation review
  recorded as L61–L68 (OMNI-176–183); the empty `identifier.db` removed and
  `*.tfplan` ignored.

## v0.5.0 — 2026-10-30

omnitrack Phase A's milestone (epic OMNI-30, closed), and the fixes since
v0.4.0.

- **OMNI-35: the first recorded fixture**, `specs/ca_price_next_week`:
  California, autumn 2022, built from a real capture with the release calendar
  checked by a human, two captures agreeing (PR #182).
- **OMNI-36: the autumn-2022 errors against persistence**, from that capture:
  18 weeks, bias +$0.050, MAE $0.179, RMSE $0.232 a gallon. Reported, never
  acted on (that is Phase B).
- **OMNI-40: the twin viewer**, slices 1 and 2 (`python -m sis.viewer`):
  replay in time and the scenario views. The geo heat map is the deferred
  third slice.
- **OMNI-48 (H3): the Serve canary serves its candidate from a sandbox
  worker**, not as a Ray worker with control-plane access. `--canary serve`
  is still refused with a non-stub proposer (OMNI-49) until a human lifts it.
- Fixes: OMNI-66 (L17, a `nan` spend cap), OMNI-67 (L18, a guardrail path in
  another case), OMNI-170 (M34, a predictor shown the value it was asked for),
  OMNI-171 (M35, a scheduled run losing its loop's lines in a long log),
  OMNI-173 (L60, a docker test that failed as root), OMNI-71 (L25, an unpriced
  model billed at another's rate), OMNI-65 (L16, a sandbox self-check a
  candidate could break through the scratch), OMNI-145 (L50, an inconclusive
  cycle counted toward "converged"), OMNI-127 (L46, an identical resubmission
  judged and billed twice), OMNI-82 (L37, a preflight that passed a rejected
  Anthropic key).

## v0.4.0 — 2026-10-26

omnitrack Phase A's sensor work: the first code that reads the outside world.

- OMNI-31: the `Sensor` port and `SimSensor`; a reading carries a unit and
  two times, `event_time` and `known_at`.
- OMNI-32: sensor input is untrusted and comes in through one door,
  `sensor_input.sanitise`.
- OMNI-33: `EIASensor` and its append-only capture trace (D12), with
  reconstructed `known_at` for backfilled history.
- OMNI-35: the fixture tooling: a replay ordered by `known_at`
  (`sis/replay.py`), the fixture builder, and the release-calendar check.
- OMNI-34: seeded scenarios (`specs/scenarios.py` pinned, `runtime/scenarios.py`
  the working copy); simulated data may refute a candidate, never promote one.
- OMNI-36: the prediction-error report and the persistence baseline
  `ca_price_persistence`.
- OMNI-168, OMNI-169: scheduled AWS runs, on a stopped box or on an ephemeral
  one the task builds and always destroys.
- Fixes from the review after v0.3.7: OMNI-155 (H10, a worker that stops
  reading its input), OMNI-156 (H11, unbounded candidate stderr), OMNI-157
  (H12, spend lost when a brake-state save failed), OMNI-158 (H13, an older
  brake snapshot replacing a newer one), OMNI-159 (M29, a trace read during a
  capture), OMNI-160 (L54, two captures at one instant), OMNI-161 (M30, a
  retail `known_at` a day early), OMNI-162 (M31, a held-out answer reaching
  the next prompt).
- Also: OMNI-54 (M14), OMNI-68 (L19, what is promoted is what was reviewed),
  OMNI-42 (L39), OMNI-52 (M12, a `soft_` config edit rewriting `forbidden_`
  keys), OMNI-164 (M32, a fixture showing an earlier vintage of the week it
  predicts), OMNI-165 (M33, a staged name placing an exam outside
  `specs/<name>/`), OMNI-167 (L56, an adapter declaring the simulated source).

## v0.3.7 — 2026-10-08

Hardening after the seventh AWS run, most of it gate integrity.

- OMNI-151 (M28): one loop per box (`runtime/loop.lock`); a second start exits 3.
- OMNI-152 (H7): no benchmark input is timed twice, nor after a worker met it.
- OMNI-153 (H8): a timed batch runs no Python per call in the worker.
- OMNI-154 (H9): each benchmark worker gets one CPU, and a different one.
- OMNI-55 (M16, L30): an exception inside a cycle is an outcome, and its spend
  is charged.

## v0.3.6 — 2026-09-30

Fixes from the sixth AWS run, the first Class-2 run (`roman`, built at the
first attempt).

- OMNI-149 (L52): a built feature's convergence check comes before planning,
  so it files nothing.
- OMNI-148 (L51): a feature's intake says "Build", not "Speed up".
- OMNI-150 (L53): the log names only a model that was called.
- OMNI-79 (L34): a GitHub read fails loudly unless the file is truly absent.

## v0.3.5 — 2026-09-29

The two blockers before omnitrack Phase A.

- OMNI-146 (M8): the interface, acceptance, invariant and backtest gates judge
  the candidate from outside its process.
- OMNI-147: the loop builds a Class-2 feature from its spec (`--contract roman`).
- Also OMNI-56 (M17), OMNI-64 (L15), OMNI-78 (L33).

## v0.3.4 — 2026-09-29

- OMNI-45, gate integrity: the Class-1 benchmark times its candidate from
  outside the candidate's process (H2), the exam files are protected (M9), a
  zero exit is no longer a verdict (M8, in part), and a candidate's own
  exception counts against it (M11).

## v0.3.3 — 2026-09-29

What the fifth AWS run called for.

- OMNI-140: the loop syncs the run's dataset to S3 itself, every cycle and
  when it stops. A box replaced ahead of a hand sync had taken run #3's log and
  the first cycle of run #5.
- OMNI-139: the runbook runs the loop inside tmux and tees the console, so a
  dropped SSM session no longer kills it; SIGHUP is a graceful stop.

## v0.3.2 — 2026-09-29

Fixes from the fourth AWS run.

- OMNI-138 (M27): a converged target stops the loop politely instead of
  tripping the breaker; the run's finished `sum_of_divisors` had filed three
  bugs.
- OMNI-135: a feature is planned once (one spec, epic and story; the run filed
  22 TES issues for six cycles), and a restart or a new box carries on a
  half-built feature from its branch.
- OMNI-137: a new release tag builds a new box.

## v0.3.1 — 2026-09-29

- OMNI-136 (M26), from the third AWS run: the loop asks GitHub which of its
  PRs are still open before every cycle, so a rebuilt box no longer opens a
  second, conflicting PR (testrun #14 beside #13).
- OMNI-29: the restructured runbook, with a working rehearsal.

## v0.3.0 — 2026-09-27

Staged delivery, phases 0 and 1 (epic OMNI-128, `docs/STAGED_DELIVERY.md`).

- OMNI-129: the sandbox worker serves a candidate hot from the sandbox, over a
  pipe, in both sandbox modes.
- OMNI-130: passing steps are committed to an agent-owned feature branch, and
  one PR opens per finished feature.
- OMNI-126, OMNI-57 (M25, M18), from the second AWS run's duplicate PR: the
  hold for a PR survives a restart, and a PR closed without merging releases
  it as `human_declined`.

## v0.2.1 — 2026-09-27

Fixes from the first AWS run (OMNI-29, 2026-09-27). The box ran v0.2.0 end to
end: a real Claude proposer in the docker sandbox, real Confluence, Jira and
GitHub artifacts (`TES-43`…`TES-53`), three cycles all rejected by the
benchmark gate with tight intervals (ratios 1.34 / 0.93 / 1.04), the breaker
tripping on the third, $0.107 of a $1.00 budget. Artifacts in
`s3://sis-first-run-artifacts-696644743351/runs/20260927-0007/`.

- OMNI-121 (H5): the run optimised the default contract, not `sort`.
- OMNI-122 (H6): the pager preflight passed with 0 confirmed SNS
  subscriptions, so the page reached nobody.
- OMNI-123 (M24): the console now says why a cycle rolled back and why the
  loop stopped.
- OMNI-124 (L44): the box knows its artifacts bucket.
- OMNI-125 (L45): commit-lint handles a large commit message.

## v0.2.0 — 2026-09-26

The first release since v0.1.4 (OMNI-63), carrying everything below through
the prerequisites of the first AWS run.

**Validated on real systems.**
- Level 2 (2026-07-25): real cycles ran end to end against a live tenant
  (scratch Jira project `TES`, throwaway repo `ozumpe/testrun`), re-runs
  included. Live-tenant fixes: a duplicate Confluence title updates the page
  in place; a cross-space parent is dropped (provenance lives in the
  SelfModel); a cycle starts from the target as merged on the base branch
  (`VersionControl.live_target_source`), so a merged optimisation is built on,
  not re-proposed.
- First real-life test (2026-07-28): cycle `cb4f6fe13ed7`, a real Claude
  proposer, real adapters and the docker sandbox. `claude-opus-4-8` proposed
  the O(√n) `isqrt` form; the gauntlet passed inside docker (192.4 µs →
  1.6 µs) and the cycle filed Confluence spec `6356994`, Jira `TES-20` and
  `testrun` PR #4, stopping at `verified_awaiting_human_merge`. Cost
  $0.014375, reconciled against the Anthropic console.

**After the 2026-07-25 full review.** H1: the benchmark runs against the
cycle's baseline source, and a candidate identical to it is the neutral
`no_change`. M1: a real proposer requires the docker sandbox. M4: the SWE
forks from the configured base branch. M5: the spend brakes are configurable.
L3: the pricing table is checked against published rates.

**A target-agnostic engine** (OMNI-1, L5, 2026-08-06; OMNI-4–7). Each target
is a contract (`sis/contract.py`) and an exam under `specs/<name>/`, which is
POLICY-FORBIDDEN; the proposer prompt is derived from the contract. Two
targets ship: `sum_of_divisors` and `sort_numbers`.

**The Ray Serve canary** (OMNI-2, 2026-08-09; OMNI-8, 9, 11–15).
- OMNI-11: blue and green served over HTTP with different source.
- OMNI-12: a load generator drawing inputs from the contract's oracle. The
  candidate measured ~5x faster alone but ~30% faster at p95 under 8-way load;
  that gap is why the canary exists.
- OMNI-13: `ServeCloud`, with a real weighted split (`CanaryRouter`, since Ray
  Serve has none), shadow dispatch and per-version windows. Green is a
  separate Serve application, because one app restarted blue on every canary
  deploy, and green's environment is scrubbed of credentials.
- OMNI-14: `DevOps.canary(canary_backend="serve")`; a live rejection changes
  the cycle's outcome.
- OMNI-15: the loop closes on a human merge (`DevOps.observe_merge`).

**Class 2, feature construction** (OMNI-3, done 2026-08-11, epic closed
2026-08-27; `docs/CLASS2_CONTRACT.md`).
- OMNI-19: the backtest gate, with held-out splits and named comparators.
- OMNI-23: the `Clock` port, so a fixture's `event_time` is a real instant.
- OMNI-17: `FeatureContract`, the contract-selected gate profile, and the
  determinism axis; the first Class-2 target is `roman`.
- OMNI-18: the invariant gate (Hypothesis, seeded).
- OMNI-21, OMNI-26: the contract-author write path into `specs/`, human
  promotion only. Its review found a discrimination check that read pytest's
  exit code, spec prose that could break out of a docstring, and a check that
  bypassed the sandbox precondition; all fixed before merge.
- OMNI-24: the SLO gate (2026-09-25). OMNI-20 (`ToolchainAdapter`) is parked.

**omnitrack decided.** D0 (OMNI-25): regional air traffic (2026-08-14),
revised on 2026-09-24 to the California gasoline market first and Iowa's
spirits supply chain second. The whole register, D0–D12, was settled on
2026-08-28 (`docs/OMNITRACK_VISION.md`).

**Operations.**
- OMNI-27 (PR #92, 2026-08-16): one configuration schema, `config.yml`, with
  CLI > env > file > default. `SIS_SANDBOX=dcoker` and
  `SIS_ALLOW_STRICT_CHANGES=yes` used to fail silently; both now raise.
- OMNI-28 (PRs #93, #97, #98, 2026-08-28): the operator console, its tier gate
  in `sis/operator.py`, the `strict_` justification and the audit log
  (`docs/OPERATOR_FRONTEND.md`).
- OMNI-29, prepared: designed 2026-08-16 (PR #94), pre-flighted 2026-08-30
  (PR #99; `user_data` raced `unattended-upgrades`, SSM landed as `ssm-user`),
  rehearsed 2026-09-23 on a local Ubuntu box (the docker sandbox could not read
  its temp dir on Linux; the bootstrap lacked the `ui` group).
- OMNI-37 (PR #106, 2026-09-25): a broken sandbox is never blamed on the
  candidate.
- OMNI-41 (2026-09-26): the benchmark decides on paired fresh inputs and total
  cost, with an inconclusive verdict. It closed a hole where a naive
  implementation under `functools.cache` measured 25 ns and passed every gate.
- From the 2026-09-26 multi-dimension review: OMNI-46 (H4), OMNI-47 (M10),
  OMNI-49 (M19), OMNI-51 (M15).
- Prerequisites of the first AWS run (2026-09-26): OMNI-60 (the CI fast path),
  OMNI-61 (L21, L23: brake state fails closed, `sis.admin`), OMNI-62 (L24: the
  SNS pager), OMNI-63 (the box runs a release tag).
- #110: the Serve test that failed one run in twenty asserted blue answered
  while green held its 5% default weight.

## v0.1.4 — 2026-07-05

- The legacy `--loop` micro-loop retired.
- A docker sandbox timeout no longer leaks its container; resource caps added.
- The real-adapter seam survives a failing adapter; the `anthropic` pin
  unfrozen.

## v0.1.3 — 2026-07-04

- Failures become artifacts: every rejected cycle and every breaker trip files
  a bug (`DevOps.file_bug`), and the CEO writes the charter once at bootstrap.
- One episodic store for every log.

## v0.1.2 — 2026-07-04

- Jira `children()` uses the enhanced search (`POST /search/jql`).

## v0.1.1 — 2026-07-03

- Candidate code never runs in the main process: the baseline measurement is
  sandboxed too.

## v0.1.0 — 2026-06-20

The bootstrap skeleton: the actor org (CEO, CTO, PM, SWE, QA, DevOps,
Designer, plus `Workspace` and `SelfModel`), one intake → deploy cycle that
stops at the human PR merge, the validation gauntlet in a subprocess or docker
sandbox, a stub and a Claude proposer, ports and adapters, the CEO's spend
brakes, the change-authorization policy, the episodic store (`jsonl`,
`duckdb`, `none`), and the branch-workflow guard.
