# Ray Serve canary — and why it needs the Class-2 contract to work

**Status:** design sketch (not implemented). Depends on
[`docs/CLASS2_CONTRACT.md`](CLASS2_CONTRACT.md) — read that first. This doc is the
extension of it that makes the canary step real.

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
@dataclass(frozen=True)
class LiveSample:
    request: Any
    baseline_response: Any     # what the live (blue) version returned
    candidate_response: Any    # what the canary (green) version returned

@dataclass(frozen=True)
class CanaryVerdict:
    version: str
    samples: int
    invariant_violations: int          # hard gate: must be 0
    baseline_p50: float; baseline_p99: float
    candidate_p50: float; candidate_p99: float
    passed: bool
    reason: str

def evaluate_canary(contract: Contract, samples: Sequence[LiveSample],
                     baseline_latencies: Sequence[float],
                     candidate_latencies: Sequence[float]) -> CanaryVerdict:
    """Pure — no Ray, no Serve, no network. Same shape as gauntlet.validate()'s
    Result, so it slots into the existing episodic/provenance plumbing."""
    violations = sum(
        1 for s in samples for inv in contract.invariants
        if not inv.check(s.request, s.candidate_response)
    )
    ...  # percentile compare; both gates must pass to promote
```

Kept as a pure function on purpose — same project convention as `evaluate_brakes`
and `gauntlet.validate`'s gate functions: decision logic testable without Ray, time,
or a live cluster. `serve()` (the Ray/network-touching shell) calls it, mirroring how
`sis/loop.py` separates `decide()` from `run_loop()`/`serve()`.

**Two independent gates to promote, both required:**
1. **Correctness (hard):** `invariant_violations == 0` over the sampled window. Any
   violation → immediate rollback, no negotiation — this is the same "candidate can't
   game it" property the differential-correctness gate protects offline.
2. **Performance (graded):** candidate's live `p95`/`p99` not worse than baseline's
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

`shift_traffic`/`live_metrics` are genuinely new `Cloud` port methods — `InMemoryCloud`
implements them as in-memory fakes (for tests), `ServeCloud` as the real thing.
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
```

`loop.serve()`'s `trigger` parameter already accepts any `Callable[[], Work | None]`
— this is a drop-in replacement for `repeat(...)`, no `run_loop`/`decide` changes
needed. That's the last piece that makes `main.py --loop` a genuine self-improving
*server* rather than a scheduler replaying one canned proposal.

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
5. **`sis/canary.py`: `evaluate_canary()` + `CanaryVerdict`** — pure function, unit
   tested with fakes (no Ray/Serve). This doc, step 1.
6. **Pick the first served target: stateless only.** Recommend the JSON/CSV
   transformer or the sort — naturally request/response-shaped, no state-handoff
   design needed. *Not* LRU cache / rate limiter yet (see above).
7. **`Cloud.shift_traffic` / `Cloud.live_metrics`** on the port; `InMemoryCloud` fake
   implementations first (so `DevOps.canary()` can be built and tested without Ray
   Serve running at all) — same "fake first, real adapter after" pattern as every
   other port in the project.
8. **`sis/loadgen.py`** — reuse the contract's invariant `strategy` to generate valid
   concurrent traffic locally.
9. **`ServeCloud`** — the real Ray Serve adapter: deploy, weighted split, atomic
   promote/rollback, `live_metrics` backed by real Serve metrics.
10. **Wire `DevOps.canary()`** to the real flow: deploy at low weight → collect a
    window → `evaluate_canary()` → rollback+bug or verified-awaiting-merge.
11. **`serve_breach()` replaces `repeat()`** as `main.py --loop`'s trigger — the real
    monitor.
12. **`ToolchainAdapter`** (language genericity) and **backtest/SLO gates** — orthogonal
    to this doc, can interleave per `CLASS2_CONTRACT.md`'s own sequencing.
13. **Stateful served targets** (LRU cache, rate limiter) — once state-handoff-on-swap
    has an explicit design (out of scope here).

Steps 2–4 are pure prerequisite (already planned); **5–11 is the actual new work**
this doc adds, and none of it can start meaningfully before 4, because 5 to 10 are
all "the same invariant/contract mechanism, pointed at live traffic" — which is the
whole point of writing this as one doc instead of two.

## Open problems

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
