# Class-2 gauntlet contract — verifying built *features*, not just optimizations

**Status:** design sketch (not implemented). This is the plan for how the gauntlet
verifies *feature construction* — the work omnitrack actually needs — as an
extension of the existing `validate()` gate stack, not a redesign.

> Mirrored in Confluence (SD space) as a child of **The Validation Gauntlet**:
> <https://olafzumpe.atlassian.net/wiki/spaces/SD/pages/6357056>. This repo copy is
> the version-controlled source that evolves with the code; keep the two in sync.

## Motivation: two task classes, one gauntlet built for the first

The bootstrap gauntlet (`sis/gauntlet.py`) is tuned for one class of problem, and
the whole "server that models a slice of the world" thesis rests on a second class
it does not yet verify.

- **Class 1 — Optimization** (the bootstrap target, `runtime/target.py`). *"Here is a
  function that already works but is slow. Make it faster without breaking it."*
  Correctness = **agreement with a known-correct reference** on random inputs;
  "better" = **faster**. This is exactly what the differential-correctness +
  benchmark gates do, and it presupposes a slow-but-obviously-right oracle exists.

- **Class 2 — Feature construction** (omnitrack: a logistics planner, a building
  model, a traffic model). *"Here is a spec — track suppliers, model cargo routes,
  flag vulnerabilities, simulate alternatives. Build code that does that."* There is
  **no pre-existing correct version** to differ against, and "10% faster" is not what
  makes it correct.

The `reference(n)` + benchmark algorithm is inapplicable to Class 2. What a feature
needs instead is a contract derived from its **spec**.

### Which gates transfer

| Gate | Class 1 (optimization) | Class 2 (feature) |
|---|---|---|
| `ast.parse` | ✅ | ✅ always |
| `mypy --strict` | ✅ | ✅ always (+ enforces the interface `Protocol`) |
| `pytest` | supporting | ✅ **primary** — spec-derived acceptance tests |
| differential correctness vs reference | ✅ core | ❌ no reference exists → **invariants** replace it |
| benchmark ≥ margin | ✅ core | ❌ speed isn't the goal → **backtests / domain SLO** |
| QA review + human PR | ✅ | ✅ weightier |

For a feature the load-bearing question shifts from *"does it agree with a fast
reference?"* to ***"does it satisfy the spec's acceptance tests and the domain's
invariants?"***

## Design: make the *contract* the pluggable part

Today `validate()` hardcodes the optimization pipeline. Generalize so the contract
selects the gate profile; both classes flow through one entry point.

```python
# sis/gauntlet.py
def validate(code_str: str, contract: Contract, *, baseline_source: str | None = None) -> Result:
    for gate in contract.gates():          # cheapest-first, chosen by contract type
        result = gate.run(candidate_path, tmpdir, env, contract)
        if not result.passed:
            return result
    return Result(passed=True, ...)
```

Two concrete contracts share a base:

- **`OptimizationContract`** (Class 1, what exists today, generalized by
  [L5](KNOWN_ISSUES.md)): `entry`, `reference(n)`, `inputs()`, `min_speedup`.
  Gates: `ast → noop → mypy → pytest → differential+benchmark`.
- **`FeatureContract`** (Class 2, new):

```python
@dataclass(frozen=True)
class FeatureContract:
    name: str
    spec_ref: str                    # Confluence page id — provenance root
    entry_module: str                # where the SWE writes impl: runtime/logistics/planner.py
    public_api: Sequence[str]        # symbols the feature MUST export
    protocol: str | None             # a typing.Protocol the impl must satisfy (mypy-checked)
    acceptance_tests: str            # path to a trusted-authored pytest module
    invariants: Sequence[Invariant]  # property checks over generated inputs
    backtests: Sequence[Backtest]    # historical fixtures + tolerance
    slo: DomainSLO | None            # optional latency/accuracy budget (NOT a correctness gate)

    def gates(self) -> Sequence[Gate]:
        return [AstGate(), MypyGate(), InterfaceGate(), AcceptanceGate(),
                InvariantGate(), BacktestGate(), SloGate()]


@dataclass(frozen=True)
class Invariant:
    name: str
    strategy: str    # Hypothesis strategy factory (generates VALID domain inputs)
    check: str       # predicate fn(inputs, output) -> bool, defined in the contract module


@dataclass(frozen=True)
class Backtest:
    name: str
    fixture: str     # recorded historical input state (json/parquet)
    expect: str      # recorded real-world outcome
    tolerance: float # "reproduces history within X"
```

## The Class-2 gate stack

| # | Gate | Checks | Class-1 analog |
|---|---|---|---|
| 1 | **ast** | syntax | same |
| 2 | **mypy --strict** | shape-consistency + conforms to the `Protocol` (interface enforced at type level) | same |
| 3 | **interface** | import in sandbox; every `public_api` symbol exists with the right signature — fails fast if the LLM built the wrong shape | (none) |
| 4 | **acceptance** | run the **trusted-authored** pytest module; example-based spec conformance | pytest gate |
| 5 | **invariant** | for each invariant, generate N random *valid* inputs, run candidate, assert the predicate (conservation, capacity, non-negativity, monotonicity) | **differential correctness** — same anti-gaming role, invariants instead of a reference |
| 6 | **backtest** | run over historical fixtures; assert output within tolerance of recorded reality | **benchmark**, but "reference = history" |
| 7 | **SLO** (optional) | latency/accuracy budget from the spec | benchmark margin |
| 8 | QA + human PR | unchanged, mandatory | same |

Gates 5 and 6 are where the *"check against an independent source of truth the
candidate can't game"* principle survives the jump from optimization to
world-modeling. The implementation can't merely special-case the acceptance
examples, because **random inputs must still conserve cargo** and **history must
still be reproduced**. The source of truth changes — from a frozen slow function to
**domain invariants + historical ground truth** — but the spirit is identical.

## Worked example — the supply-route planner

Feature `plan(demand, suppliers, routes) -> Plan`:

```python
FeatureContract(
    name="supply_route_planner",
    spec_ref="CONF-1487",
    entry_module="runtime/logistics/planner.py",
    public_api=["plan", "Plan", "Demand", "Supplier", "Route"],
    protocol="PlannerProtocol",
    acceptance_tests="specs/planner/acceptance_test.py",   # ~8 hand-authored spec cases
    invariants=[
        Invariant("conservation", "supply_scenarios", "shipped_equals_met_demand"),
        Invariant("capacity",     "supply_scenarios", "no_route_over_capacity"),
        Invariant("non_negative", "supply_scenarios", "quantities_and_costs_nonneg"),
        Invariant("monotonicity", "supply_scenarios", "removing_supplier_never_cheaper"),
    ],
    backtests=[Backtest("2026Q1", "specs/planner/q1_state.json",
                        "specs/planner/q1_outcome.json", tolerance=0.05)],
    slo=DomainSLO(metric="latency", budget_ms=500),
)
```

- **Acceptance** cases: a 2-supplier scenario with a known optimal plan; an
  infeasible-demand case that *must* flag unmet demand; a single-route degenerate case.
- **Invariants** encode the domain physics. The planner may return a *different*
  optimal plan than the test author imagined and still pass, as long as it conserves
  cargo, respects capacity, and is monotonic. This is why invariants beat golden
  outputs for anything with multiple valid answers.
- **Backtest** validates the *model against reality*: over last quarter's actual
  state, land within 5% of realized cost.

## How a Confluence spec becomes a contract

```
intake page → PM refines to spec (acceptance criteria + invariants in prose)
            → CONTRACT-AUTHOR step: a trusted actor (QA, human-reviewed) translates
              criteria → acceptance_test.py, invariants → predicate fns, into the
              POLICY-FORBIDDEN specs/ space   ← authored ONCE, human-reviewed
            → SWE generates impl at entry_module to pass the contract
            → validate() runs the Class-2 profile IN THE SANDBOX (code is untrusted)
            → QA review + human PR
provenance: spec page → contract module → branch/PR → gauntlet verdict → outcome
```

## Two load-bearing principles carried over from Class 1

1. **The contract is immutable to the implementer.** `specs/` is classified
   `FORBIDDEN` in `sis/policy.py` (the same machinery that already protects the
   gauntlet, cost brakes, and adapters); the SWE writes *only* `entry_module`
   (`SOFT`). It cannot edit its own exam — the direct generalization of "the
   reference oracle must not live in the loop-mutable target."
2. **Author and implementer are different actors.** PM/QA author the acceptance
   criteria and invariants; the SWE implements. Separation of concerns = separation
   of trust — the anti-gaming property, made structural.

Everything runs in the existing subprocess/docker sandbox — generated feature code
is untrusted, so invariants and backtests execute *inside* the sandbox, never the
main process. New `reject_gate` values (`interface`, `acceptance`, `invariant`,
`backtest`, `slo`) slot alongside the existing
`ast/noop/mypy/pytest/correctness/benchmark/policy/timeout` in the episodic log.

## Reuse: engine vs library vs contract — not per-project reimplementation

A natural worry: *"do I implement a bespoke Class-2 checker for every kind of
project?"* No. There are three layers, and only the thinnest is per-feature. The
"Class-2 checks" as *mechanism* are written **once**.

| Layer | Scope | What it is |
|---|---|---|
| **Gate engine** — `InterfaceGate`, `AcceptanceGate`, `InvariantGate`, `BacktestGate`, `SloGate` | **write once** | Domain-agnostic runners: import a module, run a pytest file, generate inputs from a strategy and check a predicate, load a fixture and compare within tolerance. Zero domain knowledge. |
| **Invariant / strategy libraries** | **per *domain*, and shared** | Reusable predicate + generator *kinds*. A new project picks and parameterizes; it rarely writes these from scratch. |
| **The contract** — acceptance cases, chosen invariants, fixtures | **per *feature*** | Mostly *declared data* + a handful of small predicates — the spec made executable. |

**Analogy — it's a test framework.** You don't reimplement pytest per project; you
write *tests* on top of it. Here the invariant/backtest gates **are** the framework,
and a project's contract is its test suite. Just as projects share fixtures and
plugins, domains share **invariant libraries**.

**Invariants cluster into reusable kinds**, so the per-domain part is far smaller
than "bespoke checks per project":

- **Conservation / flow laws** — resources moving: logistics (cargo), energy (power),
  traffic (vehicles), finance (money). *"Nothing created or destroyed."*
- **Capacity / bounds** — no edge exceeds its limit. Universal to networks.
- **Monotonicity / sensitivity** — adding a resource never worsens the optimum; more
  disruption never lowers cost. Any optimization.
- **Non-negativity, round-trip, determinism, idempotence** — fully domain-agnostic.

A logistics project and a traffic project *share* conservation and capacity,
parameterized differently. You assemble a contract from a catalog far more than you
author one.

**What's genuinely irreducible — and it's not code:** *stating the domain laws*
("cargo is conserved," "removing a supplier can't lower the optimal cost"). That's
domain *expertise*, supplied by the PM/domain expert in the spec — a few sentences of
domain truth the write-once `InvariantGate` then enforces, not a checking mechanism
to build. In the target architecture the **contract-author actor** drafts the
acceptance tests + invariants from the spec (human-reviewed), so the human input
converges toward *"here are the laws of my domain"* rather than *"here is checking
code."*

So onboarding a new domain is **declaring a contract** (assisted, mostly data +
library invariants), not **implementing a checker**. Adding a genuinely new invariant
*kind* is a one-time addition to the shared library that every future project can
reuse. That reuse is exactly what makes "one server that builds features for many
domains" tractable rather than a per-domain rewrite.

## Language genericity — the ToolchainAdapter (a wanted feature)

The same reuse argument has a second axis: **language**. The engine can target *any*
language — "even Java" — because the target code only ever runs behind commands the
sandbox executes; the target's language is a contract detail, not an engine one.

**Important distinction:** the *engine* stays Python (Ray, the actors). "omnibase
builds Java" means the Python engine *orchestrates* building/testing Java **inside
the sandbox** — it targets Java, it does not become Java.

Today the only Python-specific wiring is the gauntlet's **gate commands**. Since the
sandbox already runs arbitrary commands, going language-generic is not a rewrite — it
is making those commands **adapter-declared** instead of hardcoded:

| Gate | Hardcoded (Python) | Generic form (per-language) |
|---|---|---|
| syntax / compile | `ast.parse` | `javac` / `tsc` / `cargo check` |
| types | `mypy --strict` | the language's static check |
| tests | `pytest` | `junit`/`gradle`, `jest`, `cargo test` |
| benchmark | Python harness | a per-language bench harness |

So the reuse model gains a middle layer — a **`ToolchainAdapter`** port sitting next
to the existing ports (Confluence/Jira/GitHub/AWS):

| Layer | Scope | What it is |
|---|---|---|
| **Engine** | write once | actor org · control loop · sandbox · episodic · policy · brakes |
| **`ToolchainAdapter`** | per *language*, shared | how to build / typecheck / test / benchmark in X (Python, Java, TS, Rust); each pairs with a per-toolchain docker image |
| **Contract** | per *target* | entry point · acceptance tests · invariants · inputs · margin |

Crucially, **invariants are language-independent**: `round-trip`, `sorted-permutation`,
`conservation`, `monotonicity` hold regardless of the implementation language, so the
same invariant library and the same contract can be pointed at a Python *or* a Java
implementation — differing only in the toolchain adapter. That is the strongest form
of the genericity claim: the correctness contract is reused *across languages*.

**Status:** wanted feature, not built. Depends on the `Contract` abstraction (L5
Layer 1) existing first. Scope: a `ToolchainAdapter` port + one adapter per language
+ a docker image per toolchain.

## Where this is genuinely hard (open problems)

- **Someone must supply the domain laws.** "Cargo is conserved," "removing a supplier
  can't lower cost" — the system can't invent these. The PM/domain expert states
  them. That's not a flaw; it is *where human domain expertise enters the loop*, and
  it is a small, stable input compared to the implementation.
- **The human-review burden moves — and improves.** You stop reviewing sprawling
  implementations and start reviewing **contracts** (specs-as-tests + invariants),
  which are smaller, more stable, and closer to intent. Arguably the whole payoff.
- **Backtests need history you may not have yet.** Early in a new domain, lean on
  invariants + acceptance tests; backtests come online as real data accumulates.
- **Property generators are real work.** A Hypothesis strategy that emits *valid*
  `Supplier`/`Route`/`Demand` graphs is non-trivial and per-domain.

## Relationship to existing work

- **[L5](KNOWN_ISSUES.md)** (gauntlet hardwired to `sum_of_divisors`) is the *Class-1*
  slice of the same "contract" idea: generalize the optimization pipeline so any
  target can declare its `entry` / `reference` / `inputs` / `min_speedup`. Doing L5
  first establishes the `Contract` abstraction and the policy-`FORBIDDEN` `specs/`
  space that this Class-2 design then extends.
- This design is an **extension** of the current architecture: same sandbox, same
  cheapest-first ordering, same `Result`/episodic plumbing, same policy
  classification. What's new is (a) the `FeatureContract` shape, (b) three new gates
  (interface/invariant/backtest), and (c) the contract-author step in the actor flow.
- **[`docs/SERVE_CANARY.md`](SERVE_CANARY.md)** is the other half: this doc covers
  the *offline* gauntlet; that one covers the *online* Ray Serve canary, arguing the
  two can't be designed separately because the canary needs exactly this contract's
  invariants to check correctness on live traffic (no stored expected output exists
  in production the way it does for a generated gauntlet input).

## Example first-target projects ("easy but not too easy")

Good first targets to point the generalized engine at have a **strong property
oracle** and a **small, closed domain** — so they exercise invariants and
feature-construction, not just speed, without a leap into full domain modelling.
Each has a property that makes it a clean contract demo:

| Target | Property that makes it a good contract | Notes |
|---|---|---|
| **Roman numeral ⇄ int** | round-trip: `to_roman(from_roman(s)) == s` | total function, tiny, unambiguous — the sweet spot |
| **A sort** | output is a sorted permutation of the input | the canonical invariant; many valid impls |
| **JSON / CSV transformer** | round-trip + schema conformance | realistic I/O; edge cases |
| **Expression evaluator** | matches a reference grammar | recursion → genuine edge cases |
| **LRU cache** | capacity never exceeded; hit/miss semantics | stateful; ordering invariants |
| **Rate limiter** | safety (never exceeds the limit) + liveness (eventually allows) | a taste of temporal properties |

**Recommended progression** (proves the whole thesis, cheaply, in two steps):

1. **Roman numerals in Python** — L5 Layer 1 (the `OptimizationContract`/`Contract`)
   + the `round-trip` invariant. No new toolchain; proves the contract generalises
   beyond `sum_of_divisors` and introduces the invariant mechanism.
2. **Re-point the *same contract* at a second language** (Java / TS / Rust) via a
   new `ToolchainAdapter` + image. Because the contract is literally reused across
   languages, this is the most convincing possible demonstration — the moment
   omnibase stops being "a Python optimiser" and becomes a language-agnostic
   software factory.

## Suggested sequencing

1. **L5 first** — introduce the `Contract` abstraction + `OptimizationContract`,
   migrate `sum_of_divisors` into a policy-`FORBIDDEN` spec, add a second
   optimization target (e.g. `is_prime`, or **roman numerals** with a round-trip
   invariant) to prove generalization.
2. **`FeatureContract` + interface/acceptance gates** — the minimum to build and
   verify a first trivial feature end-to-end (no invariants/backtests yet).
3. **Invariant gate** (property-based, Hypothesis) — the anti-gaming layer for features.
4. **[`docs/SERVE_CANARY.md`](SERVE_CANARY.md)** — reuses these same invariants as the
   *online* correctness gate for a Ray Serve canary (live traffic instead of
   generated inputs), and replaces the offline benchmark's noise-floor-prone
   fixed-input timing with real-traffic latency percentiles. Picks up right after
   step 3 above; see that doc for its own step-by-step sequencing.
5. **`ToolchainAdapter`** — make the gate commands adapter-declared; add a second
   language (the "even Java" step) reusing the same contract.
6. **Backtest gate + domain SLO** — once a domain with historical data exists.
7. **Contract-author actor step** — automate spec → contract with human review.
