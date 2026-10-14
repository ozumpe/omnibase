# Actors, Roles & External Subsystems

This is the authoritative spec for the actor org and how it touches the outside
world. `CLAUDE.md` and `DESIGN.md` reference this file. Role mandates describe
the intended organization; the current implementation boundary is stated below.

**Current runtime:** `org.bootstrap()` centrally creates all seven roles plus
`Workspace` and `SelfModel` as named, detached actors in the `sis` namespace,
using `get_if_exists=True`. CEO/PM/CTO do not currently create or own Ray child
actors. The spawning relationships in the role descriptions are design goals.
Real artifact adapters use REST/SDK calls; MCP connectors are tools used by
contributors, not the engine's transport.

> **Diagrams:** [`docs/DIAGRAMS.md`](docs/DIAGRAMS.md) has the UML views —
> a [sequence diagram of one cycle](docs/DIAGRAMS.md#2-sequence--one-self-improvement-cycle)
> showing the artifact handoffs described below, and
> [state machines](docs/DIAGRAMS.md#4-state--issue-workflow-and-deploy-slots)
> for the issue workflow and the blue/green slots.

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
| Notifier                  | AWS SNS                    | Pages a human: breaker trip, spend-cap hit, broken sandbox, a loop that stopped (OMNI-62) |
| Sensor                    | `SimSensor`, a canned trace; the real one is `EIASensor`, EIA's bulk petroleum file (OMNI-33) | Readings of the modelled world, each with a unit and two times: the period it describes and when it became known (OMNI-31) |

Sensor consumers receive their adapter explicitly; sensor reads do not go
through `Workspace`. Capture and replay operations are in
[`docs/SENSOR_RUNBOOK.md`](docs/SENSOR_RUNBOOK.md). Cloud defaults are recording
adapters; `ServeCloud` is the opt-in traffic backend, with the non-stub and
Class-2 refusals documented in [`docs/SERVE_CANARY.md`](docs/SERVE_CANARY.md).

Same adapter mechanism later connects **domain** actors to the modeled world's APIs
(traffic sensors, building systems, etc.) — the "real world" connection is uniform.

## Self-Model & hardware-model (the digital twin)

A dedicated Ray **named, detached** actor (the **Registry / SelfModel**) maintains a
live, queryable model of the system itself and its substrate. This is the **first
piece of the world the server models**, using the same machinery it will later point
at traffic / a building / a factory. External modeling requires contracts,
fixtures and consumers; choosing a sensor backend alone does not build a twin.

Implemented (`sis/self_model.py`):
- Actor registrations with name, role, parent, state and a recorded restart
  counter. These are application records, not automatic Ray lifecycle probes.
- Deployment slots, live version, pending PR, contract registry and PR-contract links.
- Provenance events linking specs, plans, commits, PRs and outcomes.
- A substrate snapshot containing Ray cluster resource totals, Python version
  and platform. Budget/brake state is owned by CEO, not duplicated here.

Planned observability includes mailbox depth, actor placement, automatic restart
tracking, per-node CPU/RAM/GPU/disk/network headroom and AWS instance/region/
autoscaling inventory. Dynamic role creation and placement based on that
inventory are also planned; they are not capabilities of the current registry.

## Roles

Format per role: **Mandate / Owns / Spawns / Talks to / Real-world connections /
Ray form.**

### CEO — goals & cost
- **Mandate:** hold the project's goals and budget; decide what is worth doing.
- **Owns:** top-level objectives, the hard cost cap, the budget gate in the control
  loop, and kill-switch / circuit-breaker authority.
- **Planned spawning relationship:** PM and CTO.
- **Talks to:** PM (value/scope), CTO (cost/progress; receives reports). Brokers
  goal-vs-feasibility-vs-cost between them.
- **Real world:** reads roll-up status (Jira) and cost telemetry; writes rarely —
  sets the high-level charter (top Confluence page / top epics).
- **Ray:** named, detached actor.

### PM — user experience & specs
- **Mandate:** own the product/UX and the correctness of specifications.
- **Owns:** Confluence design docs; final acceptance of behavior vs spec; spec
  change requests.
- **Planned spawning relationship:** Designer actors (UI, higher-level outline).
- **Talks to:** CEO (value/scope); CTO (negotiates epics — reads & comments on
  epics/stories in Jira; signals when epics/stories must change -> CTO executes or
  delegates); reviews QA outcomes against intended UX.
- **Real world:** primary author/reader of Confluence (from templates); comments on
  Jira. **Intake point:** non-technical users drop new designs into a Confluence
  proposal space; the PM refines them into specs the system then builds.
- **Ray:** named, detached actor; Designer is currently created by bootstrap.

### CTO — technical execution
- **Mandate:** turn designs into a buildable, costed plan and drive delivery.
- **Owns:** translation of Confluence designs -> Jira epics; the technical
  architecture; the initial infra stories per epic; cost/progress coordination.
- **Planned spawning relationship:** DevOps, SWE (incl. SWE leads), QA.
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
- **Ray:** currently one named, detached SWE actor, created by bootstrap.
  Per-task workers and SWE leads remain planned.

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
- **Ray:** currently one named, detached QA actor, created by bootstrap.

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
- **Ray:** currently one named, detached DevOps actor, created by bootstrap.
  Autonomous infrastructure provisioning and dedicated monitor actors remain planned.

### Designer — UI / higher-level outline
- Currently a named, detached actor created by bootstrap; writes a spec outline.
  PM-owned Designer children and richer UI design work remain planned.

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
