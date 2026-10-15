# `specs/` — the contract space

Everything under this directory is **POLICY-FORBIDDEN** (`sis/policy.py`,
`GUARDRAIL_DIRS`). The self-improvement loop can never write here, with no
override — not `SIS_ALLOW_STRICT_CHANGES=1`, not human approval, the same
absolute tier that protects the gauntlet and the spend brakes.

## Why

A contract is the exam a generated candidate has to pass: the reference oracle
it must agree with, the inputs it is measured on, and the acceptance tests it
must satisfy. **The implementer must not be able to edit its own exam.** That is
the direct generalization of the rule that kept the reference oracle out of the
loop-mutable target — if a candidate could weaken its own contract, every gate
downstream is theatre.

Structurally: PM/QA (or a human) author what goes here; the SWE writes only the
target module, which is `SOFT`. Separation of concerns *is* the anti-gaming
property. See `docs/CLASS2_CONTRACT.md`.

## Layout

```
specs/
    comparators.py            # shared marking scheme for the backtest gate
    <contract-name>/
        oracle.py             # trusted reference impl + benchmark inputs + input
                              # generator for Class 1. Class 2 may use this
                              # module for laws, strategies, workloads and
                              # comparators, but has no reference to differ against.
        tests.py              # trusted host harness calls the candidate worker
        backtests/            # recorded episodes, when the target has history
            q1_fixture.json
            q1_expect.json
```

## Two classes of contract

`sis/contract.py` declares which gates apply, and the two classes differ in what
"correct" even means:

| | `OptimizationContract` (Class 1) | `FeatureContract` (Class 2) |
|---|---|---|
| The task | make a working function faster | build what a spec describes |
| Correct = | agrees with `oracle.reference` | passes `tests.py`, reproduces history |
| Better = | faster by `max_latency_ratio` | spec conformance; an optional SLO can reject correct code over its latency budget |
| Gates | ast · no-op · mypy · interface · acceptance · invariant · backtest · differential+benchmark | ast · mypy · interface · acceptance · invariant · backtest · optional SLO |

The Class-2 profile drops two gates on purpose. **No-op** has nothing to compare
against — a feature built for the first time has no prior version to be
identical to. **Differential + benchmark** both presuppose a reference that can
be evaluated on demand, and keeping the benchmark would quietly reimport the
wrong success criterion; a latency budget is a `DomainSLO`, which is explicitly
not a correctness gate.

`specs/roman/` is the first Class-2 contract: `to_roman` / `from_roman` built
from a spec, with no implementation to differ against.

Keep spec modules self-contained and independent of `sis/`. The acceptance,
invariant and backtest harnesses run trusted code on the host, calling a proxy
`target` whose methods execute in the candidate's sandbox worker. The benchmark
reference is host-side too. SLO remains an in-process sandboxed gate. Only JSON
crosses the worker boundary: returned tuples become lists and non-wireable
arguments are an exam fault. See [the code tour](../docs/CODE_TOUR.md#5-the-gauntlet-gate-by-gate-sisgauntletpy).

## Backtest fixtures

A backtest asks *did the candidate reproduce what actually happened?* — the
Class-2 analogue of the benchmark, with history as the reference
(`sis/backtest.py`). None of the shipped contracts (`sum_of_divisors`, `sort`,
`roman`) declares recorded backtests. They are pure functions with no historical
episodes to reproduce; inventing some would make the gate look exercised while
testing nothing.

Two files per episode, and the split is the reason:

```jsonc
// q1_fixture.json — the recorded input state
{
  "schema": 1,
  "event_time": "2026-03-31T23:59:59Z",   // when the episode occurred
  "args": [/* arguments to the contract's entry point */]
}

// q1_expect.json — the recorded outcome
{ "schema": 1, "value": /* what actually happened */ }
```

**Inputs and outcomes are separate files because the outcome is the exam
answer.** Showing a proposer the inputs describes a situation; showing it the
outcome lets it reproduce history without modelling anything. Keeping them apart
makes "show the inputs, never the answers" a file-level rule rather than
something a prompt author has to remember.

`event_time` stays optional for existing non-domain fixtures. A **domain
fixture** built from a sensor trace (`sis/fixture_builder.py`, OMNI-35) sets it to
the moment the question is asked, and carries everything a model may know at that
moment in `args[0]`: `{"as_of": ..., "readings": [...], "absent": [...]}`, the
as-of view of `sis/replay.py`. The gate never reads `event_time` itself, so what a
model may use has to be in `args`. Such a fixture arrives here only through
`python -m sis.contract_author promote` with the digest a human reviewed
([sensor runbook](../docs/SENSOR_RUNBOOK.md), "From a trace to a fixture").

When `event_time` is present it is **parsed and validated** (`sis/clock.py`), not
carried around as whatever string was in the file:

- it must be ISO-8601, and
- it must carry a timezone. A naive timestamp means "23:59, somewhere";
  replaying it silently assumes an offset the recorder never stated, and the
  result is a model that looks subtly wrong with nothing in the log explaining
  why. `Z` and `+09:00` are both fine.

The validation happens at parse time on purpose: a fixture with a broken
timestamp is broken *as recorded*, and discovering that during a replay — quite
possibly after the window to re-record has closed — is exactly the failure the
field exists to prevent. `ReplayClock.at()` drives a replay straight off it.

Each backtest names a **comparator** (default `within_tolerance`) resolved
by the trusted harness: the contract's own `oracle.py` first, then
`specs/comparators.py`. That is where a domain supplies a comparison the shared
set has no business knowing about — and where non-determinism will enter, since
a stochastic contract names a proper scoring rule instead and nothing else about
the gate changes (`docs/OMNITRACK_VISION.md` E2).

## Note for contributors

Adding a directory here does not need a policy change — `GUARDRAIL_DIRS`
protects the whole tree by path prefix, precisely so a contract can gain files
without the guarantee decaying.
