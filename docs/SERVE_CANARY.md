# Ray Serve canary — and why it needs the Class-2 contract to work

**Status:** partly implemented — steps 5, 6, 7, 8, 9 and 11 are in
(`sis/canary.py`, `sis/serving.py`, `sis/loadgen.py`, `sis/serve_cloud.py`, the
`Cloud` traffic/metrics port, the live breach trigger); **10 remains**, plus the
open problem of what observes the human merge. Tracked as
[OMNI-2](https://olafzumpe.atlassian.net/browse/OMNI-2).
Depends on [`docs/CLASS2_CONTRACT.md`](CLASS2_CONTRACT.md) — read that first.
This doc is the extension of it that makes the canary step real.

> Mirrored in Confluence (SD space) as a child of **The Validation Gauntlet**, sibling
> of the Class-2 contract page:
> <https://olafzumpe.atlassian.net/wiki/spaces/SD/pages/8028161>. This repo copy is
> the version-controlled source that evolves with the code; keep the two in sync.

## The claim: these are one problem, not two

`Cloud.deploy_canary()` (`sis/ports.py`) and `DevOps.canary()` (`sis/roles.py`) exist
today, but `InMemoryCloud`/`RealCloud` are placeholders — they record a green-slot
`DeployRecord` with a latency number computed *inside the gauntlet sandbox*, and there
is no traffic, no split, no live comparison. Building the real thing looks like
"just" a Ray Serve integration. It isn't, and the reason is the same reason the
gauntlet needed [L5](KNOWN_ISSUES.md) generalized into a `Contract`:

**A canary has to decide two things about a candidate — is it correct, and is it
better — using only *live production inputs*, which have no stored expected output.**
That's exactly the problem `CLASS2_CONTRACT.md` solves for the *offline* gauntlet
(invariants instead of a reference; a domain SLO instead of a synthetic benchmark).
The canary needs the identical machinery, pointed at real traffic instead of
generated inputs. Build the canary without the contract and you either (a) have no
correctness check on live traffic at all — just a latency comparison, which is how
you promote a *fast, wrong* candidate — or (b) reinvent invariants ad hoc, badly, a
second time.

So: **the contract is checked twice, by two different gates, against two different
input sources — offline (gauntlet, generated/fixture inputs) and online (canary,
live request/response pairs). Same predicates. This doc adds the second gate,
it doesn't invent a new mechanism.**

## What each side already has that the other needs

| | Offline gauntlet (`CLASS2_CONTRACT.md`) | Online canary (this doc) |
|---|---|---|
| Correctness signal | `InvariantGate`: generated valid inputs → predicate | same predicates, sampled from **live** request/response pairs |
| Performance signal | synthetic benchmark (or domain SLO), N fixed inputs | **real traffic latency percentiles** over a rolling window |
| Weakness alone | can't see production input distribution or concurrency; [L5's noise floor](KNOWN_ISSUES.md) — fixed-input timing goes non-deterministic once a target is fast enough (field evidence: runs 4/5, ~30% jitter flipping accept/reject) | no correctness check at all today; can't run *before* spending real traffic on a broken candidate |
| Role | cheap, fast **pre-filter** — reject garbage before it ever sees a real request | **arbiter** for anything the pre-filter can't decide cleanly, including everything L5 flags as noise-floor-ambiguous |

This also answers "does canary replace the offline benchmark gate?" — no. Keep both.
The gauntlet is the cheap sandboxed pre-filter (catches gross breakage before real
traffic); the canary is the expensive, trustworthy final check (catches everything a
synthetic microbenchmark can't see: real input distribution, real concurrency, real
percentile behavior). A converged, sub-microsecond target — the exact case that broke
the offline benchmark — is precisely where a live-traffic percentile comparison over
thousands of real calls is *more* trustworthy than ten synthetic ones, not less.

## Design: reuse the contract, add an online enforcement mode

Extend `Contract` (from `CLASS2_CONTRACT.md`) with one thing: its invariants must be
checkable against a `(request, response)` pair, not just a generated input. They
already are — `Invariant.check` is `predicate(inputs, output) -> bool`; a live
request *is* an input, a live response *is* an output. No new invariant authoring
per target; the same catalog (round-trip, sorted-permutation, conservation,
capacity, monotonicity, non-negativity) applies unchanged.

```python
# sis/canary.py (new)
class CanaryMode(str, Enum):
    SHADOW = "shadow"   # default: dispatch each sampled request to BOTH versions
    SPLIT = "split"     # weighted split only; each request reaches one version

@dataclass(frozen=True)
class LiveSample:
    request: Any
    candidate_response: Any        # what the canary (green) version returned
    baseline_response: Any | None  # SHADOW only — None under a weighted split

@dataclass(frozen=True)
class CanaryVerdict:
    version: str
    samples: int
    invariant_violations: int          # hard gate: must be 0
    response_disagreements: int        # SHADOW only; 0 under SPLIT
    baseline_p95: float; baseline_p99: float
    candidate_p95: float; candidate_p99: float
    passed: bool
    reason: str

def evaluate_canary(invariants: Sequence[Invariant], samples: Sequence[LiveSample],
                     baseline_latencies: Sequence[float],
                     candidate_latencies: Sequence[float],
                     *, mode: CanaryMode = CanaryMode.SHADOW) -> CanaryVerdict:
    """Pure — no Ray, no Serve, no network. Same shape as gauntlet.validate()'s
    Result, so it slots into the existing episodic/provenance plumbing."""
    violations = sum(
        1 for s in samples for inv in invariants
        if not inv.check(s.request, s.candidate_response)
    )
    ...  # percentile compare; all applicable gates must pass to promote
```

**Why `mode`, and why `baseline_response` is optional.** These two are one
decision. A weighted split routes each request to exactly *one* version, so it
can never populate both responses for the same request — a `LiveSample` carrying
both only makes sense under **shadow/mirror** dispatch, where a sampled request
goes to blue *and* green and only blue's answer is returned to the caller.
Shadow is the default: it buys a paired latency comparison (same request, same
instant, so the split's traffic-mix confounder disappears) and a direct
response-agreement check. It costs double compute on sampled requests, and it is
only sound for targets with a unique correct answer — so it is a per-target
setting on the contract, not a global constant. Under `SPLIT`,
`baseline_response` is `None`, the response-agreement gate is skipped, and the
invariant + percentile gates carry the verdict unchanged.

**`evaluate_canary` takes `Sequence[BoundInvariant]`, not `Contract`.** It uses
nothing else from the contract, and depending on the narrower type is what let it
land *before* the L5 `Contract` abstraction exists (see sequencing).

`BoundInvariant` is the *resolved* form: `CLASS2_CONTRACT.md`'s `Invariant`
carries `strategy`/`check` as **strings**, names resolved inside the sandbox
because the offline gate executes untrusted candidate code there. The canary is
the mirror case — it judges responses that have already crossed the network, in
the main process, with predicates from the POLICY-FORBIDDEN contract module
(trusted) — so it wants the bound callable. L5 Layer 1 owns the resolution step;
the distinct name keeps the two from colliding when the contract data class lands.

**Percentile floors are a property of the window size, not a tuning knob.** With
100 samples the nearest-rank p99 *is* the 99th value, so a single slow request is
a p100 event and legitimately invisible at p99 — it takes more than 1% of the
window to move the tail. Worth knowing before sizing a canary window against a
p99 SLO: a window of N can only resolve tail events more frequent than 1/N.

Kept as a pure function on purpose — same project convention as `evaluate_brakes`
and `gauntlet.validate`'s gate functions: decision logic testable without Ray, time,
or a live cluster. `serve()` (the Ray/network-touching shell) calls it, mirroring how
`sis/loop.py` separates `decide()` from `run_loop()`/`serve()`.

**Gates to promote, all applicable ones required:**
1. **Correctness (hard):** `invariant_violations == 0` over the sampled window. Any
   violation → immediate rollback, no negotiation — this is the same "candidate can't
   game it" property the differential-correctness gate protects offline.
2. **Response agreement (hard, `SHADOW` only):** `response_disagreements == 0` —
   blue and green answered the same sampled request identically. Skipped under
   `SPLIT` (there is no paired baseline response to compare), and inapplicable to
   targets with several valid answers, which is the second reason the mode is
   per-target.
3. **Performance (graded):** candidate's live `p95`/`p99` not worse than baseline's
   (or ≥ the target's margin, matching the offline `min_speedup` philosophy) —
   computed over hundreds/thousands of real calls, so it doesn't inherit the
   synthetic 10-input jitter problem.

## The `ServeCloud` adapter

A third `Cloud` implementation alongside `InMemoryCloud`/`RealCloud`, replacing the
placeholder for anything actually served:

```python
# sis/adapters_real.py (extends the existing Cloud section)
class ServeCloud:
    """Cloud via Ray Serve: weighted canary + atomic promote/rollback."""

    def deploy_canary(self, version, *, metrics=None) -> DeployRecord:
        # deploy `version` as a second Serve deployment; start traffic at ~5-10%
        ...

    def shift_traffic(self, version: str, fraction: float) -> None:
        # NEW on the Cloud port — weighted split is the mechanism the in-memory
        # placeholder never needed and the real one is entirely about.
        ...

    def live_metrics(self, version: str, window_s: float) -> dict[str, float]:
        # NEW on the Cloud port — p50/p95/p99 + error rate over a rolling window,
        # per Serve deployment version. Feeds both evaluate_canary() and the loop's
        # real trigger (see below).
        ...

    def promote(self, version: str) -> DeployRecord:
        # atomic: shift_traffic(version, 1.0), retire the old deployment.
        # Still RequiresHumanApproval per CLAUDE.md — human PR merge triggers this,
        # canary evaluation only decides whether to OFFER it, never auto-promotes.

    def rollback(self, version: str) -> None:
        # shift_traffic(version, 0.0), kill the deployment.
```

`shift_traffic`/`live_metrics` are genuinely new `Cloud` port methods — and because
`Cloud` is `@runtime_checkable`, adding them means *both* existing adapters need a
stub the same day the port grows, not just one of them: `InMemoryCloud` implements
them as in-memory fakes (for tests), and `RealCloud` (today's `SIS_ADAPTERS=real`
placeholder) needs its own stub too or it silently stops satisfying `Cloud` until
`ServeCloud` replaces it. `ServeCloud` implements both for real.
`SelfModel`'s blue/green slot fields (`set_slot`, `live_version`) already exist and
need no change — they just start reflecting a real Serve deployment instead of a
recorded string.

`DevOps.canary()` becomes: deploy at low traffic → wait/collect a window →
`evaluate_canary()` → if it fails, `rollback()` + `file_bug()` (same failure-to-artifact
convention as everywhere else in the loop) → if it passes, the result is
`verified_awaiting_human_merge`, same as today; promotion still waits for the human
PR merge, which is what actually calls `promote()`.

## Where the traffic comes from (bootstrap problem)

Nothing external is calling omnibase's target today. Two options, not exclusive:
1. **A local load generator** (`sis/loadgen.py`) — synthetic but *varied* and
   *concurrent* callers hitting the served target, for local/CI-friendly testing.
   Needs to generate **valid domain inputs**, which the contract's invariant
   `strategy` already knows how to do (reuse it — a Hypothesis strategy that
   generates valid invariant-check inputs also generates valid load-gen requests).
2. **Real traffic**, once there's a reason for the target to be user-facing (this is
   the omnitrack step, not the bootstrap one).

Bootstrap should use (1) — it's what makes this buildable and testable *before*
there's a real product behind it, same spirit as the stub proposer being the
zero-cost default.

## Stateful targets are a harder, separate problem — don't start there

Two of the Class-2 example targets — **LRU cache**, **rate limiter** — are stateful
*across calls*. That breaks the clean "deploy candidate, split traffic, compare,
atomic swap" story: promoting a new cache implementation raises "does accumulated
state transfer, or does the candidate start cold?" — a real design question (state
migration vs. cold-start-behind-the-split), not a Ray Serve detail. Recommendation:
**first canary target must be stateless** (a pure request→response function) so the
canary mechanics can be proven independent of the state-handoff problem. Revisit
LRU cache / rate limiter once atomic swap + traffic split works cleanly.

## Scope: which canary mechanism applies (reconciling with DESIGN.md §4)

DESIGN.md §4 names two canary mechanisms, not one: *"Ray Serve canary — deploy the
new version alongside the old, shift weighted traffic, compare against baseline.
For an internal (non-served) actor, the equivalent is: spin up the new actor
version, shadow-run real traffic, then atomically swap the named-actor handle."*
This doc designs only the first — the Ray-Serve/HTTP-fronted path. Disposition, so
the two docs don't drift:

- **Applies here:** any target reachable as a request/response call through Ray
  Serve — including the bootstrap target once it's wrapped behind a `ServeCloud`
  deployment for canary purposes (see "Where the traffic comes from" above), even
  before anything calls it over real HTTP.
- **Out of scope here, not superseded:** a genuinely internal Ray actor that is
  never Serve-fronted (e.g. a future domain actor other actors call directly by
  handle) still needs DESIGN.md's shadow-run-then-atomic-handle-swap mechanism.
  That mechanism has no design doc yet and isn't blocked by anything in this one —
  `evaluate_canary()`'s two gates (invariant violations, live percentile
  comparison) are the reusable part; only the traffic-splitting and
  promote/rollback mechanics differ (weighted HTTP split + Serve deployment swap,
  vs. shadow calls + a named-actor-handle swap).
- **Decision rule for a new target:** called over Serve/HTTP (or wrapped behind
  Serve for testing, per the bootstrap path) → this doc's `ServeCloud`. Called
  only actor-to-actor by Ray handle → this doc's mechanics don't apply; write the
  atomic-swap doc when the first such target exists, reusing `evaluate_canary()`
  for the correctness/performance gates.

## The other payoff: the loop's trigger stops being simulated

`sis/loop.py`'s `once()`/`repeat()` are stand-ins acknowledged in their own
docstrings ("a real deployment swaps this for a trigger that polls... or a
sustained-SLO-breach detector once there is a served endpoint to measure"). Once
`ServeCloud.live_metrics()` exists, that detector is a small pure function in the
same style as `decide()`:

```python
def serve_breach(metrics: dict[str, float], slo_p99_s: float,
                  breach_window_ticks: int, consecutive: int) -> bool:
    """Sustained breach only — CLAUDE.md/DESIGN.md §4: 'never a single spike.'"""

def breach_trigger(cloud: Cloud, version: str, *, slo_p99_s: float,
                    window_s: float, breach_window_ticks: int,
                    title: str, body: str) -> Callable[[], Work | None]:
    """The impure shell: reads live_metrics, keeps the consecutive-breach count,
    and returns Work when serve_breach() says the breach is sustained."""
```

**Two functions, not one.** `loop.serve()`'s `trigger` takes
`Callable[[], Work | None]`, so `serve_breach` — a `bool`-returning predicate over
a metrics snapshot — is not itself a drop-in for `repeat(...)`, despite being the
decision at the heart of one. Splitting it keeps the project's pure/impure line
where `decide()`/`run_loop()` already put it: `serve_breach` is pure and
table-testable (sustained breach vs single spike), `breach_trigger` owns the
`live_metrics` read and the consecutive-tick counter that `serve_breach` is given.
`run_loop`/`decide` still need no changes. That's the last piece that makes `main.py --loop` a genuine self-improving
*server* rather than a scheduler replaying one canned proposal.

**One canary in flight at a time.** This is the first design where the trigger
source and the thing under evaluation share the same live-metrics stream: a
cycle's own canary traffic feeds the very `live_metrics()` window `serve_breach()`
reads, and "collect a window" (sequencing steps 6/10) is minutes-scale, not
instantaneous. Rule: the impure wrapper that calls `serve_breach()`
(`loop.serve()`/`run_loop()` — not `serve_breach()` itself, which stays pure) must
check `SelfModel`'s existing green-slot state before calling `propose()` again; if
a green canary is already deployed, hold the next cycle rather than starting one
concurrently. No new state needed — `SelfModel.set_slot`/deploy-record tracking
already exists (`sis/roles.py`) — this just adds a read-before-propose gate,
keeping the loop's existing one-cycle-at-a-time assumption (one CEO budget gate,
one breaker) true under the real trigger, not just the simulated one.

## Concrete sequencing

Extends `CLASS2_CONTRACT.md`'s sequencing (steps 1–3 there are unchanged prerequisites;
this picks up after the `InvariantGate` lands) and folds in `BRAKE_STATE_AND_ORACLE.md`
step 1 (already done):

1. ~~M2 + L9(a)~~ — **done**.
2. **L5 Layer 1** — `Contract`/`OptimizationContract`; migrate `sum_of_divisors`;
   add roman numerals as a second target (round-trip invariant). *(CLASS2_CONTRACT.md)*
3. **`FeatureContract` + interface/acceptance gates** — first trivial feature
   end-to-end, no invariants yet. *(CLASS2_CONTRACT.md)*
4. **`InvariantGate`** (property-based/Hypothesis) — the offline anti-gaming layer;
   this is also the predicate library the canary reuses. *(CLASS2_CONTRACT.md)*
5. ~~**`sis/canary.py`: `evaluate_canary()` + `CanaryVerdict`**~~ — **done**
   (2026-08-05). Pure, no Ray/Serve/network/clock. Takes
   `Sequence[BoundInvariant]`, so it did **not** wait on step 2. Four gates:
   evidence floor → invariants (hard) → response agreement (hard, `SHADOW` only)
   → p95 *and* p99 within `max_latency_ratio` (default `1.0` = "not worse"; pass
   `0.9` for the offline gate's 10% margin). Everything fails closed — a
   predicate that raises counts as a violation rather than propagating, and a
   `SHADOW` window that lost its baselines returns a `harness:` reason instead of
   being recorded as the candidate disagreeing.
6. ~~**First served target: a sort**~~ — **done** (2026-08-08, OMNI-11).
   `sis/serving.py` stands the sort up as a Ray Serve deployment: the first
   `ray.serve` import in the codebase. Blue and green run **simultaneously with
   different source** (blue owns `/sort`, green `/sort-green`), each response
   carries its own `version`/`slot` so a sample is attributed from the response
   rather than inferred from routing, and a failing request returns an error
   *response* instead of killing the replica — which is what makes `error_rate`
   a candidate signal rather than an infrastructure fault. Stateless by
   construction, so canary mechanics can be proven without also solving
   blue/green state hand-off. `python -m sis.serving` runs both
   (docs/RUNBOOK.md Level 0b).

   Chosen (2026-08-05) over roman numerals and JSON/CSV because its **input size
   scales freely** — per-request work can dominate Serve's ~ms overhead, without
   which a live p95 comparison measures the framework rather than the candidate
   (the online rerun of L5's noise floor) — and its **output is unique per
   input**, so `SHADOW` response-agreement is a strict check.

   **Sandbox boundary, now that something actually serves:** a replica executes
   the source it is handed. Serving the merged target is trusted code; serving a
   *candidate* runs LLM-generated code in a Ray worker, and a Serve replica is
   **not** the gauntlet sandbox — no scrubbed env, no egress block, no per-call
   timeout. That is the intended shape of a canary and the candidate has passed
   every offline gate, but the guarantee is *procedural* here rather than
   kernel-enforced. **Settled in step 9:** green gets a scrubbed `runtime_env`
   (every non-allowlisted variable blanked), while egress stays open by
   construction — a replica exists to answer HTTP. A credential boundary, not a
   sandbox.

7. ~~**`Cloud.shift_traffic` / `Cloud.live_metrics`** on the port~~ — **done**
   (2026-08-05). The port grew both; `InMemoryCloud` implements them for real
   (weight map + `observe()` → windowed percentiles via the new `sis/metrics.py`),
   which is what lets `DevOps.canary()` and the load generator be built and tested
   with no Ray Serve running — the same "fake first, real adapter after" pattern as
   every other port here. `RealCloud` **raises** `NotImplementedError` rather than
   no-op'ing until `ServeCloud` replaces it in step 9: a silent no-op would let a
   real run report a passing canary that never routed a request or measured
   anything. Both adapters have `isinstance(x, Cloud)` conformance tests now, so
   the next port change can't silently drop one.
8. ~~**`sis/loadgen.py`**~~ — **done** (2026-08-08, OMNI-12). Concurrent, varied,
   valid traffic against the served target, with per-request observations fed
   through `InMemoryCloud.observe()` so `live_metrics` summarises real
   measurements. `python -m sis.loadgen --url ... -n 200 -c 8`
   (docs/RUNBOOK.md Level 0c).

   **Inputs come from the contract's oracle (`random_input`), not a Hypothesis
   strategy.** This step originally assumed the latter, and it is still worth
   doing when `InvariantGate` (OMNI-18) introduces strategies for shrinking —
   but the oracle already defines "a valid input for this target", already
   emits args tuples, and is already what the offline differential gate draws
   from. A second generator would be free to drift from it, and a canary's
   verdict is only as good as its input distribution. So Hypothesis stays
   unadded rather than becoming a dependency with no caller, and **this step
   did not need to wait on OMNI-18** as the sequencing assumed.

   **Field measurement, and the reason this whole epic exists.** The merge-sort
   candidate is ~5x faster measured in isolation (0.88ms → 0.17ms) but only
   ~30% faster at p95 under 8-way concurrent load (237.90ms → 167.78ms):
   CPU-bound Python serialises on the GIL, so both versions become queue-bound.
   The offline benchmark cannot see that — it times one call at a time in a
   quiet sandbox — which is precisely the class of thing a live canary is for.

9. ~~**`ServeCloud`**~~ — **done** (2026-08-08, OMNI-13). `sis/serve_cloud.py`:
   real deployments, a real weighted split, real shadow dispatch, real
   per-version latency windows. `python -m sis.serve_cloud` runs the whole
   online path in one command (docs/RUNBOOK.md Level 0d).

   **Ray Serve has no weighted traffic split, so the split is a component.**
   Serve routes a request to one application by path prefix; "send 5% to the
   candidate" is not a config knob. `sis/serving.py`'s new `CanaryRouter` is
   that component, and it is also where shadow dispatch lives — it is the only
   place that sees both versions answer, hence the only place a *paired*
   observation can be recorded at all. Pinned to one replica for that reason; a
   production deployment ships observations to a metrics store and drops the
   constraint.

   **Two applications, not one — and this was measured, not assumed.** The
   obvious topology (router + blue + green in one app) was built first. Adding a
   canary re-runs that graph, and re-running it **restarts the blue replica**
   (blue's construction identity changes; Serve drains gracefully, so no request
   is dropped, but the process cycles). Cycling the stable version in order to
   start observing a candidate discards blue's warm state at the exact moment
   blue becomes the baseline under comparison. So green is deployed as its own
   unrouted application and *attached* to the router: deploy, ramp and rollback
   became control-plane calls, and blue is never touched. Regression test:
   `test_deploying_a_canary_does_not_restart_blue`.

   **The step-6 sandbox question, settled.** A green replica now runs with a
   scrubbed `runtime_env`: every non-allowlisted environment variable is blanked,
   so candidate code cannot read `ANTHROPIC_API_KEY`, `AWS_*` or any other
   env-carried credential. **Network egress stays open by construction** — a
   replica exists to answer HTTP, so `--network none` is unavailable here in a
   way it is not in the gauntlet. This is therefore a *credential boundary, not
   a sandbox*, and `sis/serving.py` says so where someone extending it will look.
   Scrubbing works by **blanking rather than omitting**, because Ray's
   `runtime_env["env_vars"]` merges with the inherited environment — listing only
   the safe variables would have left every secret intact. Both halves are
   tested, and the negative control matters as much as the positive: without it
   the test passes just as happily when the variable never reached the replica,
   which is how the first version of it passed while proving nothing.

   **`live_window()` sits beside `live_metrics()`** (not on the port): the loop's
   breach trigger wants a summarised snapshot, but `evaluate_canary` compares
   latency *arrays* so that offline and online percentiles share one definition
   rather than trusting each other's arithmetic.

   **Field measurement.** 300 requests at concurrency 8, shadow mode: blue
   p95 88.99ms vs green 63.14ms, 300 paired samples, 0 disagreements, verdict
   PASS. The ~29% live gap matches step 8's unpaired ~30% — measured a second
   way, on a different comparison, which is the reassuring result.

   `ServeCloud` is **not yet wired into `Workspace`** — `DevOps.canary()` still
   calls the in-memory adapter. That is step 10 (OMNI-14) by design: the port
   signature has to change first.
10. **Rework `DevOps.canary()`** for the real flow — a signature change, not a
    rewire: today it's `canary(pr_id, candidate_latency: float)`, one scalar
    measured in the gauntlet sandbox (`sis/roles.py`). It needs the target's
    `Contract` plus live samples and baseline/candidate latency arrays instead,
    and nothing today associates a PR/target with a `Contract` to fetch — that
    lookup has to land in `Workspace`/`SelfModel` first. Then: deploy at low
    weight → collect a window → `evaluate_canary()` → rollback+bug or
    verified-awaiting-merge.
11. ~~**`serve_breach()` replaces `repeat()`** as `main.py --loop`'s trigger~~ —
    **done** (2026-08-05), ahead of steps 8–10 because none of it needs the L5
    `Contract`. `window_in_breach`/`serve_breach` are pure (sample floor, sustained
    streak, single spikes rejected); `breach_trigger` is the shell that reads
    `live_metrics` and owns the consecutive-tick counter, resetting both on a
    healthy tick and after firing so a long outage yields one cycle per window
    rather than one per tick. `loop.serve(one_canary_in_flight=True)` (default)
    holds the next cycle while `canary_in_flight()` reports green occupied.

    **Consequence, since resolved:** `DevOps.canary()` sets green and nothing
    cleared it, so with the gate on, `main.py --loop` ran one successful cycle
    and then idled. That was the correct reading of "stop at the human gate" —
    the old behaviour stacked PRs against an unmerged predecessor, and since
    cycles baseline from the *merged* target each one re-proposed the same
    change and spent again for it — but it left the loop permanently parked.
    **OMNI-15 supplies the missing release:** `DevOps.observe_merge()` frees
    green once a human merges, so the loop resumes on its own and the next cycle
    genuinely builds on the merged target. `retire_canary()` remains the
    rollback-side release; `one_canary_in_flight=False` restores the old
    always-propose behaviour and `watch_merges=False` restores the parked one.
12. **`ToolchainAdapter`** (language genericity) and **backtest/SLO gates** — orthogonal
    to this doc, can interleave per `CLASS2_CONTRACT.md`'s own sequencing.
13. **Stateful served targets** (LRU cache, rate limiter) — once state-handoff-on-swap
    has an explicit design (out of scope here).

Steps 2–4 are pure prerequisite (already planned); **5–11 is the actual new work**
this doc adds, and none of it can start meaningfully before 4, because 5 to 10 are
all "the same invariant/contract mechanism, pointed at live traffic" — which is the
whole point of writing this as one doc instead of two.

## Decisions taken (2026-08-05)

Recorded here so the sequencing above reads against settled ground:

| Decision | Choice | Consequence |
|---|---|---|
| Paired samples | **Shadow by default**, `CanaryMode` per target | `LiveSample.baseline_response` is optional; the response-agreement gate is shadow-only |
| First served target | **A sort** | scalable input (real latency signal) + unique output (strict shadow check) |
| Ray Serve in CI | **Yes** — CI runs Serve | `ServeCloud` (step 9) gets real coverage, not skipped integration tests. In the event no new CI stage was needed: `ray[serve]` is a core dependency, so the existing `pytest` job already runs the live Serve tests from steps 6, 8 and 9 |
| Property testing | **Hypothesis**, added as an optional dep group | lands with step 5/8, its first actual use — not added ahead of a caller |

## Open problems

- ~~**Nothing calls `promote()` today.**~~ — **closed** (2026-08-08, OMNI-15).
  `DevOps.observe_merge(pr_id)` reads the PR back from the version-control port
  and, only if `merged` is already true, promotes the candidate and frees green.
  `loop.serve(watch_merges=True)` (default) polls it on each tick while a canary
  is held, so the tick that notices the merge is also the tick that may start
  the next cycle.

  **The gate moved rather than loosened.** `Cloud.promote()` used to raise
  `RequiresHumanApproval` unconditionally in every adapter, which made promotion
  *unreachable* — the rule was enforced by the feature not existing, not by a
  check. It is now enforced where the evidence is: promotion follows an
  **observed** merge, and the agent cannot manufacture one because `merge_pr()`
  still raises everywhere, and `Workspace` — the only surface a role has —
  exposes no merge-shaped method at all. Both locks are asserted by
  `test_no_role_can_reach_a_merge_at_all`, including a `dir(Workspace)` check so
  that adding one later fails the suite rather than silently opening the path.

  Polling was chosen over a webhook: the loop already ticks, so it costs one
  cheap GET per tick while a canary is in flight and nothing when idle, whereas a
  webhook needs an inbound endpoint, a public URL and signature verification —
  real infra weight for a system that runs on a laptop.

  Consequence worth noting: the provenance graph now *terminates in a promotion*
  (`… → canary → promote`) instead of stopping at the canary, so it finally
  records what became live.
- **Sampling rate for live invariant checks.** Checking every response is safest but
  may not be free depending on invariant cost; needs a documented sampling policy
  (start at 100% for a low-traffic bootstrap load-gen; revisit under real volume).
- **Window sizing for `live_metrics`.** Too short → noisy (the same problem L5 already
  has); too long → slow to promote/rollback. Probably a fixed sample-count floor
  (e.g. "at least N requests, at least T seconds"), not a pure time window.
- **State handoff on swap** for stateful targets (above) — deliberately deferred.
- **Cost of the load generator itself** counts against nothing today (no LLM calls),
  but running Ray Serve + a load-gen continuously is a real infra cost once this
  moves to AWS — worth a line in the CEO budget model eventually, out of scope here.
