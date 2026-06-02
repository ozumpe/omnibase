# Self-Improving Server (Python + Ray)

Detailed brief: @DESIGN.md — read it before starting.
Actor roles, external subsystems & the self-model: @ACTORS.md
Architecture diagram: `ray_self_improving_control_loop.svg` (this folder).

## What this is
A server that models a slice of the real world via an actor hierarchy and can extend
itself by generating, validating, and safely deploying its own code from high-level
specs. Bootstrap goal first: make the server itself self-improving on a trivial
internal target before it models anything external.

## Stack (decided — don't relitigate without reason)
- Python 3.14 free-threaded (`python3.14t`), managed with `uv`. Keep this env separate
  from the unrelated Spark/py4j work project pinned to Python 3.11.
- Ray for actors, Ray Serve for serving/canary rollouts.
- License: Apache 2.0. Never copy Akka source code.

## Architecture (detail in @DESIGN.md; full role spec in @ACTORS.md)
- CEO/CTO/PM = Ray named detached actors; SWE/QA/DevOps (+ PM's Designer) = Ray
  actors they spawn. Supervision via Ray `max_restarts` / `max_task_retries`.
- Actors coordinate through durable artifacts (Confluence/Jira/Git), not just
  in-memory messages — those artifacts are the work queue, message bus, audit trail,
  and long-term memory. Handoffs are artifact state changes.
- Required external subsystems (capability required, product replaceable, behind
  adapters): Document Store = Confluence, Work Tracker = Jira, Version Control +
  Review = GitHub, Cloud = AWS, Telemetry. Defaults are the MCP connectors.
- SelfModel actor (digital twin): a named detached actor tracking the live actor
  registry, blue/green deploy slots & live version, provenance, and the
  hardware/cluster substrate. It is the first "piece of the world" the system models
  — used for error collection, blue/green, spawning roles, and supervision.
- Control loop: monitor → budget/goal gate → LLM proposes Python on a branch →
  validation gauntlet → Ray Serve canary → promote or rollback → log outcome →
  circuit breaker.

## Hard rules
- Generated/untrusted code NEVER runs in the main process — only in an isolated Ray
  task / locked-down container with no network egress and no credential mounts.
- Python has no compiler, so the gauntlet gates hard: `ast.parse` → `mypy --strict`
  → `pytest` → benchmark vs baseline → sandboxed run → human PR. Generated code MUST
  be fully type-annotated.
- Agent works on feature branches only; never commit to `main` (enforce via GitHub
  branch protection + required checks, not goodwill).
- Secrets: `.env` (gitignored) locally; AWS Secrets Manager / SSM in cloud. Never
  commit tokens.
- Destructive Jira/Confluence/GitHub actions require human approval.
- Hard LLM budget cap; circuit breaker halts the loop after N failed/regressed cycles.

## Conventions
- `mypy --strict` and `pytest` in CI and as gauntlet gates.
- Every bug found becomes a permanent regression test.
- Small reviewable PRs. Provenance: prompt → commit → PR → ticket → outcome.

## First task
Build the bootstrap skeleton from @DESIGN.md §8: one supervisor actor, one worker
actor, the gauntlet function, a proposer (stub first, real Claude API second), an
episodic log, and `main.py` that runs one full cycle locally. Keep it small and
readable — it doubles as a way for me to relearn modern Python and Ray.
Acceptance: `uv run python main.py` runs one cycle and reports promote vs rollback
with before/after benchmarks.
