# Diagrams

UML views of omnibase, in Mermaid so they render wherever the markdown is read
and stay in the same commit as the code they describe. Prose lives in
[`ACTORS.md`](../ACTORS.md), [`DESIGN.md`](../DESIGN.md) and
[`CODE_TOUR.md`](CODE_TOUR.md); these are the pictures.

> Keep these honest. A diagram that drifts is worse than none, because it is
> believed. If you change a port, a gate, or a workflow state, change the
> diagram in the same PR.

Contents:
1. [Class — ports, adapters and the contract](#1-class--ports-adapters-and-the-contract)
2. [Sequence — one self-improvement cycle](#2-sequence--one-self-improvement-cycle)
3. [Activity — the validation gauntlet](#3-activity--the-validation-gauntlet)
4. [State — issue workflow and deploy slots](#4-state--issue-workflow-and-deploy-slots)

---

## 1. Class — ports, adapters and the contract

The hexagonal core. Every external capability is a `Protocol` in `sis/ports.py`
with recording/default and real adapters. `Workspace` is the shared artifact
bus for document/tracker/version-control operations. `ServeCloud` is a separate
opt-in traffic backend owned by DevOps. Sensor consumers receive their sensor
explicitly; they do not read it through Workspace. All seven roles and shared
actors are centrally bootstrapped as named, detached actors.

```mermaid
classDiagram
    direction LR

    class DocumentStore {
        <<Protocol>>
        +create_page(space, title, body) Page
        +get_page(page_id) Page
        +list_pages(space, label) list~Page~
        +archive_page(page_id) None
    }
    class WorkTracker {
        <<Protocol>>
        +create_issue(type, summary) Issue
        +transition(issue_id, status, comment) Issue
        +children(parent_id) list~Issue~
        +delete_issue(issue_id) None
    }
    class VersionControl {
        <<Protocol>>
        +create_branch(name, base) Branch
        +commit(branch, message) str
        +open_pr(branch, title, artifact, path) PullRequest
        +get_pr(pr_id, path) PullRequest
        +live_target_source(path) str
        +merge_pr(pr_id) PullRequest
    }
    class Cloud {
        <<Protocol>>
        +deploy_canary(version, metrics) DeployRecord
        +shift_traffic(version, fraction) None
        +live_metrics(version, window_s) dict
        +promote(version) DeployRecord
        +rollback(version) None
    }
    class Telemetry {
        <<Protocol>>
        +emit(event) None
        +events() list
    }

    class InMemoryDocumentStore
    class InMemoryWorkTracker
    class InMemoryVersionControl
    class InMemoryCloud
    class ConfluenceDocumentStore
    class JiraWorkTracker
    class GitHubVersionControl
    class RealCloud
    class ServeCloud
    class Notifier {
        <<Protocol>>
        +notify(severity, title, body) str
    }
    class Sensor {
        <<Protocol>>
        +read(start, end, by) list~Reading~
    }
    class Reading {
        +str source
        +str series
        +float value
        +str unit
        +datetime event_time
        +datetime known_at
    }
    class SimSensor
    class EIASensor
    class InMemoryNotifier
    class SNSNotifier
    Sensor <|.. SimSensor
    Sensor <|.. EIASensor
    Sensor ..> Reading
    Notifier <|.. InMemoryNotifier
    Notifier <|.. SNSNotifier

    DocumentStore <|.. InMemoryDocumentStore
    DocumentStore <|.. ConfluenceDocumentStore
    WorkTracker <|.. InMemoryWorkTracker
    WorkTracker <|.. JiraWorkTracker
    VersionControl <|.. InMemoryVersionControl
    VersionControl <|.. GitHubVersionControl
    Cloud <|.. InMemoryCloud
    Cloud <|.. RealCloud
    Cloud <|.. ServeCloud

    class Workspace {
        <<Ray actor, detached>>
        +create_page() Page
        +transition() Issue
        +open_pr() PullRequest
        +deploy_canary() DeployRecord
        +emit() None
    }
    Workspace o-- DocumentStore
    Workspace o-- WorkTracker
    Workspace o-- VersionControl
    Workspace o-- Cloud
    Workspace o-- Telemetry
    Workspace o-- Notifier

    class SelfModel {
        <<Ray actor, detached>>
        +register(name, role, parent)
        +set_slot(slot, version)
        +register_contract(c)
        +contract_for(target_path) Contract
        +record(kind, ref) None
    }

    class Contract {
        <<abstract>>
        +gate_profile() tuple~GateName~
    }
    class FeatureContract {
        <<frozen dataclass>>
        +str name
        +str entry
        +str target_path
        +tuple public_api
        +tuple invariants
        +tuple backtests
        +DomainSLO slo
    }
    class OptimizationContract {
        <<frozen dataclass>>
        +str name
        +str entry
        +str target_path
        +str oracle_path
        +str tests_path
        +float max_latency_ratio
        +int diff_trials
    }
    class Oracle {
        <<module under specs/>>
        +reference(args) Any
        +random_input(rng) tuple
        +BENCH_INPUTS list
    }
    class gauntlet {
        <<module>>
        +validate(code, contract) Result
        +measure_baseline(src, contract) float
    }
    class Result {
        <<frozen dataclass>>
        +bool passed
        +str reason
        +float latency_seconds
    }

    Contract <|-- OptimizationContract
    Contract <|-- FeatureContract
    SelfModel "1" o-- "*" Contract : registry
    OptimizationContract "1" --> "1" Oracle : oracle_path
    gauntlet ..> Contract : selects gates
    gauntlet ..> Oracle : trusted reference and laws
    gauntlet --> Result

    note for Oracle "POLICY-FORBIDDEN. The implementer cannot edit its own exam."
    note for RealCloud "Recording placeholder; ServeCloud is the separate traffic backend."
```

The canary types are the online mirror of the same idea — the contract's
predicates, applied to live traffic instead of generated inputs:

```mermaid
classDiagram
    direction LR

    class BoundInvariant {
        <<Protocol>>
        +str name
        +check(request, response) bool
    }
    class LiveSample {
        <<frozen dataclass>>
        +Any request
        +Any candidate_response
        +Any baseline_response
    }
    class CanaryVerdict {
        <<frozen dataclass>>
        +int samples
        +int invariant_violations
        +int response_disagreements
        +float baseline_p95
        +float candidate_p95
        +bool passed
        +str reason
    }
    class CanaryMode {
        <<enumeration>>
        SHADOW
        SPLIT
    }
    class canary {
        <<module>>
        +evaluate_canary(invariants, samples, ...) CanaryVerdict
    }

    canary ..> BoundInvariant
    canary ..> LiveSample
    canary ..> CanaryMode
    canary --> CanaryVerdict

    note for LiveSample "baseline_response is populated in SHADOW mode only. A weighted SPLIT routes each request to one version, so no paired baseline exists."
```

---

## 2. Sequence — one self-improvement cycle

`org.run_cycle()` can finish a feature or add one accepted optimization step.
Plans are reused across steps and rejected attempts feed the next prompt. The
sequence below compresses artifact calls; actors coordinate through Workspace,
with SelfModel retaining contract/deploy/provenance links.

```mermaid
sequenceDiagram
    autonumber
    actor Human
    participant Driver as org / loop
    participant CEO
    participant SWE
    participant G as gauntlet
    participant WS as Workspace
    participant QA
    participant Ops as DevOps
    participant SM as SelfModel

    Driver->>CEO: pause / breaker / budget checks
    Driver->>SWE: already_built(contract)
    alt Class-2 target already passes
        SWE-->>Driver: already built
        Driver->>CEO: record_neutral(cost=0)
    else build or improve
        Driver->>WS: reuse feature plan, or create spec and plan via PM/Designer/CTO
        Driver->>SWE: implement(story, contract)
        SWE->>WS: source from feature head or merged base
        SWE->>G: validate(proposed source, contract)
        G-->>SWE: result
        alt rejected or neutral
            SWE-->>Driver: reason and proposal cost
            Driver->>CEO: record outcome or neutral spend
            Note over Driver,WS: Counted failures file bugs, neutral outcomes do not.
        else accepted optimization, unfinished
            SWE->>WS: commit accepted step to feature branch
            SWE-->>Driver: feature_step
            Driver->>CEO: record outcome and cost
            Note over Driver,SWE: Continue with the same feature next cycle.
        else feature finished
            SWE->>WS: commit and open fixed-head PR
            Driver->>QA: review(story, PR)
            QA->>G: validate again
            QA-->>Driver: verdict
            Driver->>Ops: canary(PR), if approved
            Ops->>SM: green slot / pending PR
            Driver->>CEO: record outcome and cost
            Note over Driver,Human: Passing result holds for human review.
            Human->>WS: merge or close PR externally
            Driver->>Ops: observe_merge(PR)
            Ops->>WS: read PR state
            alt merged
                Ops->>WS: promote recording backend (or ServeCloud directly)
                Ops->>SM: live version, clear green and pending PR
            else closed or missing
                Ops->>SM: release hold
            end
        end
    end
```

`merge_pr()` always raises `RequiresHumanApproval` in the engine adapters.
Promotion executes only after observing the human merge. A Class-2 feature
finishes at its first passing step; optimization completion follows the feature
step limit or no further gain. See [STAGED_DELIVERY.md](STAGED_DELIVERY.md).

---

## 3. Activity — the validation gauntlet

`Contract.gate_profile()` chooses the path. Undeclared invariants/backtests and
an absent SLO have no work to run. Candidate execution stays in the sandbox;
trusted acceptance/invariant/backtest harnesses and the benchmark judge run on
the host. SLO is the remaining in-process sandboxed gate.

```mermaid
flowchart TD
    A["source + contract"] --> B{"syntax valid?"}
    B -- no --> R["reject"]
    B -- yes --> C{"optimization?"}
    C -- yes --> N{"byte-identical to baseline?"}
    N -- yes --> Z["neutral no_change"]
    N -- no --> T["strict types"]
    C -- no --> T
    T --> I["interface via candidate worker"]
    I --> H["trusted host harnesses: acceptance, invariant, backtest"]
    H --> K{"optimization?"}
    K -- yes --> D["host reference checks candidate answers"]
    D --> W["host times isolated candidate and baseline workers"]
    W --> V{"paired total cost + interval"}
    V -- clears --> P["pass"]
    V -- misses --> R
    V -- unproven --> U["neutral inconclusive"]
    K -- no --> S["optional SLO inside sandbox"]
    S --> P
    T -. failure .-> R
    I -. failure .-> R
    H -. failure .-> R
    D -. failure .-> R
    S -. failure .-> R
```

Each gate's completion is checked for exam tampering and the required verdict.
A failed sandboxed gate gets a trusted self-check: if that also fails, the
reason is `harness:`. Gate timeouts kill hangs. Docker's read-only exam mount,
no network, credential-free environment and CPU isolation protect the host;
subprocess mode is a soft guard and cannot enforce that confinement.

Only JSON crosses the worker boundary; tuples become lists and non-wireable
arguments are an exam fault. Timed inputs are never reused after either worker
has seen them; batches run without per-call Python harness work. Docker workers
get distinct single CPUs, swapped halfway. See [CODE_TOUR.md](CODE_TOUR.md).

Serve is outside this boundary and remains refused for non-stub proposers and
Class-2 contracts. See [SERVE_CANARY.md](SERVE_CANARY.md).

---

## 4. State — issue workflow and deploy slots

The issue diagram is the current engine workflow: QA verification marks a story
Done before the human PR merge. Repository contributor Jira stories are moved
Done on merge; that convention is separate from the engine's issue transitions.

**Issue lifecycle** (`sis.ports.IssueStatus`). QA bouncing a story to `TBD`
rather than failing it outright is what makes the SWE↔QA loop an artifact
exchange instead of a conversation.

```mermaid
stateDiagram-v2
    [*] --> Backlog
    Backlog --> ToDo : CTO plans the epic
    ToDo --> InProgress : SWE picks it up
    InProgress --> ReadyForReview : gauntlet passed, PR opened
    InProgress --> TBD : gauntlet rejected the candidate
    ReadyForReview --> Done : QA verified
    ReadyForReview --> TBD : QA found a discrepancy
    TBD --> InProgress : SWE retries with feedback
    Done --> [*]

    note right of TBD
        Not a terminal failure.
        The feedback rides on the
        artifact, not in a message.
    end note
```

**Deploy slots** (`SelfModel._slots`). Blue is live, green is the canary. The
loop can put a candidate in green; only a human can make it blue.

```mermaid
stateDiagram-v2
    [*] --> BlueOnly : bootstrap
    BlueOnly --> GreenDeployed : DevOps.canary()
    GreenDeployed --> BlueOnly : DevOps.retire_canary()
    GreenDeployed --> BlueOnly : observe_merge reads PR closed or missing
    GreenDeployed --> Promoted : DevOps.observe_merge() reads<br/>the PR back as merged
    Promoted --> BlueOnly : green becomes the new blue

    note right of GreenDeployed
        While green is occupied,
        loop.serve() holds the next
        cycle: one canary in flight.
    end note

    note right of Promoted
        loop.serve(watch_merges=True)
        polls observe_merge() each tick,
        which is what actually fires
        this transition.
    end note
```

`DevOps.observe_merge()` (OMNI-15) is what makes `GreenDeployed → Promoted`
real: it reads the PR back from version control and, only if a human has
*already* merged it, promotes and frees green. `merge_pr` still raises
`RequiresHumanApproval` in every adapter, so the agent can never make that
condition true itself — it can only notice that a human did. Before this
existed, the transition had no trigger at all and the loop idled forever after
one successful cycle.
