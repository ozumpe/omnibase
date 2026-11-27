# Changelog

What each release of omnibase carried, newest first. A release is a git tag on
`main`, cut from `develop` through a release PR; the AWS box runs the latest
tag, never `develop`. `OMNI-N` is a Jira key
([board](https://olafzumpe.atlassian.net/browse/OMNI)); H/M/L IDs are defects
in [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md) (open ones in full,
resolved ones as one line; the write-up is in the Jira ticket). Until 2026-10-31 this history lived in `CLAUDE.md`'s "Current status"
(OMNI-184).

When a change merges to `develop`, add a line under **Unreleased**; cutting a
release renames that heading to the new tag.

## Unreleased

- OMNI-199: Phase B's milestone runs as one command,
  `scripts/phase_b_demo.py`. A seeded supply shock breaches the error budget
  for two windows, pages and starts a cycle through the `--loop` trigger; a
  quiet market starts none. No credentials or LLM; `--cycle` also runs the
  cycle with the stub proposer. `tests/test_phase_b_demo.py` pins the outcome.

## v0.9.0 — 2026-11-27

Phase B (epic OMNI-194) reaches the box: the Friday run captures EIA's weekly
data itself (OMNI-214), prediction error can start a cycle (OMNI-197), and
two Phase B parts arrive unwired: the retrain stop rule (OMNI-228; the
trigger does not call it yet) and the refinery-outage event signal
(OMNI-227). Gate verdicts are typed (OMNI-208). Not in this release: the
OMNI-199 demo and wiring OMNI-228 into the trigger.

- OMNI-214: the weekly EIA capture runs unattended. The scheduled AWS run
  fetches the canonical trace from the artifacts bucket (`traces/eia.jsonl`),
  runs `scripts/capture_sensor.py`, reads the trace back and writes it back
  with a conditional write (`--if-match`), before its preflight. Only line
  counts reach the log; the licensed LA spot series stays in the bucket. A
  failed, empty or raced capture leaves the bucket's trace as it was and
  pages through SNS; the loop's run goes on. The bucket needs seeding once
  (docs/SENSOR_RUNBOOK.md).
- OMNI-197 (Phase B 3): prediction error can start a cycle. With
  `--loop-trigger model-error` (`loop.trigger`, default `repeat`), `--loop`
  reads the live predictor's scored error series each tick and replays the
  two D9 triggers over it: a sustained error-budget breach pages (WARNING)
  and starts a cycle, sustained skill decay starts one without a page, a
  spike starts none. A firing starts one cycle; the cycle's body and
  `Work.evidence` say which trigger fired and the numbers. New STRICT keys
  `model_error.mae_budget` (acceptable error), `breach_weeks` (how long),
  `skill_floor`, `window_weeks` and `min_weeks`; docs/ENGINE_RULES.md has a
  tight "refine often" and a loose "leave it alone" setting.
- OMNI-208: a rejection is typed, not read from its wording. Every failure
  the gauntlet returns carries a `Verdict` (gate, kind, sentence;
  `sis/verdict.py`, FORBIDDEN), and the SWE, QA, the canary, the review and
  the cycle's own errors carry one next to their reason. The sandbox probe,
  the neutral statuses, the breaker's gate, the bug headline, the end of a
  feature and the episodic `reject_gate` read its fields, so a candidate's
  text can no longer decide whose failure it was (the class of bug behind L27
  and L64). Old episodic rows and stored rejections are still read by the
  former parser, and only they. The benchmark's worker crashes are now
  recorded as `benchmark_worker` (they had no gate before), and QA's
  `review()` returns the verdict as a third value.
- OMNI-228 (Phase B): retraining that does not help stops. `sis/futility.py`
  (FORBIDDEN) decides, from the episodic records, whether the last
  `brakes.futility_retrains` (3) retrains a model-error trigger started for a
  contract each failed to cut the window's error by
  `brakes.futility_min_improvement` (5%). If so, the trigger starts no more
  retrains for it (no spend), records `futile_stopped`, and pages once
  (WARNING, "likely missing input", with the weeks of largest error). A new
  input (a new basis) or `python -m sis.admin reset-futility --contract <name>
  --reason "..."` resets it; `sis.admin futility` shows where each contract
  stands. The breaker is untouched. New episodic fields `trigger`,
  `trigger_mae`, `trigger_basis`. OMNI-197 calls `Guard.may_start` and
  `Guard.started` when it wires the triggers.
- OMNI-227 (Phase B): a free event signal for price shocks. The EIA sensor
  now also captures West Coast (PADD 5) refinery utilisation
  (`PET.W_NA_YUP_R50_PER.W`, public domain, same release), and
  `scripts/capture_sensor.py` derives from it a weekly refinery-outage flag (a
  fall of 4 points or more) into its own append-only trace,
  `runtime/sensor_traces/events.jsonl`, through the sanitising door, never
  known before the values it rests on (`sis/events.py`, FORBIDDEN).
  `scripts/build_fixture.py --training --events` shows it in a training split
  as context; no gate reads it and no episode shows it. No recurring cost.
- OMNI-189: a new box remembers what earlier runs rejected. A rejected step's
  episodic record now carries `baseline_sha`, and each step is handed the
  rejections in this box's log and in `runtime/history/`. A step from the same
  base no longer re-judges a candidate an earlier run rejected (the
  2026-11-06 run of `sort` paid to judge one again), and its prompt carries
  the earlier reasons. Records from before this change have no
  `baseline_sha` and are not matched.
- OMNI-198 (Phase B 4): the predictor's error series is kept. After each
  capture, `scripts/capture_sensor.py` scores the live predictor on the weeks
  that became scorable (`--no-score` skips it; `scripts/score_capture.py`
  scores a trace on its own) and appends one row per week to the episodic
  store (`runtime/episodic_scored.jsonl`, or duckdb's `scored_weeks`), with
  the predictor version, capture, errors and running skill. Each week is
  scored once per predictor version; a later vintage of its actual is a
  revision row, never an overwrite. `sis/error_series.py` (STRICT);
  `as_steps` feeds `sis.model_error`. `capture_sensor.py --help` now prints
  help instead of capturing.
- OMNI-225 (D5): held-out rotation. `scripts/build_fixture.py --rotate` stages
  the weeks after the held-out set as the new one and a training split that
  now covers the old one, as one draft for one promotion; its budget starts at
  zero. New config `brakes.holdout_rotation_min_weeks` (13). A plain rebuild
  of a promoted private fixture now keeps its committed weeks.
- OMNI-224 (M41): a candidate judged again on the same held-out fixture no
  longer uses another holdout judgement. The "already built" check after a
  merge re-judges the merged module every cycle; the 2026-11-21 run used 4 of
  its 50 judgements where 1 was due. Each `candidate_sha` now counts once per
  contract and fixture; new code still counts the first time it is judged.
  A contract's `holdout_count_repeats` (else `brakes.holdout_count_repeats`,
  off by default) brings back counting every repeat.
- OMNI-144 (L49): every artifact sync now also uploads `run.json`, which says
  which part of the box's cumulative files is this run's (byte offsets, first
  and last `cycle_id`, contracts, cost, code version, carried spend at the start
  and now). A run's S3 folder read as one run no longer double-counts.
- Docs: `docs/KNOWN_ISSUES.md` listed M37 (OMNI-218) as open after #235 fixed
  it; it is now a Resolved row.

## v0.8.0 — 2026-11-21

Epic OMNI-204, the proposer's honest homework: a training split it may see
(OMNI-205), a limited number of judgements per held-out fixture (OMNI-207),
and a workbench to test its model on the training split before it answers
(OMNI-206). The scheduled AWS run's budget rises to $2.50.

- OMNI-206 (epic OMNI-204): the proposer's workbench. A real proposer
  building a forecasting contract may run Python on its training split in the
  docker sandbox before it answers, one run per turn, and see what it printed:
  it can measure its own error against the baseline on history it may see.
  Nothing held out is mounted. Limits and a spend cap in
  `proposer.workbench_*`; a cycle that uses it reserves the cap plus a review
  (about $1.64 at the defaults), so the scheduled AWS run's budget is now
  $2.50 (was $1.00, Olaf's choice).
- OMNI-205: a forecasting contract may have a training split
  (`specs/<name>/training.json`): public history known before its first
  held-out question, shown to the proposer as compact tables so it can fit a
  model instead of writing blind. No gate reads it; the contract refuses a
  split that reaches into the held-out weeks. Built with
  `scripts/build_fixture.py --training` and promoted like any spec file.
- OMNI-207: the holdout budget (D5). Each cycle's record names the held-out
  fixture it was judged on, and a contract's fixture gives at most
  `holdout_budget` judgements (default `brakes.holdout_budget`, 50). When
  they are spent the next cycle is refused before any spend, pages a WARNING,
  and the loop stops; a new fixture starts at zero. Each cycle prints the
  budget left; `python -m sis.admin holdout` shows every contract's. The
  scheduled AWS run fetches earlier runs' episodic logs first, so the count
  survives a new box.

## v0.7.0 — 2026-11-19

Phase B's model-error triggers (OMNI-196, OMNI-221) and climatology fix
(OMNI-222), the proposer's configurable output limit with one retry when cut
off (OMNI-218, OMNI-219), token logging (OMNI-220), and the fixes since v0.6.0.

- OMNI-222 (L77): climatology is computed from exactly the readings the
  predictor is shown (the same as-of view, back to the same lookback), not
  the whole history. With the default 52 weeks it learns from last year
  alone, and has nothing in reach after a 53-week ISO year (2021, 2027), so
  no skill is claimed for those weeks. The skill no longer counts a lead
  built on seasons the predictor never sees. The autumn-2022 figure in
  `docs/SENSOR_RUNBOOK.md` was measured with five years and needs a re-run.
- OMNI-221 (M39): the model-error triggers count a breaching window toward
  their streak only when it shares no week with the last window counted.
  One outlier week used to fire the "sustained" error budget twice, and a
  tick on unchanged data counted as a new breach. `error_budget` and
  `skill_decay` take `counted_through` and return it on the `Decision`.
- OMNI-219 (M38): a proposal cut off at its output limit is retried once in
  the same cycle, at the new `proposer.retry_output_limit` (32000, FORBIDDEN),
  instead of the next cycle repeating the same call at the same limit. The
  cut-off call is charged and the retry reserved and saved before it is sent;
  the spend cap may refuse it. The retry's verdict decides the cycle, which
  counts once toward the breaker. Cut off again, the breaker opens with
  "proposal cut off twice, at N and then M output tokens; raise
  proposer.retry_output_limit", and a human is paged. A call above 20000
  output tokens is streamed, which the SDK requires.
- OMNI-166 (L55): an atomic write (`sis/atomic.py`) keeps the file's
  permissions, and a new file gets what a plain write would (`0o666` less the
  umask). It used to create its temporary file 0600 and put it in place of the
  original, so the first operator save or `python -m sis.config --write` made
  `config.yml` unreadable to a console or CI step under another uid. Brake
  state and `config.yml` hold no secret, so no caller asks for less.
- OMNI-220 (L76): each cycle now records the proposer call's token counts
  (input, output, cache written and read), its output limit and why it
  stopped, on the episodic record and as `out N/LIMIT tokens` on the
  `[cycle]` line. The weekly run's cut-offs showed only their cost, so how
  close a call came to the limit had to be worked back from the price list.
  Counts only, never prompt or answer text; older episodic rows and DuckDB
  files still load.
- OMNI-218: the proposer's output limit, thinking included, is now the
  `proposer.output_limit` config key and defaults to 16000 instead of a fixed
  8000. In the weekly run of 2026-11-14 two of three `ca_price_wholesale`
  cycles were cut off at 8000 and thrown away unjudged. The per-cycle spend
  reservation rises from $0.50 to $0.60 so it still covers a proposal and its
  review at their limits.
- OMNI-181 (L66): `scripts/aws_secret.py` now says "Anthropic could not be
  reached" and refuses the upload when the connection drops mid-response
  (`RemoteDisconnected`, a reset socket, a cut-off read), instead of ending in
  a traceback. It catches `OSError` and `http.client.HTTPException`.
- OMNI-182 (L67): when the proposer's answer is cut off at the token limit
  and recording that then fails (the Workspace unreachable, say), the error is
  labelled the SWE step's, not the proposer's: the proposer had answered. The
  cost and the breaker count were right before and are unchanged.
- OMNI-203 (L75): the agent review's deterministic rules allow only imports
  from a list of pure-computation modules (`review.SAFE_MODULES`) instead of
  refusing a list of I/O ones, which had missed `codecs`, `gzip`, `zipfile`,
  `runpy`, `inspect` and others. They also flag `__builtins__`, dunder
  attributes such as `__globals__` and `__subclasses__`, `getattr`/`hasattr`
  with a computed or dunder name, and `setattr`/`delattr`. Every shipped stub
  candidate still passes, pinned by a test.
- OMNI-179 (L64): the gauntlet skips its sandbox self-check only for the
  harness's own timeout words (`gauntlet.is_harness_timeout`), the same test
  the episodic store files a timeout by. A candidate whose failure merely
  quotes "timed out" is probed like any other failure, so a broken sandbox is
  no longer missed on that path.
- OMNI-186 (L69): the `test_merge_observation.py` tests that need a real cycle
  ending in a PR now build `roman` (Class 2, no benchmark) instead of speeding
  up `sum_of_divisors`, so an oversubscribed CI box can no longer make them
  `inconclusive`. They are about observing a merge, not timing.
- OMNI-211: `CLAUDE.md`, loaded into every session, is cut from 733 lines plus
  two `@`-included files to 122 lines with no includes; the detail moved to
  `docs/ENGINE_RULES.md`, and `tests/test_claude_md.py` caps it at 200 lines.
  `docs/KNOWN_ISSUES.md` keeps open and won't-fix entries in full and each
  resolved one as a line (2,320 to about 500 lines); `docs/AWS_RUN.md` drops
  its run-by-run history. Both full texts are in git at `0a86c58`.
  `docs/SENSOR_RUNBOOK.md` now says the capture is weekly and by hand
  (OMNI-214) and that only promotion still needs a person (OMNI-215).
- OMNI-196 (Phase B 2): the two model-error triggers as pure decisions,
  `sis/model_error.py`. The error budget fires on a window's MAE above the
  actor's budget, skill decay on its skill against climatology below the
  actor's floor; each needs a sustained breach and a minimum of scored weeks,
  keeps its own streak and names its reason. Nothing calls them yet
  (OMNI-197 wires them into `--loop`).
- OMNI-180 (L65): a backtest fixture or expectation file that is not UTF-8 is
  reported as a malformed fixture naming the backtest and the file, not as
  "the backtest gate raised (UnicodeDecodeError ...)".

## v0.6.0 — 2026-11-10

The first gated omnitrack contract (`ca_price_wholesale`, OMNI-163) and its
private fixtures, Phase B's first story (OMNI-195), signed CI (OMNI-190),
and the fixes since v0.5.0.

- OMNI-163: a real proposer building a contract with backtests is told the bar
  it must clear on the held-out episodes: each answer's tolerance and, with a
  skill check, how far its mean absolute error must fall below the named
  baseline, whose source it is shown. It was told only that episodes were held
  out. No fixture's name, path or value reaches the prompt.
- OMNI-201 (L73): a predictor answer too large for a float (an int JSON can
  carry, such as `10**400`) is "not a number" for its week in the
  prediction-error report, instead of an `OverflowError` that lost the whole
  report; a backtest baseline returning one is a harness fault, not a crash.
- OMNI-195 (Phase B 1): a climatology baseline and the skill against it in the
  prediction-error report. Climatology is the latest price plus the mean
  change from that ISO week to the next over up to five earlier years, from
  the as-of view, on the host. The summary (and `prediction.summary`
  telemetry) adds `climatology_mae`, `skill` and `skill_weeks`;
  `scripts/prediction_error.py` prints both. Autumn 2022: persistence's skill
  is about −0.03.
- OMNI-200 (M36): develop blue/green judges what `develop` serves after a
  merge (the base branch's file), not the PR's head. A head branch deleted on
  merge no longer earns a good merge a revert PR and a CRITICAL page, and a
  merge that differs from its head is judged as merged. Nothing to judge is a
  harness fault, never green's.
- OMNI-185 (L72): a human merge seen by the running loop is printed and logged
  (`human_merged`, at no cost) like a decline, so a run's `loop.log` and
  episodic log say when a PR was merged; a restart or the open-PR check logs
  one too.
- OMNI-190: CI checks the release calendar, in place of a person's sign-off.
  The sources it is drafted from are committed, reduced to dates (every value
  in a CSV is `x`: the spot prices are licensed) and pinned by sha256 in
  `scripts/data/release_calendar_sources/sources.json`. The fast suite
  re-drafts the calendar from them and fails on any difference, naming a date
  earlier than the sources as the leak it would be; a changed copy fails
  rather than re-drafting. `checked_by` is now `CI, sources sha256:…`, a claim
  `scripts/build_fixture.py` checks again before building, and a workflow
  attests the checked calendar with a Sigstore signature on `develop` and
  `main`. Retail dates have a second source, FRED's `GASREGW` vintages, and
  where sources disagree the later date wins; it reproduces the 2026-09-14
  correction by itself. The re-drafted calendar adds one spot day,
  2026-10-28 (released 2026-11-04).
- OMNI-187 (L70): a public fixture refuses to show a licensed series. The
  rule was only `scripts/build_fixture.py`'s choice of inputs; now
  `SourceRules.licensed` names the series (the LA spot price for EIA) and
  `fixture_builder.build` refuses one unless the fixture is private.
- OMNI-188 (L71): the scheduled run's log tail is capped by construction, each
  part bounded by `cut`, `head` and an offset `dd` read of a snapshot, so it
  stays under the 24,000 characters SSM keeps even if one tool returns too
  much; CI once saw 24,393 where 20,357 were expected.
- OMNI-50 (M20, M21): the live canary decides latency on paired samples, the
  offline benchmark's paired bootstrap on total cost, with an inconclusive
  interval neutral; an equal-speed candidate is rejected in about 2% of
  simulated windows, not 56%. It also judges error rate (green may fail no
  more often than blue), and a request green failed is a disagreement, not a
  dropped sample. `develop`'s blue/green uses the same paired rule.
- **OMNI-163: the first gated omnitrack contract.** `ca_price_wholesale`
  predicts next week's California retail price and must beat persistence: a
  `BacktestSkill` on the backtest gate requires a mean absolute error at least
  10% below the baseline's over the held-out episodes, with the baseline run
  in the trusted harness on its own copy of the arguments. `EIASensor` also
  captures the Los Angeles spot price, which is licensed: a fixture that shows
  it is private, its episodes kept in the artifacts bucket and checked by the
  sha256 in a committed `specs/<name>/private.json`. The contract registers
  once that index is promoted.
- OMNI-163: a fixture week whose answer was public before its question could
  be asked (a catch-up release: 2022-06-27 and 2026-09-21 in EIA's record) is
  skipped and listed in the staged README instead of refusing the whole build
  (M32's guard, unchanged otherwise). A build with every week skipped still
  refuses.
- OMNI-163: `ca_price_wholesale`'s private fixture is promoted (354 weekly
  episodes, 2020-01-06 to 2026-10-26), so the contract is registered. Git holds
  `private.json`, the README and the calendar; the episodes are in S3.
- OMNI-175: a proposer API error (a 401, say) is named as the proposer's, not
  as "in the SWE's step, after the proposal".
- OMNI-73 (L27): only the harness's own words (`<gate> gate timed out`) make a
  timeout; a candidate's text that says "timed out" no longer does.
- OMNI-176 (L61): a malformed `private.json` makes only `ca_price_wholesale`
  unavailable, with a reason naming the file and the episode; before, it stopped
  `import sis.contract`, and with it the loop and every role actor.
- OMNI-177 (L62): a scheduled AWS run of a contract with a committed
  `private.json` fetches its private fixtures from the artifacts bucket and
  checks each against its sha256 before anything is spent; before, every
  cycle would have failed the backtest gate as a `harness:` fault.
- OMNI-178 (L63): `scripts/build_fixture.py --fixture ca_price_wholesale`, with
  no `--last`, stops at the last week the release calendar covers and says so,
  instead of refusing a trace captured after the calendar ends.
- `scripts/aws_secret.py` checks the Anthropic key against the API before it
  uploads the run secret, and a scheduled run's log shows the key's last four
  characters.
- OMNI-59 (M23): the test suite ignores `SIS_*` variables, the Anthropic key
  and AWS credentials exported in the shell, so a shell set up for a real run
  no longer points `pytest` at real adapters or real spend; a test opts in with
  the `real_env` fixture.
- OMNI-58 (M22): the Serve canary's blue serves the target as merged on the
  base branch, not the local file, so a `ServeCloud` rebuilt after a restart
  no longer falls back to code older than what was merged.
- OMNI-184: this changelog; the 2026-10-31 code and documentation review
  recorded as L61–L68 (OMNI-176–183); the empty `identifier.db` removed and
  `*.tfplan` ignored.
- **OMNI-131: the agent reviews its own feature before its PR opens.**
  `sis/review.py` (POLICY-FORBIDDEN) reviews the whole diff against the base.
  Deterministic rules always run: the target must be a SOFT path, and the
  diff may add no I/O or egress import or call. With a real proposer, a
  reviewer model runs too, behind its own LLM interface, with a
  schema-checked reply that fails closed. No PR opens without an approving
  review, which is recorded on the PR and in the provenance graph. A review
  that asks for changes sends the head back: the next cycle revises it
  against the base (neutral `review_requested`). After
  `loop.review_max_rounds` (default 3) the feature is given up as a counted
  `review:` failure, with no PR. The review's cost is charged with the step.
- OMNI-53 (M13): the operator console's GitHub OAuth is installed. The
  provider, credentials, a cookie secret and signed sessions are passed to
  `pn.serve` as arguments; setting `pn.config` had left the server on
  `NullAuth`, where an unsigned websocket session skipped the login. The
  session also re-checks its user against the allowlist when the page is
  built and before every save.

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
