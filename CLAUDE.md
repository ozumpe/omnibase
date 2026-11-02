# Self-Improving Server (Python + Ray) — "omnibase"

Detailed brief: @DESIGN.md — read it before starting.
Actor roles, external subsystems & the self-model: @ACTORS.md
New to the code? `docs/CODE_TOUR.md` (Python + Ray walkthrough). Contributor flow: `CONTRIBUTING.md`.
Architecture diagram: `ray_self_improving_control_loop.svg` (this folder).
Class 2 (feature construction) design: `docs/CLASS2_CONTRACT.md`. What comes after
Class 2, toward omnitrack: `docs/OMNITRACK_VISION.md`. How work is promoted
(feature → develop → main, epic OMNI-128) and the sandbox worker behind hot
deploys: `docs/STAGED_DELIVERY.md`.

**Naming:** *omnibase* = this engine (the `sis/` package; `sis` is just the import handle).
*omnitrack* = the future end product (modeling an external slice of the world).

## What this is
A server that models a slice of the real world via an actor hierarchy and can extend
itself by generating, validating, and safely deploying its own code from high-level
specs. Bootstrap goal first: make the server itself self-improving on a trivial
internal target before it models anything external.

## Stack (decided — don't relitigate without reason)
- **Python 3.14, standard CPython — NOT free-threaded.** Ray ships `cp314` wheels but
  not `cp314t`, so a `python3.14t` env can't install Ray. Managed with **Poetry** (`uv`
  works too). Keep this env separate from the unrelated Spark/py4j work project on 3.11.
- Ray for actors, Ray Serve for serving/canary rollouts.
- License: Apache 2.0 (`LICENSE` committed). Never copy Akka source code.

## Architecture (detail in @DESIGN.md; full role spec in @ACTORS.md)
- CEO/CTO/PM + SWE/QA/DevOps/Designer = Ray actors (leadership are named & detached);
  `Workspace` + `SelfModel` are named detached actors holding shared state.
- Actors coordinate through durable artifacts (Confluence/Jira/Git), not just in-memory
  messages — the work queue, message bus, audit trail, and long-term memory. Handoffs
  are artifact state changes.
- External subsystems behind ports/adapters (`sis/ports.py`): Document Store=Confluence,
  Work Tracker=Jira, Version Control=GitHub, Cloud=AWS, Telemetry, Notifier=AWS SNS
  (OMNI-62). Default = in-memory
  adapters; real adapters in `sis/adapters_real.py` (`SIS_ADAPTERS=real`).
  **The sixth port is the `Sensor`** (OMNI-31): it reads the modelled world.
  A `Reading` is an artifact with a unit and **two timezone-aware times**,
  `event_time` (the period the value describes) and `known_at` (when it
  became available); a replay that hands a model a value before its
  `known_at` hands it the future. Every read names its `TimeAxis`: there is
  no default. `SimSensor` replays a canned trace and is the default
  (`sensor.backend`, a `forbidden_` key: a loop that could pick the simulator
  would pick its own exam). A consumer is given its sensor as an argument,
  never looks it up inside an actor.
  **Scenarios are generated, seeded and refute only** (OMNI-34): a supply
  shock, an outage, a national price move, a demand shock, plus a missing
  week, a re-publication and a withdrawal, from `specs/scenarios.py` (the
  pinned copy; `runtime/scenarios.py` is the SOFT working copy nothing judges
  with, D7). `sis.scenarios.sensor(kind, seed=...)` replays one through
  `SimSensor` like a trace; the invariant gate draws them
  (`fuel_scenario_states`). Their readings carry source `sim`, which a
  backtest fixture refuses: simulation may reject a candidate, never promote
  one (D4). No adapter may declare it (`SourceRules` refuses it in any case,
  OMNI-167).
  **Prediction error is computed and reported, never acted on** (OMNI-36,
  `sis/prediction_error.py`, POLICY-FORBIDDEN). At the moment each week's
  California price first became known, the predictor is shown the as-of view
  (`replay.as_of`), so never a value before its `known_at`, and asked next
  week's price; the signed error (predicted − actual, the next week's latest
  vintage) goes to telemetry (`prediction.error`, `prediction.summary`) and,
  through `publish(record=...)`, the SelfModel's provenance. A week with no
  value to compare with is skipped, never scored, and so is a week whose next
  value was already known when asked (a skipped capture; M34, OMNI-170). The
  predictor is a SOFT target and runs in a `SandboxWorker`. The baseline is persistence, the
  Class-2 contract `ca_price_persistence` (`specs/ca_price_persistence/`,
  `runtime/ca_price_persistence.py` absent until built); it declares no
  backtest, since persistence would fail one in the jump week.
  `scripts/prediction_error.py` replays autumn 2022 from the EIA trace, or a
  scenario (`--scenario KIND --seed N`), and prints the errors. Nothing starts
  a cycle from it: an error budget and skill against climatology are Phase B's
  triggers (D9).
  **The real sensor is `EIASensor`** (OMNI-33, `sis/adapters_real.py`): EIA's
  bulk file `PET.zip` (no key), five weekly series (PADD 5 gasoline stocks;
  California, Los Angeles, San Francisco and US retail prices) and, since
  OMNI-163, the daily Los Angeles RBOB spot price. **The spot price is
  Thomson Reuters' data under licence to EIA** ("Protected materials", checked
  by Olaf on 2026-10-30): it may be modelled, never redistributed. It is
  `redistributable=False`, the trace is gitignored, and a public fixture shows
  only `EIA_PUBLIC_SERIES`; one that needs spot is private: its episodes in
  `specs/<name>/private/` (gitignored, kept in the artifacts bucket), their
  sha256 in the committed `private.json`, which the backtest gate checks
  (`backtest.private_backtests`).
  `scripts/capture_sensor.py` fetches it and appends to an append-only trace,
  `runtime/sensor_traces/eia.jsonl`, in D12's capture format
  (`sis/sensor_trace.py`, POLICY-FORBIDDEN). `read()` answers from the trace
  alone, through the door again; `absent()` shows what EIA stated no value
  for, so a value it later blanks is visible as withdrawn. A changed value is
  a new line with a later `known_at`, never an overwrite. The first capture of a series backfills
  it, each `known_at` reconstructed from EIA's release schedule and marked
  `reconstructed`: Thursday 17:00 ET for Friday stocks, Wednesday 17:00 ET for
  Monday prices, after both the 2022 rule (Monday, Tuesday after a holiday) and
  today's (Tuesday, Wednesday after a holiday; Tuesday was a day early, OMNI-161).
  That schedule is wrong for an irregular delay: the stocks
  for the week ending 2022-06-17 came out 12 days later, and the prices for
  Monday 2022-06-20 on the same day (2022-06-29). So OMNI-35 checks its window
  against EIA's release archive. Later values are known at the fetch that
  first saw them. Read-back also checks each value line against its capture
  (in its block, known no later than the fetch, the block whole), under a
  shared lock, so a read never sees half a capture (OMNI-159). Each capture
  must be fetched after the last, or it is refused: two at one instant, or a
  clock stepped back, would leave revisions a replay cannot order (OMNI-160).
  It cannot
  catch a forgery consistent with itself; the human promotion into `specs/`
  is the guard there. No trace is written under `specs/` (checked by inode,
  so `SPECS/` on a Mac is caught too). EIA's terms were
  checked by Olaf on 2026-10-13 (public domain, an acknowledgment dated by
  publication, no modified content labelled EIA's).
  **Sensor input is untrusted, and comes in through one door** (OMNI-32):
  `sensor_input.sanitise`. Generated code was the untrusted thing; a real
  sensor adds data an outside party writes, and it reaches fixtures,
  scenarios and possibly a prompt. Every record a real adapter fetches
  becomes a `Reading`, is stated `Absent` (a marker such as `W` or `NA` is no
  value, never zero), or is refused with a reason: the wrong shape, a series
  the adapter did not declare, a number that is not plainly one, a value
  outside its series' plausible range, a time without a timezone, a value
  known before its period began, a field or a batch over its size. Nothing is
  dropped without a count (`Sanitised.counts()`). A rejection's text shows
  only a few harmless characters of what the source sent. Free text is
  `UntrustedText`, with no raw `str()`: formatted, it is marked and inert,
  so it cannot close a docstring or leave a string; `.literal()` is its
  `repr`, for an expression of its own. A real adapter builds no `Reading`
  itself (pinned by a test).
- SelfModel (digital twin): live actor registry, deploy slots, provenance graph,
  substrate — the first "piece of the world" the system models.
- Episodic store (`sis/episodic.py`): every cycle's outcome behind a port —
  spec → diff → gauntlet verdict → outcome, incl. every rejected diff and the
  gate that caught it. Backends via `SIS_EPISODIC_STORE`: `jsonl` (default),
  `duckdb` (SQL analytics), `none`. This is the dataset the system learns from;
  Postgres+pgvector can drop in later as another backend.
- Control loop: monitor → budget/goal gate → propose Python on a branch → validation
  gauntlet → canary → promote/rollback → log → circuit breaker.

## Hard rules
- Generated/untrusted code NEVER runs in the main process. The gauntlet sandboxes every
  gate: `SIS_SANDBOX=subprocess` (scrubbed env + in-process egress block, default) or
  `docker` (kernel-enforced `--network none`, no creds, only the temp dir mounted,
  run as the host user's uid — never root; see KNOWN_ISSUES "Resolved"). A
  per-gate timeout kills infinite loops. A real (non-stub) proposer writes untrusted code
  and REQUIRES `SIS_SANDBOX=docker` — the loop refuses otherwise (override:
  `SIS_ALLOW_UNSANDBOXED_LLM=1`); the subprocess sandbox leaves host files readable (M1)
  and cannot confine a benchmark worker to one CPU (H9).
  **A Serve replica never runs the candidate it serves** (OMNI-48, H3 fixed
  2026-10-27): a replica is an ordinary Ray worker, so `TargetDeployment`
  starts a `SandboxWorker` for its source and forwards each request over the
  pipe. The driver's sandbox settings are bound into the deployment
  (`serving.sandbox_settings`), because green's scrubbed environment would
  otherwise fall back to `config.yml`'s mode. Only docker keeps the candidate
  off the cluster: in the soft sandbox Ray's gRPC client bypasses the egress
  block (M1 again). `canary.backend=serve` is **still refused** with any
  non-stub proposer, with **no** override, until a human decides to lift it
  (OMNI-49, M19; `gauntlet.serve_canary_problem`, checked at startup, per
  cycle, and in `ServeCloud.deploy_canary`).
  **Hot deploys go through the sandbox worker** (`sis/sandbox_worker.py`,
  OMNI-129): the candidate is served from the gauntlet's own sandbox over
  stdin/stdout, so it needs no network. The host times each exchange and
  JSON-decodes each answer, so neither can be faked from inside. One deadline
  covers sending a request and reading its answer, so a worker that stops
  reading its input is killed like one that never answers (OMNI-155, H10).
  Its stderr is drained as it comes and only an 8 KiB tail is kept, never a
  host-side file; sandbox containers run with `--log-driver none`, so docker
  keeps no copy either (OMNI-156, H11). The Serve canary's
  replicas serve through it too (OMNI-48).
  **A broken sandbox is never blamed on the candidate** (OMNI-37): when a
  sandboxed gate fails, the gauntlet first runs a trusted self-check in the
  same sandbox (`gauntlet.probe_sandbox`); if that fails too, the verdict is
  `harness:`. Docker exit codes 125–127 are reported but never trusted on
  their own — docker passes a container's exit code through, so a candidate
  could exit 125 to launder its failure. A harness fault still counts in full
  toward the breaker (a broken sandbox fails every cycle); only the bug's
  attribution changed.
- **The contract selects which gates run** (`Contract.gate_profile()`,
  `sis/gauntlet.py`); both task classes flow through one `validate()`:
  - **Class 1** (`OptimizationContract` — make a working function faster):
    `ast.parse` → no-op check → `mypy --strict` → interface → the contract's
    acceptance tests → invariant gate → backtest gate → differential
    correctness on random inputs (anti-gaming) + benchmark vs a freshly
    measured baseline (must clear the contract's margin, ≥10% faster by
    default). **The benchmark never decides on one comparison** (OMNI-41):
    candidate and baseline are timed back-to-back on the *same fresh* input
    (alternating order, seeded, never reused — a replayed workload rewarded
    `functools.cache`, not speed), plus each `BENCH_INPUTS` entry timed at most once
    for shape coverage. `gauntlet.benchmark_decision` (pure) decides on
    **total cost** — `sum(candidate)/sum(baseline)` with a paired-bootstrap
    interval — never a per-input median (a candidate fast on typical inputs
    and 4x slower on large ones passed a median rule while ~2x slower
    overall): **accept** if the interval clears the margin, **reject** if the
    point estimate misses it, else **inconclusive** — reachable only by a
    candidate that *looks* faster, and neutral like a no-op at both the SWE
    and QA stage (`episodic.neutral_status`: no bug, no breaker increment,
    spend recorded). **The candidate never shares a process with the clock,
    the reference or the verdict** (OMNI-45, H2 fixed 2026-09-29): candidate and
    baseline each run in a `SandboxWorker`, the oracle runs on the host from
    `specs/`, and every exchange is timed on the host and decoded from JSON.
    Exchanges carry a batch of fresh inputs, sized by time so the baseline's
    batch takes ~20 pipe round trips (`_WINDOW_OVER_ROUND_TRIP`; ~20 µs a round
    trip in the subprocess sandbox, ~350 µs in docker on a Mac). **No input
    is timed twice, and none is timed after a worker has met it** (OMNI-152,
    H7 fixed 2026-10-08; `gauntlet._UnusedInputs`, `gauntlet._timed_work`):
    not in another timed batch, a sizing exchange or `BENCH_INPUTS`, and not
    after the candidate met it in the differential phase. Inputs are compared
    with `==`, at least as coarsely as a cache could key them. A fresh draw
    each time was not enough: 99 batches of 1024 from `sum_of_divisors`'
    19,999 values repeated four calls in five, and a cache measured as speed
    (AWS run #7 ran at that size). The timed inputs are set aside before each
    sizing exchange. When the oracle's range cannot fill the batch the timing
    asks for, the batch shrinks (`capped by the oracle's input range` in the
    reason). A range that cannot give every sample one unused input is the
    exam's fault, a `harness:` verdict that pages a human: with 300
    differential trials, a range under about 300 values.
    **A timed batch runs no Python per call in the worker** (OMNI-153, H8
    fixed 2026-10-08). The worker's loop is trusted code in the candidate's
    interpreter, so whatever it did once per call the candidate could make
    cheaper for its own worker: the same algorithm plus a patched `json.dumps`
    measured 0.78. Now the loop decodes the request, runs the calls through
    `itertools.starmap` and encodes the reply, three C calls for the batch
    (`sandbox_worker_main._plain_batch`), and answers `{"id": n, "ok": [...]}`
    when every call returned. Keep it that way: no per-call Python on the
    timed path. The loop's per-call cost is ~0.17 µs (was 1.24). It cannot be
    amortised and dilutes a gain rather than invents one, so differences well
    under a µs are resolved poorly — accepted. **Each benchmark worker has
    one CPU, and a different one** (OMNI-154, H9 fixed 2026-10-08). The clock
    is a wall clock and a batch is many calls, so a candidate that took over
    its worker's loop spread the batch over several cores: the same algorithm
    measured 0.50, and 0.76 to 0.81 in docker's two CPUs. In docker each
    worker's container is confined by `--cpuset-cpus`, which cannot be left
    from inside (`--cpus` is a quota per 100 ms; a burst of a few hundred µs
    on every core never reaches it). The CPUs come from a trusted container
    (`gauntlet._sandbox_cpus`), differ so a candidate that spins does not take
    the baseline's time (`gauntlet.benchmark_cpus`), and are swapped halfway
    so a busier CPU counts against both sides. **The subprocess sandbox
    confines nothing** and is refused for a real proposer (M1).
    Every timed answer is checked against the baseline's, and differences
    against the reference, so a candidate cannot be wrong only while timed.
    M7 — false-accept above nominal
    under stalls — is won't-fix for now (OMNI-88). Too few usable timings is
    `benchmark unmeasurable`, a counted failure, never neutral.
  - **Class 2** (`FeatureContract` — build what a spec describes, no
    pre-existing version to diff against): `ast.parse` → `mypy --strict` →
    interface → acceptance → invariant gate → backtest gate → SLO gate. No
    no-op (nothing to be identical to) and no differential/benchmark (no
    reference exists, and "faster" isn't what makes a feature correct).
    **The loop builds one** (OMNI-147; `--contract roman`, the only one
    shipped). The feature is done at its first step that passes every gate:
    that step is committed and the PR (`Build <contract>: 1 step`) opens at
    once. Rejected attempts are notes on the feature even before it has a
    branch, so the next prompt carries every reason. The prompt is built
    from the spec (`proposer._build_prompt`): the public API, the acceptance
    tests, and the laws with the module that defines them; held-out backtest
    fixtures are never shown. A feature already built is judged again at the
    next cycle, before anything is planned (OMNI-149): passing, it is the
    neutral `no_gain` ("already built"), filing nothing, so the loop's
    convergence stop ends the run; failing (its spec changed), the
    reason starts a rebuild. No timings anywhere, and `--canary serve` is
    refused for a Class-2 contract.
  - **SLO gate** (`sis/slo.py`, OMNI-24): an optional latency *budget* from
    the spec (`FeatureContract.slo = DomainSLO(...)`), explicitly **not** a
    correctness gate — it runs last, only on an already-correct candidate, and
    rejects under its own reject gate `slo` ("correct but over budget"). Fixed
    workload (literal `inputs` or a named `workload` function in the
    contract's oracle), best of up to `max_repeats` per input stopping at the
    first run under budget, nearest-rank percentile vs the budget. The CEO
    counts an `slo` rejection at `brakes.slo_failure_weight` (default 0.5)
    toward the consecutive-failure breaker; a candidate that *raises* on the
    workload is `slo_error` and counts in full. Class 1 has no SLO — its
    benchmark already answers the performance question. No shipped contract
    declares one yet.
  - **Invariant gate** (`sis/invariant.py`, Hypothesis-generated inputs) and
    **backtest gate** (`sis/backtest.py`, recorded-episode fixtures under
    `specs/<name>/`, held-out split) are the anti-gaming layer for targets with
    no reference oracle to differ against — domain laws over generated inputs,
    and "does it reproduce recorded reality", respectively. **A forecast must
    also beat a baseline on average** (`BacktestSkill`, OMNI-163): a contract
    may name a baseline in its oracle (persistence) and a margin, and the
    candidate's mean absolute error over the held-out episodes must be at least
    that far below the baseline's. A per-episode tolerance cannot separate the
    two, because both miss a jump week alike. The baseline runs in the harness
    on its own copy of the arguments, and a rejection shows neither error (M31).
    Its first user is `ca_price_wholesale` (OMNI-163): next week's California
    price from the LA spot price, stocks and the season, judged on every week
    from 2020 (each within 15%, and a mean absolute error at least 10% below
    persistence's). Its fixture is private, so the contract is registered only
    once `specs/ca_price_wholesale/private.json` is promoted; a malformed one
    makes only this contract unavailable, with the reason (OMNI-176).
  - **Candidate output is compared only as plain values, and never shares an
    input object with the other side** (OMNI-46/47). `sis/canonical.py`
    rebuilds output from exact builtins before every `==` — differential,
    acceptance (via a gauntlet-written `conftest.py` wrapping the contract's
    `public_api`), invariant, backtest, and the live canary — because a return
    type with its own `__eq__` passed every gate (H4). Each side gets its own
    `copy.deepcopy` of the input, made outside any timed window, because a
    candidate that emptied a shared list "agreed" with the reference, and one
    that grew it slowed the baseline (M10). Neither stops a candidate that
    tampers with the harness in its own process — that took OMNI-45 and
    OMNI-146.
  - **The exam is the candidate's to read, not to write** (OMNI-45 slice 1,
    M9/M8/M11). The candidate, oracle, baseline, tests, canonicaliser and
    fixtures live in one directory, the sandbox's `HOME`/`TMPDIR`/tool caches in
    another, off `sys.path`; docker mounts the first `:ro`. In both sandboxes
    every trusted file goes through `_GateContext.put`, which records its
    sha256, and after **every** gate — passed or failed — `tampering()`
    compares the directory with that record (modified, added, removed,
    symlinked or special files). A difference rejects the candidate as
    `tampered:` (episodic gate `tampering`) before anything else is concluded:
    the exam is on `sys.path`, so a planted `json.py` would shadow the standard
    library. A zero exit is no verdict: the interface, invariant and backtest
    scripts print a per-run token that the harness must see, and the acceptance
    gate needs pytest's own `N passed` line (`gauntlet._ended_without_verdict`).
    A candidate's own exception (or `sys.exit`) inside the invariant or backtest
    gate is a counted violation (`canonical.guard_exports`), never
    `harness: … crashed`.
  - **No gate shares a process with the candidate it judges, except SLO**
    (OMNI-146, M8). The acceptance, invariant and backtest gates run their
    trusted code (pytest, Hypothesis, the backtest replay) on the host in a
    harness process under the exam (`gauntlet._run_harness`), where `target`
    is a stand-in (`sandbox_worker.install_proxy`) that sends each call to the
    candidate's own `SandboxWorker` and returns the JSON-decoded answer. The
    interface gate asks a worker what the candidate exports. So the token, the
    pytest summary and the exit code come from a process the candidate never
    runs in. The stand-in keeps what an in-process call would show: an
    exception arrives as the nearest builtin in its hierarchy, a changed
    argument list is mirrored back, and a `SystemExit` is a plain failure. Only
    JSON crosses (a returned tuple becomes a list; an argument JSON cannot carry
    is `NotWireable`, the exam's fault). The acceptance tests' own
    `target.<name>` references are served too (`_names_the_tests_use`). **The
    SLO gate** still runs its candidate in-process; no shipped contract declares
    an SLO yet (L42, OMNI-86).
  - Every gate ends in a human PR. Generated code MUST be fully typed. What
    counts as correct/better is per-target — see `sis/contract.py`.
- **Change-authorization policy (`sis/policy.py`) — what the loop may rewrite:**
  - FORBIDDEN (never, no override): guardrail/safety code — the gauntlet and
    its output canonicaliser (`sis/canonical.py`), the hot-deploy sandbox
    worker (`sis/sandbox_worker.py`, `sis/sandbox_worker_main.py`), the contract layer (`sis/contract.py`, `sis/backtest.py`, `sis/invariant.py`,
    `sis/slo.py`, `sis/clock.py`), the ports with the `Sensor` and the shape
    of a reading (`sis/ports.py`, OMNI-31), the door sensor data comes in
    through (`sis/sensor_input.py`, OMNI-32), the capture trace
    (`sis/sensor_trace.py`, OMNI-33), what a replay shows a model and the
    fixtures built from it (`sis/replay.py`, `sis/fixture_builder.py`,
    OMNI-35), which scenario generator drives a twin (`sis/scenarios.py`,
    OMNI-34; the generator itself is `specs/scenarios.py`), how a predictor's
    error is computed (`sis/prediction_error.py`, OMNI-36), the contract-author approval gate
    (`sis/contract_author.py`),
    `specs/` (the exam itself — oracles, acceptance tests, domain laws, backtest
    fixtures), cost accounting (`sis/cost.py` — the breaker/threshold logic
    itself lives in `sis/roles.py`, which is only STRICT; see KNOWN_ISSUES
    L43), settings/secrets, **the configuration
    (`sis/config.py` + `config.yml`, OMNI-27)**, the adapters,
    `Dockerfile.gauntlet`, and the policy itself.
  - STRICT (off-limits unless `policy.allow_strict_changes` /
    `SIS_ALLOW_STRICT_CHANGES=1`, then needs human approval + justification +
    passing checks): all other engine code.
  - SOFT (optimisable; checks + review): the designated target(s) —
    `runtime/target.py`, `runtime/sort_target.py`, `runtime/roman.py` (Class 2,
    deliberately absent until an implementer writes it), `runtime/scenarios.py`
    (the scenario generator's working copy, OMNI-34, D7),
    `runtime/ca_price_persistence.py` (OMNI-36's predictor, Class 2, absent
    until built), `runtime/ca_price_wholesale.py` (OMNI-163's, likewise),
    extendable via
    `policy.target_paths` / `SIS_TARGET_PATHS`. **Guardrail classification always
    wins** — and that ordering is now load-bearing rather than tidy, because the
    target list is itself a config key: "name the config as a target, then
    rewrite the spend cap" is inert only because `classify` checks the guardrail
    list first. Asserted in `tests/test_config.py`.
- **The one write path into `specs/` is human-approved** (`sis/contract_author.py`,
  OMNI-21/26): a spec drafts into `runtime/contract_staging/` (loop-writable, no
  gate reads it), and `promote()` raises `RequiresHumanApproval` with no
  override. **What is promoted is what was reviewed** (OMNI-68): staging writes
  a manifest of every file's sha256, and a human runs `python -m
  sis.contract_author review <name>`, then `promote <name> --digest <digest>
  --reason "..."` (audited); a digest or file that changed is refused. Worked examples stated in a spec transcribe into real assertions
  (values only, `ast.literal_eval` never `eval`); prose criteria and domain laws
  draft as `NotImplementedError` stubs, because the system cannot invent
  domain laws. A discrimination check runs the drafted exam against a null
  implementation before a human sees it, so an exam that asserts nothing is
  flagged rather than silently approved.
- Branches: `feature/*` → `develop` → `main`. Never push to `develop`/`main` directly
  (see workflow below).
- Secrets: `secrets.local.yml` (gitignored) locally; AWS Secrets Manager in cloud
  (`SIS_ENV=aws`). Never commit tokens.
- Destructive Jira/Confluence/GitHub actions and PR merges require human approval
  (`RequiresHumanApproval` in the adapters). **Canary→live promotion follows an
  *observed* merge** (OMNI-15): `Cloud.promote()` no longer raises, because doing
  so unconditionally made promotion unreachable rather than gated. Its only
  caller is `DevOps.observe_merge()`, which promotes solely when the PR reads
  back as `merged`. The agent cannot manufacture that — `merge_pr()` still raises
  in every adapter and `Workspace` exposes no merge-shaped method at all (both
  asserted by tests). It applies a human's decision; it never makes one.
- Hard LLM spend cap + cost-per-accepted-improvement SLO + circuit breaker (CEO brakes).
  **Brake state fails closed** (OMNI-61): written atomically (`sis/atomic.py`);
  a state file that exists but cannot be read boots the CEO with the breaker
  *open* and a named reason, and is never overwritten; `episodic.store=none` is
  refused with a real proposer or real adapters (no override — `jsonl` costs
  nothing). Operators pause/resume/reset through `python -m sis.admin`, each
  change with a written `--reason`, audited; a reset never touches spend.
  **Spend is reserved and saved before it is spent** (OMNI-157, H12): after
  the budget gate the CEO reserves the cycle's estimate, and the state is
  saved before the cycle goes on; the outcome replaces the reservation with
  the real cost. A save that fails, before or after, opens the breaker and
  pages, and a reservation no outcome replaced (found at boot, or still
  outstanding at the next reservation) counts as spent and as a failure. A failed save used to warn and go on, so a restart forgot the
  spend since the last save. **An older snapshot never replaces a newer one**
  (OMNI-158, H13): the loop and `sis.admin` both save the CEO's state, so the
  CEO numbers it (`version`, one more per change, within a `lineage` that a
  CEO started from unreadable state begins afresh) and the store keeps the
  higher version, checking and writing under one lock (jsonl) or transaction
  (DuckDB).
  **An exception inside a cycle is an outcome, and its spend is charged**
  (OMNI-55, M16 fixed 2026-10-08). The cost of a proposal used to reach the
  driver only in `SWE.implement`'s return value, so a 403 at `open_pr` lost
  it: the cap never saw it and `--loop` died. Now the SWE's step never leaves
  by raising once a proposal was asked for; it returns the cost with the
  error. An error the proposer's own call raised (a rejected key, a rate
  limit) is labelled "in the proposer, while asking for the proposal", not
  "after the proposal" (OMNI-175). `org.run_cycle` records anything else that raises as status `error`:
  charged, counted toward the breaker in full, a bug filed, a human paged, a
  canary set by that cycle retired. Only the refusals before any spend still
  raise, and a CEO that cannot be told: a loop that cannot reach its brakes
  must not go on.
  **One loop per box** (OMNI-151, M28): `main.py` holds an exclusive `flock` on
  `runtime/loop.lock` for the run, and a second start exits 3 naming the
  holder. Two loops would each restore their own CEO from one state file, so
  neither would enforce the cap for both (AWS run #7).
  **A trip pages a human** (OMNI-62), not only files a TES bug nobody watches:
  the `Notifier` port (SNS topic `adapters.notify_sns_topic_arn`, `sns:Publish`
  on that one topic) pages on a breaker trip, a spend-cap refusal, a broken
  sandbox, a loop that stops, and a boot from unreadable brake state. A failed
  page never breaks the loop; it is printed, emitted, and kept in the episodic
  store. Without a topic a real-adapter run warns, and the AWS preflight fails.

## Repo & GitHub workflow
- Repo: **github.com/ozumpe/omnibase** (public since 2026-08-06). `gh` CLI is
  authenticated (account `ozumpe`, SSH). **Default branch: `develop`.**
- Flow: **`feature/<name>` → `develop` (integration sandbox) → `main` (tested releases).**
  Open PRs with `gh pr create --base develop`. CI must pass before merge; self-merge is
  fine (0 required approvals).
- **Never push to `develop`/`main` directly.** Client-side guard: `git config
  core.hooksPath hooks` enables `hooks/pre-push`, which blocks it (emergency override:
  `git push --no-verify`). Server-side rulesets are also active on both branches
  (GitHub Pro; ruleset ids `17153780` main, `17153781` develop) — the client hook
  is belt-and-suspenders, not the sole enforcement.
- **Every commit needs a Jira key or an explicit opt-out.** Same `core.hooksPath
  hooks` setting enables `hooks/commit-msg`, which rejects a commit message with
  neither an `OMNI-N` key nor a `No-Ticket: <reason>` trailer line (same style as
  `Co-Authored-By:`; emergency override: `git commit --no-verify`). A trailer, not
  free text like `[no-ticket]` — free text collides with prose *about* the marker
  (this hook's own commit needed to document it, which would otherwise trip the
  bypass). Merge commits and `fixup!`/`squash!` commits are exempt. The hook is
  client-side, so the `commit-lint` CI job below is its server-side backstop for
  when it is bypassed or not enabled in a clone.
- CI (`.github/workflows/ci.yml`): `ruff` + `mypy --strict` + `pytest`. Runs on
  every push to `main`/`develop` and on **any PR regardless of base** (OMNI-16,
  fixed 2026-08-09) — the `pull_request` trigger used to filter on `[main,
  develop]`, which matches the PR's *base*, so a PR stacked on a feature branch
  skipped CI entirely and could merge looking green with zero verification
  (what happened to #67/#68). The required status-check context is `test`.
  **Layout (OMNI-60):** `changes` classifies the diff; `lint` (ruff + mypy),
  `fast` (default pytest) and `serve` (`-m serve -n 0`) then run as parallel
  jobs, all skipped on a **docs-only PR** (only `.md`/`.svg`/`.png` —
  an allowlist, so anything else runs everything), which runs `docs`
  (`pytest -m docs`: the tests that read the repo's Markdown) instead. `test`
  is an aggregator that always reports and passes only if the jobs *this*
  change needed succeeded — skipped counts as missing, not passed. A push to
  `main`/`develop` always gets the full run. No `paths-ignore`: a skipped
  workflow never reports `test`, and the PR waits on it forever. Decisions in
  `scripts/ci_gate.py` (pure, `tests/test_ci_gate.py`); shape pinned by
  `tests/test_test_layout.py`. **A test that reads repo Markdown must be
  marked `docs`** — the layout test flags an unmarked one.
- Commit-lint (`.github/workflows/commit-lint.yml`): every non-merge commit newly
  introduced by a PR needs an `OMNI-N` key or a `No-Ticket:` trailer — the
  server-side backstop for the Jira-key convention above, for when
  `hooks/commit-msg` is bypassed or not enabled in a given clone. Runs on a PR
  against any base, same as `ci.yml` now. Required status-check context:
  `commit-lint`. **Except the release PR** (this repo's `develop` → `main`,
  OMNI-63): its commits were checked on the way into `develop`, or predate the
  rule — v0.2.0 carried 25 July commits from before it existed. Same-repo only,
  so a fork's branch named `develop` is still checked.
- After ANY `pyproject.toml` change, run `poetry lock` — CI fails on a stale lock file.
- `pyproject.toml`'s `version` tracks the last git tag (bump it as part of cutting
  a release, e.g. `git tag v0.1.5`); it had drifted to `0.1.0` across five
  releases (OMNI-22) before being corrected to match `v0.1.4`, the last actual
  tag — unreleased work on `develop` has no version number until its own tag.

## Conventions
- `ruff`, `mypy --strict`, `pytest` — all green before a PR (also CI + gauntlet gates).
- Keep decision logic in pure functions (e.g. `evaluate_brakes`, `policy.classify`) and
  I/O in the Ray actors, so logic is unit-testable without standing up Ray.
- Every bug found becomes a permanent regression test (see `tests/test_adversarial.py`).
- Small reviewable PRs. Provenance: prompt → commit → PR → ticket → outcome.
- **Every commit and PR title touching planned work references its Jira key**
  (e.g. `OMNI-6: contract-derived proposer prompt`) — anywhere in the message,
  first line preferred. This is what lets the GitHub-for-Jira app populate the
  issue's Development panel automatically; without the key present, nothing
  links. Ad-hoc fixes with no OMNI story don't need one.

## Operational quick reference
- Run a cycle: `poetry run python main.py` (in-memory, no creds).
- Gates: `poetry run pytest` · `poetry run mypy --strict sis/ main.py scripts/` · `poetry run ruff check .`
- **`pytest` defaults to `-m "not serve" -n auto`** (fast inner loop, ~50s,
  parallel): it deselects the Ray Serve integration tests
  (`test_live_canary.py`, `test_serve_cloud.py`, `test_serving.py`,
  `test_loadgen.py`, `test_loop_serve.py`, 63 tests), which stand up a real
  cluster/Serve deployment and take minutes serially. **Not a full verification
  by itself** — run `poetry run pytest -m serve -n 0` (serial: they share a
  cluster and a port) before trusting a change touches Serve, or let CI run
  both halves, which it always does explicitly (`tests/test_test_layout.py`
  pins that it must, so the excluded half can never silently run nowhere).
- **Writing a Serve test: `deploy_canary` attaches green at
  `DEFAULT_CANARY_WEIGHT` (5%), not 0.** A test that asserts blue answered
  must `shift_traffic(v, 0.0)` first, or it fails one run in twenty.
  `test_promotion_makes_the_candidate_the_new_baseline` did exactly that
  (`'green-answer' == [1, 2, 3]`, three CI failures, fixed 2026-09-26). It
  looked like a replica race after a redeploy, but Ray's replica logs show
  the failing request going to green through the router that had just
  attached it — check those logs before adding a wait.
- Optional deps: `poetry install --with llm` (anthropic) · `--with real`
  (requests/boto3/pyyaml) · `--with analytics` (duckdb) · `--with ui` (panel).
- Operator UI (OMNI-28): `poetry run python -m sis.frontend`. Locally,
  `SIS_FRONTEND_AUTH=none` on the default loopback bind needs no OAuth app —
  GitHub exempts `localhost` from the HTTPS redirect rule. It refuses to start
  unauthenticated on any non-loopback bind. Panel is only partly annotated, so
  it is `follow_imports = "skip"` in the mypy overrides: otherwise the type
  gate would mean something different on a machine that has the `ui` group
  installed than in CI, which does not.
- Twin viewer (OMNI-40): `poetry run python -m sis.viewer` replays the
  autumn-2022 fixture (`specs/ca_price_next_week`) week by week; `--trace
  PATH` replays a capture instead, and a second tab drives generated
  scenarios (hatched, labelled SIMULATED, D4). It draws the chain as a tank
  and pumps, with refineries and demand drawn empty (no adapter yet), and a
  toggle between what was published by then and everything. What to show is
  pure (`sis/twin_view.py`); `sis/viewer.py` only loads and draws. It is
  read-only and never reaches Ray (pinned by `tests/test_viewer.py`), asks
  the predictor in a sandbox worker, and is served under the console's
  `frontend.*` rules on the console's port plus one. Reach it as
  `localhost`, not `127.0.0.1`: Bokeh's websocket refuses the other origin.
- **Configuration is one file: `config.yml`** (OMNI-27), generated from the
  schema in `sis/config.py` — every key with its default, doc, env var, and CLI
  flag. `poetry run python main.py --show-config` prints the effective values
  **and where each came from**. Don't restate defaults in prose anywhere: this
  list used to be a hand-maintained table in `README.md` and had already drifted.
  - Precedence: **CLI flag > env var > `config.yml` > built-in default.** Every
    legacy `SIS_*` var still works — it is the env layer.
  - Sections: `brakes`, `sandbox`, `policy`, `episodic`, `adapters`, `proposer`,
    `canary`, `loop`, `sensor`, `contracts`. Each key is prefixed `forbidden_` /
    `strict_` / `soft_`.
  - **The prefix gates the human operator UI (OMNI-28), not the loop.** The loop
    is stopped by `config.yml` and `sis/config.py` both being POLICY-FORBIDDEN,
    at every tier. Naming the config as an optimisation target buys nothing —
    guardrail classification is checked first, and `tests/test_config.py` pins it.
  - Add a knob by adding one `Key(...)` to `SCHEMA`, then
    `poetry run python -m sis.config --write` (which re-renders *around the
    values already in the file*, so adding a key doesn't reset operator edits).
    A test re-renders the committed file from its own values and compares, so
    its **structure** cannot drift from the code. Values may now differ from
    their defaults, because the operator UI writes here (OMNI-28) — except for
    `forbidden_` keys, which a separate test pins to their defaults so that
    deleting `config.yml` can never weaken a guardrail.
  - Secrets are **not** here (`config.yml` is committed) — they stay in
    `secrets.local.yml` behind `sis/settings.py`. `ANTHROPIC_API_KEY` is a
    credential, not a config key.
  - `SIS_CONTRACT`/`SIS_CANARY`'s BEFORE-launch caveat still applies to the *env*
    layer only (actors snapshot the environment at creation); the file and CLI
    layers do reach the actors. Prefer `--contract`/`run_cycle(contract_name=)`.
- Real adapters: `cp secrets.example.yml secrets.local.yml`; then
  `poetry run python scripts/check_connections.py --deep` before a real cycle.
- Operate a running loop: `poetry run python -m sis.admin status|pause|resume|reset-breaker
  --reason "..."` (OMNI-61) — acts on the live CEO, audited to
  `runtime/operator_audit.jsonl`. `pause` idles the loop without tripping anything.
- Docker sandbox image: `docker build -t sis-gauntlet:latest -f Dockerfile.gauntlet .`
- Confluence specs/docs live in the **"Software Development" (SD)** space (Atlassian MCP,
  cloudId `760ca470-0091-4601-9704-a56633b5e9b6`): Architecture, Validation Gauntlet,
  Codebase Guide, Guardrails & Operations, Milestone Roadmap, Risks & Next Steps.
- **Milestone plan lives in Jira: project `OMNI` ("OmniBase"), same cloudId**
  (<https://olafzumpe.atlassian.net/browse/OMNI>). Epics + stories for the next
  milestone; **this is the source of truth for what to work on next.** Move a story
  to Done as its PR merges, and reference the key in the commit/PR
  (e.g. "OMNI-6: contract-derived proposer prompt"). `docs/KNOWN_ISSUES.md` stays
  the canonical record of *defects* (H/M/L IDs); Jira holds *planned work*.
  Note the scratch project `TES` is where the **loop itself** files artifacts during
  live runs — don't put planning there.

## Current status — where to pick up
Latest release: **v0.5.0** (2026-10-30). What each release carried, and what
is on `develop` since, is in `CHANGELOG.md`; the AWS box runs the latest
release tag, not `develop`. **What to work on next is in Jira**
([`OMNI`](https://olafzumpe.atlassian.net/browse/OMNI)): check the board, not
this file. Defects are in `docs/KNOWN_ISSUES.md`. Neither list is copied here,
because a copy is what drifts.

Where things stand (2026-10-31):
- **omnitrack Phase A is done** (epic OMNI-30): the `Sensor` port, the
  sanitising door, `EIASensor` and its trace, the scenario generator, the first
  recorded fixture (`specs/ca_price_next_week`) and the prediction-error report.
  **OMNI-163, the first gated omnitrack contract** (`ca_price_wholesale`, which
  must beat persistence on held-out history), is on `develop`; its private
  fixture still needs a human-checked 2018–2026 calendar and a promotion (L61, L62
  and L63, which stood in its way, are fixed). What comes next, and the settled decision
  register D0–D12, is `docs/OMNITRACK_VISION.md`: read it before designing
  anything the twin touches. Data-source licences and terms are checked by a
  human before any adapter is written (D0). Iowa's spirits supply chain
  (OMNI-39) is a placeholder epic.
- **Staged delivery is where engine work continues** (epic OMNI-128,
  `docs/STAGED_DELIVERY.md`). Built: the sandbox worker (OMNI-129), now behind
  every gate but SLO and behind the Serve canary (OMNI-48), and feature
  branches (OMNI-130, hardened by OMNI-135). Next: OMNI-133 (`develop`
  blue/green), OMNI-131 (the agent's own review), OMNI-132 (AI approval),
  OMNI-134 (dynamic feature size). OMNI-142 (holds per contract) matters only
  once two contracts run at the same time.
- **Open epics from the 2026-09-26 review:** OMNI-43 (gate integrity; left:
  L42, OMNI-86) and OMNI-44 (Serve-canary isolation; left: OMNI-50, M20/M21).
- **Elsewhere on the board:** OMNI-38 (wire the live SLO-breach trigger into
  `main.py --loop`) and OMNI-20 (`ToolchainAdapter`, parked).
- **Not yet scheduled:** the atomic actor swap for internal, never-served
  actors, which `docs/SERVE_CANARY.md` scopes out and which has no design doc
  yet. E3/D2 in `docs/OMNITRACK_VISION.md` either is that design or subsumes
  it, so whoever schedules either writes the one document, and drain/handoff
  machinery is not built twice. D2 (state behind a `StateStore` port) already
  answers its central question.

**Before trusting a gauntlet verdict:** every gate but SLO judges its candidate
from outside the candidate's process (H2, M8), and the exam files are protected
(M9). **The SLO gate still runs its candidate in-process** (L42,
[OMNI-86](https://olafzumpe.atlassian.net/browse/OMNI-86)); no shipped contract
declares an SLO yet. The Serve canary serves its candidate from a sandbox
worker (OMNI-48), but `--canary serve` is still refused with any non-stub
proposer (OMNI-49, no override) until a human lifts that.

**Known issues:** `docs/KNOWN_ISSUES.md` is the canonical, ID'd list (H/M/L);
its High, Medium and Low sections are the open list. Reference the IDs in
commits and PRs. **Every entry starts with its Jira ticket**, open, won't-fix
and resolved alike, and `tests/test_known_issues.py` fails on an entry without
one. File the ticket first, then the entry; when it is fixed, move it to
"Resolved" and add a line to `CHANGELOG.md`.

**Tests.** `poetry run pytest` runs the fast half, the serve half runs
separately (Operational quick reference above), and CI runs both. No count is
kept here: CI's is the one that matters, and a hand-kept one drifted.
**Known test flakes**: re-run, then file or update the ticket; never call a
failure environment noise.
- Open: L58 (`test_fast_on_typical_inputs_but_slower_in_total_is_rejected`,
  OMNI-141), L59 (`test_a_feature_is_planned_once_and_carried_on_after_a_restart`,
  OMNI-174) and L69 (four real cycles in `test_merge_observation.py` came out
  inconclusive, OMNI-186), each seen once under `-n auto`.
- `test_correct_but_not_faster_is_rejected` is **not** test-only: on a heavily
  loaded machine the benchmark can accept its candidate, which is a few percent
  slower than the baseline (M7,
  [OMNI-88](https://olafzumpe.atlassian.net/browse/OMNI-88), won't-fix for
  now). CI did so once, on 2026-10-08.
- Before touching the benchmark gate, know what shaped it (OMNI-41): **a
  larger timing window makes pairing worse, not better.** Drift cancels in a
  ratio only as far as the two halves are adjacent in time: under 17× CPU
  oversubscription, 9 pairs × 10 inputs gave intervals up to [0.19, 1.18];
  99 pairs × 1 input gave [0.922, 0.934].

**How the loop behaves around a human** (each pinned by a test; the history is
in `CHANGELOG.md`):
- **It closes on a human merge** (`DevOps.observe_merge`, OMNI-15): while a
  canary holds green, `loop.serve(watch_merges=True)` polls the pending PR; a
  merge promotes the candidate and starts the next cycle. A PR closed without
  merging releases the hold as `human_declined`, never a failure (OMNI-57).
- **The VCS is the source of truth for what awaits a human** (OMNI-136):
  before every cycle (`loop.serve` and a single `main.py` run),
  `org.hold_for_open_prs` asks `VersionControl.open_prs()` and holds for the
  oldest open PR on a `feature/` branch. A failed listing holds (fail closed).
  Deliberately not in `bootstrap()`, which tools that never start a cycle also
  call. The real GitHub adapter also keeps the pending PR in the episodic
  state, checked at boot (OMNI-126).
- **A feature is planned once and survives a restart** (OMNI-135): its spec
  page, epic and story are reused until its PR opens (`CTO.open_plan`). Each
  step's commit carries trailers (`feature.step_message`), and a restarted
  process finds a branch no PR was opened from
  (`VersionControl.unproposed_branches`) and rebuilds the feature from it,
  skipping a branch the base has moved past or one with a commit that is not
  a loop step.
- **A converged target stops the loop politely** (OMNI-138):
  `loop.converged_after` no-change or no-gain cycles in a row end `loop.serve`
  with "*contract* has converged" and a WARNING page. An inconclusive
  benchmark is neutral too but resets that count (OMNI-145).
- **The run's dataset syncs itself** (OMNI-140, `sis/artifact_sync.py`) every
  `loop.artifact_sync_every` cycles and when the loop stops for any reason;
  a failed sync warns and never stops the loop. Only `--loop` syncs. On AWS
  the loop runs inside tmux and tees `runtime/loop.log` (OMNI-139), and
  SIGHUP is a graceful stop like SIGTERM.
- **The Serve canary**: Ray Serve has no weighted split, so `CanaryRouter`
  (`sis/serving.py`) is the split and the only place a paired sample can be
  recorded; green is a separate Serve application, because one app restarted
  blue on every canary deploy (`docs/SERVE_CANARY.md`).

Two traps L5 surfaced, both worth knowing before writing similar code:
- **Anti-gaming is only as strong as the input distribution.** A candidate that
  was silently wrong above a size threshold passed every gate until the oracle's
  random inputs spanned that range.
- **Env vars don't reach the role actors.** They are detached Ray actors in
  their own processes, inheriting the driver's environment when *created* — so
  anything exported after `bootstrap()` (including `monkeypatch.setenv`) is
  invisible. Per-cycle configuration must be passed as an argument.
