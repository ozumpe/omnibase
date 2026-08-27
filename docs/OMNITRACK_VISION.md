# From Class 2 to omnitrack — building a digital twin with omnibase

**Status:** vision & sequencing. **None of E1–E5 is built** — with one exception, the
`Clock` port, which D6 pulled forward into
[OMNI-23](https://olafzumpe.atlassian.net/browse/OMNI-23) and which shipped as
`sis/clock.py`.

The Class-2 base this document builds on **has landed** (2026-08-11):
`FeatureContract` with a contract-selected gate profile, `InterfaceGate`,
`AcceptanceGate`, `InvariantGate`, `BacktestGate`, and the contract-author actor. Two
pieces of [OMNI-3](https://olafzumpe.atlassian.net/browse/OMNI-3) did **not** ship and
neither is on this document's path: `SloGate`
([OMNI-24](https://olafzumpe.atlassian.net/browse/OMNI-24), low) and `ToolchainAdapter`
([OMNI-20](https://olafzumpe.atlassian.net/browse/OMNI-20), parked) — the epic stays
open for those two alone. The ✅ column in §2 therefore describes shipped code except
where a cell says otherwise. Read [`CLASS2_CONTRACT.md`](CLASS2_CONTRACT.md) first; this
is its continuation. [`SERVE_CANARY.md`](SERVE_CANARY.md) is the online half of the same
verification story and is reused wholesale here.

> Not yet mirrored in Confluence. When it is, it belongs in the SD space as a sibling of
> the Class-2 contract page.

---

## 1. The thesis

**omnitrack is not one system omnibase builds. It is N modelled actors, each of which is
a contract plus a deploy slot on the same engine.**

The engine is already target-agnostic — nothing in `sis/` knows a target by name, and a
new target is "a `specs/` directory plus a registry entry, not an engine change." That
property is the whole plan. A new modelled actor — a supplier, a junction, a regulator, a
tenant — is a new contract, a new slot, an episodic partition, and a share of the budget.
It is *not* a new omnibase.

Separate omnibase *instances* may eventually make sense for ops reasons (independent
failure domains, independent cadence, separate ownership). That is a deployment decision
to defer (**D1**). The decision to make now is that **the contract boundary is the actor
boundary**, because that is what gives credit assignment: a system-level score is one
scalar for hundreds of components and nothing converges against it.

---

## 2. What OMNI-3 provides — and what it doesn't

| Capability | After OMNI-3 | Gap for omnitrack |
|---|---|---|
| Verify a built feature offline | ✅ `FeatureContract` + invariants + backtests | assumes a **deterministic** entry point |
| Verify on live traffic | ✅ `ServeCloud` canary, same predicates | assumes a **stateless** target |
| Any target, any language | ⚠️ contract yes; `ToolchainAdapter` **parked** (OMNI-20) — Python only today | — |
| Spec → contract | ✅ contract-author actor (human-reviewed) | — |
| **Read the world** | ⚠️ `Clock` port shipped (OMNI-23); no `Sensor` | no way to *read* the world — event time exists, readings don't |
| **Know the model is wrong** | ❌ | trigger is "code is slow", not "model disagrees with reality" |
| **Model non-deterministic reaction** | ❌ | every gate compares values, not distributions |
| **Hold live state across a swap** | ❌ | canary works *because* the target is stateless |
| **Many targets in parallel** | ❌ | one contract per cycle; `Workspace.cloud` is a single slot (`DevOps` already holds one `ServeCloud` per contract) |
| **Catch emergent misbehaviour** | ❌ | every actor can pass locally while the system oscillates |

Six gaps, five new components — the sixth ("know the model is wrong") is wiring, not a
component: Phase B connects prediction error to the trigger shape that already exists.
None of this is an engine rewrite.

---

## 3. The five new components

### E1 — `Sensor` port (+ a clock)
A port like the existing five, with **two adapters from day one**: `RealSensor` (reads the
world) and `SimSensor` (generates scenarios). Paired with a `Clock` port so the twin can
run in event time, not wall-clock.

**The clock half is already built** — `sis/clock.py` (OMNI-23) ships `WallClock`,
`ReplayClock`, and timezone-required `event_time` parsing, so what remains of E1 is the
`Sensor` port and its two adapters. Note what that module already settled, so E1 doesn't
relitigate it: event time is *not* used for durations (the benchmark gate keeps
`perf_counter`) and *not* for the audit trail (`SelfModel.record` stays on the wall
clock). Only "when did something happen out there" goes behind the port.

This single component pays for itself three times:
1. it is the input to the twin;
2. it is the **scenario generator** — drive the model into situations that have never
   happened;
3. it is the **input generator for the gauntlet**, which closes the open problem
   `CLASS2_CONTRACT.md` flags ("property generators are real work"). Same trick as the
   oracle being both reference and prompt source: one artifact, two uses.

**The trap:** if the simulator generates the test inputs *and* the model is judged against
the simulator, the loop closes with no contact with reality — and the simulator is itself a
model you are also improving. The rule that prevents it: **simulation is for refutation,
real data is for acceptance.** Sim finds failure modes and drives coverage; promotion
requires real-trace evidence (**D4**). The simulator gets its own contract, backtested
against recorded sensor traces, or it drifts and takes everything downstream with it.

**The second trap: sensor data is untrusted input.** The existing hard rules treat
generated *code* as untrusted; a `RealSensor` adds untrusted *data* — readings an outside
party can influence. Traces flow into scenario libraries, backtest fixtures, and (because
the proposer prompt is contract-derived) potentially into LLM prompts: prompt injection
via a sensor trace is a real risk class, not a hypothetical. Anything that travels from
`RealSensor` into a prompt or a contract artifact gets the same skepticism as generated
code — sanitised, size-bounded, never interpolated raw.

One operational note, already learned elsewhere in this repo: detached Ray actors inherit
the driver's environment at *creation*, so `Sensor`/`Clock` configuration must be passed
per cycle as arguments, never read from env vars after bootstrap (same rule as
`SIS_CONTRACT`).

### E2 — the determinism axis and the distributional gate family
The load-bearing change: **correctness stops being a value and becomes a distribution.**
A reaction model ("how would this institution respond to X") has no reference oracle, no
single right answer, and only sparse ground truth.

**This is an axis, not a Class 3.** The tempting move is to call it a third contract class
after optimisation (Class 1) and feature construction (Class 2), and that would be a
category error with real downstream cost. Classes 1 and 2 differ by *what the task is* —
optimise a working function, versus build one from a spec — and correspondingly by where
truth comes from. Determinism differs by *what the output is*, and it crosses both:

|                          | Deterministic                                   | Stochastic                                                        |
|--------------------------|-------------------------------------------------|-------------------------------------------------------------------|
| **Optimise** (Class 1)   | differential vs reference + benchmark *(today)*  | distributional equivalence vs reference + benchmark                |
| **Construct** (Class 2)  | acceptance + invariants + backtest               | acceptance bounds + distributional invariants + calibration/skill  |

A slow Monte Carlo simulation someone wants sped up is the top-right cell: unambiguously
a Class-1 optimisation, unambiguously stochastic. A "Class 3" has no room for it.

So determinism is **a field on the contract, defaulting to deterministic** — not a new
contract type. Three consequences worth stating plainly:

1. **Deterministic contracts stay deterministic, permanently.** There is no ladder to
   climb and no upgrade path to opt out of. A contract that declares nothing behaves
   exactly as it does today, forever.
2. **The gate *stack* never branches on determinism.** Only the **comparator** inside a
   gate does: exact-or-tolerance versus a proper scoring rule. One pipeline, swappable
   comparison — which is why `Backtest.compare` in OMNI-19 is the whole mechanism rather
   than a convenience.
3. **The only structural addition is the seed requirement**, and it applies solely to
   contracts that declare themselves stochastic.

With that framing, the Class-1 stack maps over almost line-for-line:

| Existing gate | Stochastic analogue |
|---|---|
| `ast` / `mypy --strict` | unchanged |
| **no-op check** | **must beat the base-rate / climatology baseline** (skill > 0) |
| interface | unchanged, **plus the entry must take an explicit seed** |
| acceptance tests | named scenarios with expected *bounds*, not expected values |
| **differential vs reference** | no reference → **calibration + distributional invariants** over N sampled runs |
| **benchmark ≥ margin** | **proper scoring rule vs the incumbent, by margin** (Brier / log score / CRPS) |
| canary on live traffic | paired live prediction-error vs incumbent |

Three details carry the weight:

- **The no-op analogue is not optional.** A model that always predicts the base rate is
  perfectly calibrated and useless. The gate is calibration *and* discrimination against a
  climatology baseline — structurally the same check that already rejects an unchanged
  candidate as `no_change`.
- **Determinism under seed is a hard interface requirement.** Without it the gauntlet
  cannot reproduce a failure and every distributional gate is noise.
- **`diff_trials` generalises directly** (`sis/contract.py`): 300 randomised trials becomes
  "N sampled runs", so statistical power is a contract field, not a new concept.

**Anti-gaming gets genuinely weaker here, and this is the one real regression.** A
candidate can overfit sparse historical episodes, and the accept/reject signal leaks the
holdout one bit per cycle — the loop overfits data it never saw, purely through selection
(the adaptive-data-analysis problem; cf. Dwork et al.'s reusable holdout). Mitigation fits
what already exists: a held-out episode split inside the POLICY-FORBIDDEN `specs/` space
that never enters the proposer prompt, plus a **holdout-evaluation budget** as a brake next
to the CEO's spend cap. The episodic store already records every rejected diff and the gate
that caught it, so holdout burn is measurable from data being written today (**D5**).

### E3 — Stateful swap
The Serve canary works *because* `/sort` is stateless by construction. A modelled actor
holds live state by definition. There are two ways out and they differ enormously in cost —
see **D2**. Externalising state behind a `StateStore` port collapses this component into
the canary pattern that already works; keeping state in the actor requires drain/handoff
semantics, a versioned state schema, a contract-verified `migrate()`, and trajectory
comparison rather than single-output comparison.

This is also where the **atomic actor swap** scoped out of
[`SERVE_CANARY.md`](SERVE_CANARY.md) (and sketched in `DESIGN.md` §4: spin up the new
version, shadow-run, swap the named-actor handle) comes home: E3 either *is* that design
or must subsume it. Neither has a design doc yet — whoever schedules either piece first
writes the one document, so drain/handoff machinery isn't built twice.

### E4 — Per-actor slots
Registry entries, a deploy slot per modelled actor, an episodic partition per actor, and a
budget share. Mechanically the smallest of the five; `Workspace.cloud` becoming a map
rather than a single slot is most of it. **The CEO brake stays global** — N loops each
honouring their own cap is N times the budget.

### E5 — Emergence gate
Per-actor contracts cannot see oscillation, deadlock, or cascade. A second, slower
verification level: a system-level backtest over a multi-actor scenario, run every N cycles
rather than every cycle. Structurally the same split as gauntlet (fast, offline) vs canary
(slow, live), one level up. Advisory before blocking (**D10**).

Also needed here: **interaction contracts.** A's model of how B reacts, versus B's own
model of itself. Without an explicit protocol between modelled actors, each drifts into
private assumptions. An actor's *observable behaviour* wants to be a port, with a contract
on the interface, not only on the implementation.

---

## 4. Sequencing

Each phase ends in something runnable, in the style of the RUNBOOK levels.

| Phase | Delivers | Milestone you can run |
|---|---|---|
| **A** | E1: `Sensor` + `Clock` ports, real + sim adapters, one toy domain | drive the twin from a recorded trace *and* from a generated scenario; prediction error computed, nothing acts on it |
| **B** | prediction error as trigger | a **sustained model-error breach** starts a cycle, exactly as a sustained SLO breach does today — reuses the existing monitor/brake shape; the error series lands in the Telemetry port + SelfModel, the same home as the SLO metrics it mirrors |
| **C** | E2: the determinism axis + distributional comparators | one actor's reaction model passes/fails on calibration + skill + invariants, against a held-out split it never sees |
| **D** | E4: per-actor slots | two modelled actors improving independently on one engine; roll one back without touching the other |
| **E** | E3: stateful swap | swap a live actor's model version under load, state preserved, nothing else restarts |
| **F** | E5: emergence gate + interaction contracts | system-level backtest over a multi-actor scenario; advisory verdict |

Rationale for the order: **sense → notice you're wrong → verify a fix → parallelise →
swap live → integrate.** A and B are cheap and unlock the honest version of every later
demo (you cannot backtest without event time, and you cannot justify a cycle without a
model-error signal). C is the intellectual core. D is mechanical. E's cost is set entirely
by D2. F needs history that only exists after D and E have been running.

Phases A–C are the minimum that proves the thesis on one modelled actor. That is the
milestone worth aiming at before scoping the rest.

---

## 5. The verification model, in one picture

```
                 sim sensor ──┐                  ┌── refutation only
                              ├─> contract gates ┤
                real sensor ──┘                  └── acceptance evidence
                                    │
        per actor, per cycle ───────┤ fast: interface, acceptance, invariants,
                                    │       calibration, skill vs climatology
                                    │
        system, every N cycles ─────┤ slow: multi-actor backtest, interaction
                                    │       contracts, emergence
                                    │
                     live ──────────┘ canary: paired prediction error vs incumbent
```

Two rules hold the whole thing together, and both are carried over rather than invented:

1. **The implementer cannot edit its own exam.** `specs/` stays POLICY-FORBIDDEN, and that
   now covers oracles, invariants, scenario libraries, and held-out splits.
2. **Author and implementer are different actors.** The domain laws — "cargo is conserved",
   "a regulator never acts before notice" — come from a human or the contract-author actor
   under review, never from the loop.

---

## 6. Decisions to be made

Numbers are stable identifiers, not an ordering — the → line on each gives its deadline.
Each entry states a recommendation; where a decision has been taken it follows below it and
supersedes it. **Decided: D0–D9, D11, D12** (2026-08-27). **Still open: D10 alone** — does
the emergence gate block promotion — which is not due until Phase F and cannot sensibly be
settled before E5 has run advisory for a while.

**D0 — What slice of the world does omnitrack model first? DECIDED: regional air traffic
(OpenSky / ADS-B Exchange).**
Everything in §8 — ground-truth density, episode frequency, sensor availability — is a
property of this choice, and it determines the first `RealSensor` adapter and the domain
of Phase A. **D8** then decides who decomposes the chosen domain into modelled actors.
→ *Decided before Phase A, as required; every later decision is shaped by it.*

Richest, highest-frequency public data of the four candidates considered, and the most
visually compelling demo. Also the heaviest scope/optics tax — the one domain here where
"wrong is dangerous" needs active management even for a passive, non-controlling twin —
and a first fixture (holding-pattern/reroute vs. weather) is a bigger lift than one bike
station.

The other three candidates considered, not selected:

- **Bike-share network** (station GBFS feeds + historical trip data). Best fit on all four
  selection criteria (public/cheap data, frequent events, real structure to model, low
  stakes if wrong) and on §1's actor-network shape: each station is an actor, the
  rebalancing dispatcher is a literal regulator reacting to network imbalance. Cheapest
  Phase-A path — one station, fill-level from rides + weather — before any network model
  exists.
- **Regional power grid** (EIA + ISO real-time load). Strongest real structure to model
  (actual supply/demand balancing physics/economics); the regulator/supplier actor shape
  comes free from how the grid is already organised. More moving parts than bike-share to
  stand up a first fixture.
- **Multi-agency transit** (GTFS-realtime across a metro's subway/bus/rail). Best E5 story —
  cascading delay is the textbook "every local actor passes, the system misbehaves" demo —
  but that payoff needs several actors already standing, so it's a weaker Phase-A start.

**D1 — One engine with N contracts, or N omnibase instances?**
*Recommend one engine, N slots.* The engine is already target-agnostic; per-actor
separation buys credit assignment and rollback granularity without a second engine.
Separate instances become attractive only for independent failure domains and ownership.
→ *Decided before Phase D; cheap to revisit.*

Decision:
*One Engine with N Conttracts*

**D2 — Where does twin state live: in the Ray actor, or behind a `StateStore` port?**
The highest-leverage decision in this document. *Recommend externalising it.* If modelled
actors are stateless compute over an external store, E3 collapses into the Serve canary
pattern that already works, and state migration becomes an ordinary schema migration with
its own contract. Keeping state in the actor means building drain/handoff, versioned state,
and trajectory comparison from scratch — and Ray cannot hot-swap an actor class anyway, so
you would be building that machinery regardless.
→ *Decided before any twin code is written. Retrofitting is a rewrite.*

Decision:
The actor state needs to be externalized. Ideally in DuckDB and in a human readable way.
Ideally, it should be possible to optionally store each transition with the clock time and the 
cause for transitioning to be able to debug/follow reasoning (it may not even take too much 
space if implemented right but it should probably not be the default).

**D3 — What is a reaction model, as an artifact?**
Generated Python with explicit parameters, a fitted statistical model, or an LLM called at
runtime? This determines what the gauntlet is even checking. *Recommend generated,
parameterised code — LLM at build time, not at runtime.* Runtime LLM calls put
non-determinism, per-request cost, and an unauditable dependency inside the twin, and no
gate in this document can verify them. If a runtime LLM is ever wanted, it is a distinct risk
class that needs its own design.
→ *Decided before Phase C.*

Decision:
Here we should compromise between using LLMs and still being able to test and simulate:
The power of LLMs is indispensable for certain applications to get real world real-time digital twins and it is good to have the option to use LLM responses - within testable and simulatable boundaries.
There will definitely be actors that need to use LLMs at some point and in some formalized ways - like expecting responses certain formats (e.g. JSON with specified fields)
so it can be efficiently evaluated by code.
If an actor depends on LLMs, we need to be able to simulate the LLM responses for testing and game playing/simulating scenarios (e.g. answering with pre-canned responses, pre-defined scenarios).
In order to parameterize tests, we could have for each LLM dependent actor specialized LLM actors or interfaces (reverse of MCP servers), that can be mocked for testing purposes.

However, this should be not relevant until we need actors that interact with LLMs.

Points to remember:
- mocks need to verify how actor's are handling the responses, never the LLM's behavior — so promotion evidence can't come purely from mocked responses, and the first LLM-dependent actor still needs its own design note
    (the original "distinct risk class" point survives this D3 compromise).
- Runtime LLM calls are per-request spend and must sit under the CEO brakes, which today only meter the proposer.
- All LLM responses are untrusted input (like sensor data in §3's second trap — schema-validated, size-bounded, never interpolated raw. One genuine gap to name: an LLM-backed actor is STOCHASTIC on the E2 axis, but E2's hard requirement is "determinism under seed," which no LLM API can honor (temperature 0 is not determinism).

**D4 — What evidence is required to promote: simulated, real, or both?**
*Recommend real-trace evidence required for promotion; simulation for refutation and
coverage only.* Needs to be a contract field, not a convention, or it erodes the first time
real data is inconvenient.
→ *Decide with Phase A, enforce from Phase C.*

Decision:
As base for a promotion decision, recorded and human approved real world data is preferred, simulated data is to be used, if no recorded data is available (all tests must pass and it needs to be able to tolerate life traffic).

Before a promotion we should expose the candidate to real world traffic and look at the error rate (if it can handle the format and the volume, and if there are no exceptions and probably if the responses are in an expected range)

**D5 — How is holdout burn managed?**
Options: rotating splits, a fixed evaluation budget, noised score reporting, or all three.
*Recommend budget + rotation, with burn measured from the episodic log* (the data is
already being written). The alternative is silent overfitting that no gate reports.
Rotation implies ongoing writes into `specs/` — the ingestion path is **D12**.
→ *Decide with Phase C.*

Decision:
Go with the recommendation: budget + rotation, with burn measured from the episodic log.

**D6 — Event time or wall-clock?**
*Recommend event time behind a `Clock` port, with wall-clock as one adapter.* Backtest and
replay are impossible without it, and it is nearly free at the start and painful later.
→ *Decided with Phase A.*

Decided as recommended: event time behind a Clock port with wall-clock as one adapter

**D7 — Is the simulator part of the exam or a target the loop may improve?**
Tension: you want the simulator to get better, but it generates the test inputs.
*Recommend splitting it* — the generator mechanism is FORBIDDEN, the scenario library is
reviewed data, and simulator improvements go through their own contract judged against
held-out **real** traces. Never against itself.
→ *Decide before Phase C.*

Decided as recommended:
the simulator needs to be an improvable target. All improvements must only be judged against held-out real traces and 
never ba judged against its own output, with a copy that generates the gauntlet inputs staying FORBIDDEN at a pinned 
version, improvements reaching it via the human-approved promote path — is what prevents a closed loop.

**D8 — Who decides what the modelled actors are?**
Human/PM-authored ontology, or loop-proposed decomposition? *Recommend human-authored for
v1.* This is the same input class as the domain invariants — a small, stable, high-value
human contribution. A loop that chooses its own decomposition is also choosing its own
scoring boundaries.
→ *Decide before Phase D.*

Decided as recommended:
A human should decide what the modelled actors are. However, this human decision can be driven by a 
proposal.from any of the actors, but it must be approved by a human.

**D9 — What counts as "the model is wrong enough to act"?**
A per-actor prediction-error budget with a sustained-breach rule, mirroring the existing
SLO trigger. The open question is whether the threshold is absolute, relative to the
incumbent, or relative to climatology. *Recommend relative to climatology* — it is the only
form that stays meaningful as the model improves.
→ *Decide with Phase B.*

That's a decision to be made case by case, actor by actor and project by project. My opinion is to optimize the implementation
of a model over time based on historic values/timelines. The situation for each actor can change suddenly and trastically - 
even the climatology option can become unreliable quickly. It is paramount to keep a detailed history 
of input values, and actions. Then come up with the most applicable model for each of the actors over time - maybe 
through regression or an appropriate functions, backward propagation, what ever fits.

What counts as a model is wrong enough to act? I think it depends on the context and the specific requirements of each actor but mainly two criteria:
- Absolute error budget → "is the active twin fit for its purpose right now?" This is a safety or utility statement. Consequence: alert a human, downgrade confidence, stop trusting the output. 
- Skill vs climatology → "is there recoverable headroom a code change could capture?" Consequence: spend LLM budget on a cycle.

Ocasionally,
(1) we need to spend money on a world that got harder rather than a model that got worse by initiating a self-improvement cycle because the absolute error rate is too high, while the skill didn't chang.
(2) or if the model has drifted and the headroom is real, we need to spend money by firing when skill a decays while the absolute error still looks fine.
(1) and (2) should not be the same trigger.

**D10 — Does the emergence gate block promotion?**
*Recommend advisory first, blocking once it has enough history to be trusted.* A blocking
gate with a high false-positive rate will be switched off, and then it is worse than
advisory.
→ *Decide at Phase F.*

**D11 — How is the budget split across N actors?**
Global cap with per-actor shares, or per-actor caps? *Recommend a global cap with soft
per-actor shares* so one actor's productive streak isn't starved by an idle sibling, while
the hard ceiling stays global.
→ *Decide with Phase D.*

Recommendation sounds correct:
- The total amount of money to be spend on the system must be capped globally.
- the money spend on each actor should be allocated based on their contribution, impact and error rate.

**D12 — Who writes real traces into the exam?**
The held-out splits and scenario library live in POLICY-FORBIDDEN `specs/` (§5, rule 1),
but the traces are produced at runtime by the system itself, and D5's rotation implies
ongoing writes. The loop must not write its own exam; a human reviewing every trace does
not scale past the toy domain. *Recommend a one-way ingestion pipeline outside the loop's
authority:* traces land in an append-only staging partition of the episodic store, and
promotion into `specs/` is a batched, human-approved operation — the same
`RequiresHumanApproval` shape the adapters already use for destructive actions. The loop
never holds write access to `specs/` at any point.
→ *Decide with Phase A (capture format), enforce from Phase C (first holdout).*

The loop could propose its exam and a human can review/change/approve/reject it.

---

## 7. What does not change

Worth stating explicitly, because the temptation to relax these grows with domain
complexity:

- **Policy tiers.** Guardrails FORBIDDEN with no override; `specs/` FORBIDDEN; only the
  designated target is SOFT. Modelled-actor implementations are new SOFT targets; sensors,
  scenario generators, invariants, and held-out data are not.
- **The sandbox.** Generated reaction models are untrusted code. Sampled runs, invariants,
  and backtests execute inside the sandbox, never the main process.
- **The human merge.** Promotion still follows an *observed* merge. Nothing in this document
  gives the loop a path to promote its own work.
- **The brakes.** Global spend cap, cost-per-accepted-improvement SLO, circuit breaker —
  plus the new holdout budget from D5.

---

## 8. Where this is hard

- **Sparse ground truth.** Institutional reactions are rare events. Calibration over a
  handful of historical episodes is weak evidence, and no gate can manufacture more data.
  Expect long acceptance windows and resist tightening margins to compensate.
- **The simulator is a model too.** D7 contains it; it does not solve it. A confidently
  wrong simulator produces a confidently wrong twin that passes every gate.
- **Emergence has no oracle.** E5 can detect that a system-level backtest degraded. It
  cannot attribute the degradation to an actor. Credit assignment across interacting
  stochastic models is an open problem, not a deferred task.
- **Verification cost scales with N actors × N samples.** Distributional gates need many
  runs; per-actor parallelism multiplies that. The gauntlet's cost stops being negligible
  and becomes something the budget gate must reason about.

---

## 9. Relationship to existing work

- [`CLASS2_CONTRACT.md`](CLASS2_CONTRACT.md) — the feature contract this extends. E2 is
  *not* a Class 3: it adds a determinism axis that crosses both existing classes, leaving
  every deterministic contract untouched.
- [`SERVE_CANARY.md`](SERVE_CANARY.md) — the online gate. Reused unchanged for stateless
  targets; E3/D2 decides whether it is reused unchanged for stateful ones too.
- [`ACTORS.md`](../ACTORS.md) — the org that builds omnitrack. Unchanged. The modelled
  actors of the twin are a *different* population from the engineering roles, and the two
  should never be conflated in code or naming.
- `docs/KNOWN_ISSUES.md` — defects. This document is not a defect list; planned work lands
  in Jira.

**Already delivered via the Class-2 epic** (folded in 2026-08-09, shipped by 2026-08-11).
Rather than wait for a Phase-A epic, the parts of this plan that the Class-2 epic
([OMNI-3](https://olafzumpe.atlassian.net/browse/OMNI-3)) could absorb were folded into
it. That paid off: the groundwork for Phases A and B exists as a side effect of finishing
Class 2, so Phase A starts from the `Sensor` port rather than from scratch.

| Ticket | Status | Relationship to this document |
|---|---|---|
| [OMNI-19](https://olafzumpe.atlassian.net/browse/OMNI-19) — `BacktestGate` | ✅ Done | Re-scoped and raised to Highest. Carries `split` (D5) and the pluggable `compare` comparator (E2). Was scheduled last on the theory that backtests need history; that is backwards here, where comparing against recorded reality *is* Phases A/B. |
| [OMNI-23](https://olafzumpe.atlassian.net/browse/OMNI-23) — `Clock` + event time | ✅ Done | D6, pulled forward: a fixture recorded without event time cannot be replayed, and the window to re-record may be gone. Landed as `sis/clock.py` before any fixture — the one piece of E1 that already exists. |
| [OMNI-17](https://olafzumpe.atlassian.net/browse/OMNI-17) — `FeatureContract` | ✅ Done | Declares `determinism`, defaulting to deterministic, and requires a seeded entry point when stochastic. |
| [OMNI-18](https://olafzumpe.atlassian.net/browse/OMNI-18) — `InvariantGate` | ✅ Done | Seeds generation explicitly and records the seed in the reject reason, so E2's gates inherit reproducibility. |
| [OMNI-21](https://olafzumpe.atlassian.net/browse/OMNI-21) — contract-author actor | ✅ Done | Owns the *only* write path into `specs/`, built as a general human-approved ingestion mechanism so D12's trace pipeline reuses it. [OMNI-26](https://olafzumpe.atlassian.net/browse/OMNI-26) added worked-example transcription and the discrimination check. |
| [OMNI-25](https://olafzumpe.atlassian.net/browse/OMNI-25) — D0 | ✅ Done | Decided: regional air traffic (§6, D0). |
| [OMNI-24](https://olafzumpe.atlassian.net/browse/OMNI-24) — `SloGate` · [OMNI-20](https://olafzumpe.atlassian.net/browse/OMNI-20) — `ToolchainAdapter` | ⬜ To Do (low / parked) | Split out and parked respectively. Neither is on this document's path. Detached from OMNI-3 on 2026-08-27 so the completed epic could close; they stand alone in the backlog. |
| [OMNI-3](https://olafzumpe.atlassian.net/browse/OMNI-3) — the Class-2 epic | ✅ Done | Closed 2026-08-27. |

**Phase A is filed:
[OMNI-30](https://olafzumpe.atlassian.net/browse/OMNI-30)** (2026-08-27) — the `Sensor`
port, an OpenSky/ADS-B `RealSensor` with sanitisation in-scope, the `SimSensor`, the
first recorded fixture, and prediction error computed but not acted on. It also carries
the one OMNI-25 acceptance criterion that was never completed: checking the `Clock`
shape against air traffic's actual data cadence before a fixture is recorded.
