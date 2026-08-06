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
with two implementations: in-memory (the credential-free default) and real
(Confluence / Jira / GitHub / AWS). Roles never touch an adapter directly — they
go through the `Workspace` actor, which is the shared artifact bus.

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
        +open_pr(branch, title, artifact) PullRequest
        +live_target_source() str
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

    DocumentStore <|.. InMemoryDocumentStore
    DocumentStore <|.. ConfluenceDocumentStore
    WorkTracker <|.. InMemoryWorkTracker
    WorkTracker <|.. JiraWorkTracker
    VersionControl <|.. InMemoryVersionControl
    VersionControl <|.. GitHubVersionControl
    Cloud <|.. InMemoryCloud
    Cloud <|.. RealCloud

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

    class SelfModel {
        <<Ray actor, detached>>
        +register(name, role, parent)
        +set_slot(slot, version)
        +register_contract(c)
        +contract_for(target_path) OptimizationContract
        +record(kind, ref) None
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

    SelfModel "1" o-- "*" OptimizationContract : registry
    OptimizationContract "1" --> "1" Oracle : oracle_path
    gauntlet ..> OptimizationContract : reads
    gauntlet ..> Oracle : copies into sandbox
    gauntlet --> Result

    note for Oracle "POLICY-FORBIDDEN. The implementer cannot edit its own exam."
    note for RealCloud "shift_traffic and live_metrics raise NotImplementedError until ServeCloud lands."
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

`org.run_cycle()`, the happy path. The point ACTORS.md makes in prose and this
makes visible: **roles do not chat.** Every handoff is a state change on a
durable artifact, which is simultaneously the work queue, the message bus and
the audit trail.

```mermaid
sequenceDiagram
    autonumber
    actor Human
    participant CEO
    participant PM
    participant CTO
    participant SWE
    participant G as gauntlet
    participant QA
    participant Ops as DevOps
    participant WS as Workspace
    participant SM as SelfModel

    Human->>WS: drop proposal into the intake space
    Note over CEO: brakes first — no spend before the gate
    CEO->>CEO: breaker_open? approve_budget(estimate)
    alt breaker open or over budget
        CEO-->>Human: circuit_breaker_open / budget_denied
    end

    PM->>WS: refine_proposal() → spec page
    CTO->>WS: plan() → epic + stories

    SWE->>SM: contract_for(target_path)
    SM-->>SWE: OptimizationContract
    SWE->>WS: live_target_source()
    Note right of SWE: baseline from the MERGED target,<br/>never the stale local file (H1)
    SWE->>G: measure_baseline(source, contract)
    SWE->>SWE: propose(source, baseline)
    SWE->>G: validate(candidate, contract)
    G-->>SWE: Result(passed, reason, latency)

    alt gauntlet rejected the candidate
        SWE->>WS: transition(story, TBD)
        Ops->>WS: file_bug()
        CEO->>CEO: report_outcome(success=False)
    else candidate passed
        SWE->>SWE: policy.authorize_change(target)
        SWE->>WS: create_branch, commit, open_pr
        SWE->>WS: transition(story, Ready for Review)
        QA->>G: validate() again, same merged baseline
        QA->>WS: transition(story, Done)
        Ops->>WS: deploy_canary(version) → green slot
        Ops->>SM: set_slot("green", version)
        PM->>WS: accept(spec)
        CEO->>CEO: report_outcome(success=True)
    end

    Note over Human,SM: the loop stops here.<br/>Merging the PR and promoting to live are human acts.
    CEO->>SM: provenance: charter → spec → epic → story → PR → outcome
```

---

## 3. Activity — the validation gauntlet

`gauntlet.validate()`. Cheapest and safest checks first; the first failure
short-circuits, so an expensive gate never runs on a candidate a cheap one
already rejected. Everything inside the dashed boundary executes **candidate
code**, and therefore runs in the sandbox — never in the actor process, which
holds credentials.

```mermaid
flowchart TD
    A[/"candidate source + contract"/] --> B{"ast.parse"}
    B -- "SyntaxError" --> RJ["reject: ast"]
    B -- ok --> C{"identical to baseline?"}
    C -- "yes" --> NC["no_change: benign.<br/>No bug filed, breaker untouched."]
    C -- "no" --> S

    subgraph S["sandbox: scrubbed env, no network, per-gate timeout"]
        direction TB
        D{"mypy --strict"} -- "fail" --> RJ2["reject: mypy"]
        D -- ok --> E{"exports contract.entry?"}
        E -- "no" --> RJ3["reject: interface"]
        E -- ok --> F{"contract acceptance tests"}
        F -- "fail" --> RJ4["reject: pytest"]
        F -- ok --> G{"agrees with oracle.reference<br/>on random inputs?"}
        G -- "no" --> RJ5["reject: correctness<br/>possible benchmark gaming"]
        G -- ok --> H["time candidate and baseline<br/>over oracle.BENCH_INPUTS"]
    end

    H --> I{"within contract.max_latency_ratio?"}
    I -- "no" --> RJ6["reject: benchmark"]
    I -- "yes" --> P["pass → PR opened → human review"]

    style NC fill:#e8f0e8
    style P fill:#e8f0e8
```

Two cross-cutting protections wrap every gate that runs candidate code: the
sandbox (`SIS_SANDBOX=subprocess` by default, or kernel-enforced `docker` —
mandatory for a real LLM proposer) and a per-gate timeout, so an infinite loop
is killed rather than left to hang. A gate that *cannot run* — a missing oracle
or acceptance suite — fails closed with a `harness:` reason, so a broken harness
is never recorded as a verdict on the candidate.

---

## 4. State — issue workflow and deploy slots

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
    GreenDeployed --> Promoted : human merges the PR
    Promoted --> BlueOnly : green becomes the new blue

    note right of GreenDeployed
        While green is occupied,
        loop.serve() holds the next
        cycle: one canary in flight.
    end note
```

> **Known gap:** nothing observes the human merge today, so the
> `GreenDeployed → Promoted` transition has no automatic trigger — `merge_pr`
> raises `RequiresHumanApproval` in every adapter and there is no post-merge
> path. Tracked as an open problem in [`SERVE_CANARY.md`](SERVE_CANARY.md).
