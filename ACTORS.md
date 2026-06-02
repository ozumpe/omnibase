# Actors, Roles & External Subsystems

This is the authoritative spec for the actor org and how it touches the outside
world. `CLAUDE.md` and `DESIGN.md` reference this file.

## Coordinating principle: artifacts are the bus

Actors do **not** primarily coordinate through free-form chat. They coordinate by
reading and writing durable artifacts:

- **Confluence pages** — designs and specs
- **Jira issues** — epics, stories, spikes, bugs, and their status workflow
- **Git branches / PRs** — code and review

These artifacts are at once the **work queue**, the **inter-actor message bus**, the
**provenance/audit trail**, and the system's **long-term memory**. Every meaningful
handoff is a state change on an artifact (e.g., a SWE moves a Story to
"Ready for Review" — that *is* the message to QA), not just an in-memory message.

Design consequences:
- Keep LLM "negotiation" between actors **bounded** — actors converge by updating
  artifacts against explicit exit criteria, not by chatting indefinitely.
- Provenance comes for free: spec -> epic -> story -> branch/PR -> deploy -> outcome
  is reconstructable from the artifacts.

## Required external subsystems (ports & adapters)

The **capability is required; the product is replaceable.** Each subsystem sits
behind an interface (port) with a default adapter. Chosen for their existing MCP
connectors and industry-standard status, and defined as load-bearing because they
are the actors' shared coordination + memory substrate.

| Capability (port)         | Default adapter            | Used for |
|---------------------------|----------------------------|----------|
| Document Store            | Confluence (Atlassian/Rovo MCP) | Designs/specs; **intake channel** where non-technical users drop new designs to be refined & implemented |
| Work Tracker              | Jira (Atlassian/Rovo MCP)  | Epics/stories/spikes/bugs + status workflow |
| Version Control & Review  | GitHub (GitHub MCP / `gh`) | Branches, commits, PRs, CI |
| Cloud / Infra             | AWS                        | Compute, networking, deploy targets |
| Telemetry                 | cluster + cloud metrics    | Logs, metrics, traces |

Same adapter mechanism later connects **domain** actors to the modeled world's APIs
(traffic sensors, building systems, etc.) — the "real world" connection is uniform.

## Self-Model & hardware-model (the digital twin)

A dedicated Ray **named, detached** actor (the **Registry / SelfModel**) maintains a
live, queryable model of the system itself and its substrate. This is the **first
piece of the world the server models**, using the same machinery it will later point
at traffic / a building / a factory. Bootstrapping = the system modeling itself;
pointing it outward is then a configuration change, not a rewrite.

Tracks (software / self):
- Actor registry: each actor's role, state, parent/child, mailbox depth, restart
  count, node placement
- Deployment state: blue/green slots, which version is live, in-flight canaries
- Goals, SLOs, budgets, active self-improvement cycles
- Provenance graph: spec -> epic/story -> branch/PR -> deploy -> outcome

Tracks (hardware / runtime):
- Ray cluster nodes; CPU/RAM/GPU/disk/network headroom per node
- AWS substrate: instance types, region/AZ, autoscaling state
- OS / runtime versions

Why it is load-bearing:
- **Error collection** — map an exception to the exact actor, node, and code version
- **Blue/green handling** — know which slot is live and whether there is capacity to
  stand up a green environment
- **Building additional roles** — reason over its own org chart to decide it needs a
  new actor (e.g., a SecurityReviewer) and where to place it
- **Starting/monitoring actors** — the source of truth for supervision and placement

## Roles

Format per role: **Mandate / Owns / Spawns / Talks to / Real-world connections /
Ray form.**

### CEO — goals & cost
- **Mandate:** hold the project's goals and budget; decide what is worth doing.
- **Owns:** top-level objectives, the hard cost cap, the budget gate in the control
  loop, and kill-switch / circuit-breaker authority.
- **Spawns:** PM and CTO.
- **Talks to:** PM (value/scope), CTO (cost/progress; receives reports). Brokers
  goal-vs-feasibility-vs-cost between them.
- **Real world:** reads roll-up status (Jira) and cost telemetry; writes rarely —
  sets the high-level charter (top Confluence page / top epics).
- **Ray:** named, detached actor.

### PM — user experience & specs
- **Mandate:** own the product/UX and the correctness of specifications.
- **Owns:** Confluence design docs; final acceptance of behavior vs spec; spec
  change requests.
- **Spawns:** Designer actors (UI, higher-level outline).
- **Talks to:** CEO (value/scope); CTO (negotiates epics — reads & comments on
  epics/stories in Jira; signals when epics/stories must change -> CTO executes or
  delegates); reviews QA outcomes against intended UX.
- **Real world:** primary author/reader of Confluence (from templates); comments on
  Jira. **Intake point:** non-technical users drop new designs into a Confluence
  proposal space; the PM refines them into specs the system then builds.
- **Ray:** named, detached actor; spawns Designer children as needed.

### CTO — technical execution
- **Mandate:** turn designs into a buildable, costed plan and drive delivery.
- **Owns:** translation of Confluence designs -> Jira epics; the technical
  architecture; the initial infra stories per epic; cost/progress coordination.
- **Spawns/instructs:** DevOps, SWE (incl. SWE leads), QA.
- **Talks to:** PM (negotiate epics; absorb spec changes), CEO (report cost &
  progress), and down to the engineering roles. Engineers may correct/push back but
  coordinate changes through the CTO.
- **Real world:** writes/curates Jira epics; reads Confluence; governs the Git
  branch policy; watches telemetry & cost.
- **Ray:** named, detached actor.

### SWE (and SWE lead) — implementation
- **Mandate:** implement features, fix bugs, keep infra requirements current.
- **SWE lead owns:** detailed planning — breaks epics into Spikes/Stories in Jira,
  grounded in the Confluence designs.
- **Talks to:** CTO (plan/architecture), QA (review feedback loop), DevOps (infra
  needs).
- **Real world:** reads Jira stories + Confluence designs; writes code on **feature
  branches** in Git; opens PRs; moves a story to "Ready for Review" only after a
  clean commit/test. **Never commits to main.**
- **Ray:** actors spawned by the CTO; shorter-lived per task; under supervision
  (restart/retry).

### QA — verification
- **Mandate:** verify that what was built matches the Jira stories and Confluence
  specs.
- **Trigger:** acts when a SWE sets a story "Ready for Review".
- **Talks to:** SWE (returns discrepancies -> sets Jira to "TBD" with feedback);
  reads PM's specs.
- **Real world:** reads Confluence + the Jira story; runs/inspects the
  implementation; writes Jira state changes/comments. In the control loop, QA + the
  deterministic gauntlet (tests/benchmark) are the review gate — QA **augments**, it
  does not replace, the deterministic checks.
- **Ray:** actors spawned by the CTO.

### DevOps — infrastructure & operations
- **Mandate:** provide minimal-but-scalable infra and CI/CD; keep the system
  observable.
- **Owns:** AWS infra + pipelines; blue/green & canary mechanics; log/metric
  monitoring.
- **Trigger:** acts on Jira stories (initial infra stories authored by the CTO per
  epic). May correct, but coordinates infra changes with the CTO.
- **Talks to:** CTO (infra plan/cost), SWE (deploy needs), everyone via telemetry.
- **Real world:** provisions AWS; runs GitHub Actions/CI; executes canary deploys via
  Ray Serve; monitors logs/metrics; files bug/defect Jiras (exceptions, missing or
  insufficient functionality) and feeds the SelfModel's error collection.
- **Ray:** actors spawned by the CTO; some long-lived (monitors).

### Designer — UI / higher-level outline
- Spawned by the PM for UI and high-level project outlines; writes/illustrates in
  Confluence.

## Roles mapped onto the self-improvement control loop

- **Budget & goal gate** -> CEO
- **Spec / design** -> PM (+ Designer), in Confluence
- **Plan (epics)** -> CTO, in Jira; SWE leads break into stories/spikes
- **Propose / implement** -> SWE, on a feature branch
- **Validation gauntlet** -> deterministic gates + QA review; benchmark/sandbox via
  DevOps tooling
- **Canary deploy / monitor** -> DevOps via Ray Serve; SelfModel tracks the live slot
- **Promote / rollback** -> DevOps executes; outcome logged to provenance (SelfModel
  + episodic memory)
- **Circuit breaker / freeze** -> CEO authority

## Intake flow (non-technical users)

Human drops a new design into a Confluence proposal space -> PM refines it into a
spec -> CTO turns it into epics -> normal build/review/deploy lifecycle. This is how
the running system receives new products/models to build.
