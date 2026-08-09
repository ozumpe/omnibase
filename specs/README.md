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
                              # generator; copied into the sandbox next to the candidate
        tests.py              # acceptance tests the candidate must pass (run in-sandbox)
        backtests/            # recorded episodes, when the target has history
            q1_fixture.json
            q1_expect.json
```

These modules run **inside the sandbox** alongside untrusted candidate code, so
they must be self-contained: standard library only, no imports from `sis/`.

## Backtest fixtures

A backtest asks *did the candidate reproduce what actually happened?* — the
Class-2 analogue of the benchmark, with history as the reference
(`sis/backtest.py`). Neither shipped target declares any: `sum_of_divisors` and
`sort` are pure functions with no history to reproduce, and inventing some would
make the gate look exercised while testing nothing.

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

`event_time` is optional today — nothing records it yet — and is in the schema
from version 1 anyway. A fixture written without it can never be replayed in
event time, and unlike code, history does not come round again to be
re-recorded. OMNI-23 populates and consumes it.

Each backtest names a **comparator** (default `within_tolerance`) resolved
inside the sandbox: the contract's own `oracle.py` first, then
`specs/comparators.py`. That is where a domain supplies a comparison the shared
set has no business knowing about — and where non-determinism will enter, since
a stochastic contract names a proper scoring rule instead and nothing else about
the gate changes (`docs/OMNITRACK_VISION.md` E2).

## Note for contributors

Adding a directory here does not need a policy change — `GUARDRAIL_DIRS`
protects the whole tree by path prefix, precisely so a contract can gain files
without the guarantee decaying.
