# Brake state & the oracle-versioned circuit breaker (design)

**Status:** **Parts 1–2 & 6 implemented (2026-07-29)** — the `sis` namespace +
`get_if_exists` (M2) and CEO brake/spend state persisted to the episodic store and
rehydrated on restart (L9), with a `reset_breaker()` RPC. **Parts 3–5 remain a
design sketch** — the breaker-cause split and the oracle-versioned auto-reset need
the L5 target contract to hash against (see the sequencing note at the end). See
`docs/KNOWN_ISSUES.md` for the issue IDs and `docs/CLASS2_CONTRACT.md` for the
target/oracle contract this builds on.

> Mirrored in Confluence (SD space) as a child of **Guardrails & Operations**:
> <https://olafzumpe.atlassian.net/wiki/spaces/SD/pages/6946818>. This repo copy is
> the version-controlled source that evolves with the code; keep the two in sync.

## Why these three interlock

- **M2** — detached actors live in an anonymous Ray namespace, so a persistent
  cluster can't find them across runs (duplicates + leaks + silent state reset).
- **L9** — the CEO's brake state (spend total, consecutive-failure count, tripped
  flag) lives only in that actor's memory; a fresh run clears it.
- **L5** — a circuit-breaker trip on a *converged* target is really the signal
  "the oracle is exhausted; redefine it," not "something is broken."

Together they answer one question: **what is the lifetime and meaning of the
CEO's brake state on a real, persistent cluster — and when should it reset?**

## Part 1 — M2: one shared namespace

`org.bootstrap()` calls `ray.init(...)` with no namespace, so every process gets a
random one; `ray.get_actor("CEO")` from a later run misses the detached actor and
duplicates it.

**Fix (small):**
- `ray.init(namespace="sis", ...)` (or `address="auto", namespace="sis"` on AWS) —
  one shared namespace so lookups find the existing actors across runs.
- Replace the `try ray.get_actor / except ValueError: create` in `_get_or_create`
  with Ray's atomic
  `Cls.options(name=..., namespace="sis", lifetime="detached", get_if_exists=True).remote()`
  — closes the race where two concurrent bootstraps both create.

This is a genuine prerequisite before any persistent/AWS cluster.

## Part 2 — L9: brake-state lifetime

On a persistent cluster you *want* brake state to persist across runs — the spend
cap should span the cluster's life, not reset every `python main.py`. Two levels:

| Option | Survives across runs | Survives cluster/actor restart | Effort |
|---|---|---|---|
| **(a)** State stays in the CEO actor (M2 namespace makes the actor persist) + a `reset_breaker()` admin RPC | ✅ | ❌ (in-memory) | small |
| **(b)** Persist CEO state to a durable store + rehydrate on bootstrap | ✅ | ✅ | medium (the real L9 fix) |

**Recommendation:** ship **(a)** for the first small AWS run; do **(b)** when
durability across restarts is needed. The oracle-hash mechanism below is part of
**(b)**.

## Part 3 — Two trip causes, not one

The consecutive-failure breaker currently counts *every* `rolled_back` cycle the
same way. The live L3 runs showed two very different causes hiding behind that:

1. **Goal exhaustion (convergence).** The failures are all
   `reject_gate=benchmark` at the noise floor (runs 4/5 on `testrun`): the loop
   can't make progress because there is nothing left to win. **Not a malfunction.**
2. **Quality failure.** Failures at `mypy`/`pytest`/`correctness`: the proposer is
   emitting bad code. **This is the genuine safety case** the breaker exists for.

These need different handling, and different reset semantics:

- **Convergence → a `goal_exhausted` escalation**, not the safety breaker. It
  carries the remedy ("target X converged — redefine the oracle: enlarge inputs /
  new target / mark solved"), the same way `no_change` was made benign one level
  down (`record_neutral` sidesteps the failure counter). Ideally noise-floor
  benchmark-rejects don't count toward the safety breaker at all.
- **Quality failure → the classic safety breaker** + page-a-human; cleared only by
  an explicit human reset (someone confirms the proposer issue is understood).

## Part 4 — The oracle-versioned breaker

**Idea:** persist the breaker state tagged with a **hash of the current oracle**,
and auto-reset when that hash changes. Redefining the oracle *is* the reset signal
— no manual reset in the common (convergence) case.

Three refinements make it correct and safe:

### 4.1 Reset the breaker, not the spend (or you build a budget bypass)

The CEO holds two kinds of state with different reset semantics:
- **breaker/failure state** (consecutive failures, tripped flag) — oracle-scoped;
  hash-reset. ✅
- **hard spend cap + cost accounting** — a *financial* guardrail, independent of
  what's being optimized. It must **not** reset on an oracle change; otherwise the
  loop (or an impatient operator) bypasses the hard cap by nudging the oracle.

So the hash-reset is **selective**: clear the failure/breaker state, carry the
spend total forward. Spend resets only on a deliberate budget-period boundary.

### 4.2 Hash only loop-**immutable** content (or the loop clears its own breaker)

If anything the loop can write feeds the hash, the loop can trip-then-reset its own
safety breaker by changing that input — the classic "rewriter edits its own
guardrail" failure. The reset key must derive **exclusively from human-owned
state**:
- the oracle/target contract itself — already in policy-**FORBIDDEN** space, so the
  loop can't touch it (`sis/policy.py`); and
- the merged target baseline — SOFT, but **human-gated at the PR merge**, so the
  loop alone can't change `main`.

Both require a human to change, so the breaker can **never** self-clear. State this
as an invariant: *the reset key is a hash of human-owned state only.*

### 4.3 Only the convergence breaker hash-auto-resets

Hash-on-oracle-change is exactly right for **goal exhaustion** (a new oracle
plausibly has fresh headroom). A **quality trip** is about the proposer, not the
oracle, and is independent of which target you point at — changing the oracle must
not wave it away. So:
- convergence breaker → oracle-hash-scoped, auto-resets;
- safety breaker → separate state, explicit human reset.

### 4.4 Minor: hash semantic fields, not raw bytes

Hash the *semantic* contract fields (entry symbol, reference oracle, inputs/sizing,
min-speedup margin) + the baseline, not the raw file bytes — so a cosmetic comment
edit doesn't cost a re-converge cycle.

## Part 5 — The resulting closed loop

```
converged target → benchmark-rejects at the noise floor → goal_exhausted trip
   → escalate "redefine the oracle" (carrying the remedy)
   → human edits the FORBIDDEN oracle contract (or resets the target)
   → oracle-hash changes
   → next bootstrap: stored hash ≠ current hash → clear the *convergence* breaker
     (spend carried forward; safety breaker untouched)
   → loop resumes against the new oracle
```

The breaker becomes a **versioned, self-healing progress signal**. The only manual
actions left are the ones that *should* be manual: redefining the goal, and
acknowledging a broken proposer. Mapped onto the org, a convergence trip is the
loop reaching the edge of its current goal and asking leadership (PM/CTO) to set a
new one — a concrete step toward self-directed goal-setting.

## Part 6 — Where the persisted record lives

The episodic store (`sis/episodic.py`) is the natural home — it's already the
durable substrate behind a port. It is append-only, so add a small **latest-wins
`CEO state` record**: `{ oracle_hash, spent_usd, consecutive_failures,
convergence_tripped, safety_tripped, accepted }`. On bootstrap the CEO rehydrates
from it, compares the stored `oracle_hash` to the freshly-computed one, and clears
the convergence breaker on mismatch while carrying spend forward.

## Relationship to L5 & sequencing

"Redefine the oracle" is only *actionable* once the oracle is a rewritable
per-target contract — i.e. **L5 Layer 1** (the `OptimizationContract`:
`entry`/`reference`/`inputs`/`min_speedup`, in FORBIDDEN space). The hash in Part 4
is computed over exactly that contract. Suggested order:

1. **M2 + L9(a)** — namespace + `get_if_exists` + `reset_breaker()` + documented
   persistent-cluster semantics. Small; unblocks AWS.
2. **Breaker classification** — split `goal_exhausted` (convergence) from the safety
   breaker; make convergence benign-but-escalating. Small; fixes the run-4/5
   coin-flip-trip pathology.
3. **L5 Layer 1** — the target/oracle contract, so "redefine the oracle" has
   something to rewrite.
4. **L9(b) + the oracle-versioned breaker** — persist CEO state with the oracle
   hash; auto-reset the convergence breaker on change.
