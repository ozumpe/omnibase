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
specs/<contract-name>/
    oracle.py     # trusted reference impl + benchmark inputs + input generator;
                  # copied into the gauntlet sandbox next to the candidate
    tests.py      # acceptance tests the candidate must pass (run in-sandbox)
```

These modules run **inside the sandbox** alongside untrusted candidate code, so
they must be self-contained: standard library only, no imports from `sis/`.

## Note for contributors

Adding a directory here does not need a policy change — `GUARDRAIL_DIRS`
protects the whole tree by path prefix, precisely so a contract can gain files
without the guarantee decaying.
