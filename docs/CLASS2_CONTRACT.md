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

## Suggested sequencing

1. **L5 first** — introduce the `Contract` abstraction + `OptimizationContract`,
   migrate `sum_of_divisors` into a policy-`FORBIDDEN` spec, add a second
   optimization target (e.g. `is_prime`) to prove generalization.
2. **`FeatureContract` + interface/acceptance gates** — the minimum to build and
   verify a first trivial feature end-to-end (no invariants/backtests yet).
3. **Invariant gate** (property-based, Hypothesis) — the anti-gaming layer for features.
4. **Backtest gate + domain SLO** — once a domain with historical data exists.
5. **Contract-author actor step** — automate spec → contract with human review.
