# Delivery contracts — the engine delivers a Jira ticket

**Status:** design only, nothing built. Epic
[OMNI-193](https://olafzumpe.atlassian.net/browse/OMNI-193) (Phase G in
[OMNITRACK_VISION.md](OMNITRACK_VISION.md) §4). No stories are filed for it yet.
Phase B ([OMNI-194](https://olafzumpe.atlassian.net/browse/OMNI-194)) stays the
current goal; this document is what comes after.

Read [CLASS2_CONTRACT.md](CLASS2_CONTRACT.md) and
[STAGED_DELIVERY.md](STAGED_DELIVERY.md) first: this builds on both and changes
neither.

---

## 1. The idea in one paragraph

Today a contract is one file the loop may write, judged by an exam a human
approved: a speed-up (Class 1) or a feature at one entry point (Class 2). A
**delivery contract** (call it Class 3) takes a whole Jira ticket instead: an
extension to omnibase, or a new digital twin. The ticket's **acceptance
criteria** plus a project-wide **definition of done** become the exam. The
**org structure** (CEO, PM, CTO, SWE, QA, DevOps) decides who proposes and who
approves each step. The engine first proposes a **plan** that a human can edit,
reject or approve; then it delivers the plan as a series of small PRs, each one
a normal Class 1 or Class 2 contract, that a human can stop or merge. Nothing
here lets the loop merge, write its own exam, or skip the gauntlet.

## 2. What a delivery looks like

```
Jira ticket (AC + DoD ref)            human edits the ticket at any time
      │                                         │ (digest changes → loop stops, re-plans)
      ▼                                         ▼
 1. INTAKE     PM reads ticket through a sanitiser  → ticket digest
 2. EXAM       ContractAuthor drafts one check per AC → staging → human promotes into specs/
 3. PLAN       CTO + SWE lead propose steps         → plan artifact → human approves digest
 4. STEPS      each step = one Class 1/2 contract   → gauntlet → agent review → draft PR
                                                     → human merges or closes (closing = feedback)
 5. DONE       done gate on the merged develop head → every AC check + DoD passes
                                                     → PM marks "ready for acceptance" → human closes ticket
```

Every arrow that a human sits on is a place to **intervene or redefine**:
change the ticket, change the drafted exam before promoting it, edit the plan,
close a step's PR, pause the loop (`sis.admin pause`). Each of those is a state
change on an artifact, which is how the actors already talk (ACTORS.md,
"artifacts are the bus").

## 3. The contract

```python
@dataclass(frozen=True)
class DeliveryContract:
    name: str
    ticket_ref: str              # "OMNI-301": the provenance root, like spec_ref
    ticket_digest: str           # sha256 of the sanitised AC text the exam was built from
    definition_of_done: str      # specs/_dod/<version>.json — project-wide, pinned
    acceptance: tuple[Criterion, ...]
    plan: str | None             # specs/<name>/plan.json once a human approves it
    approvals: str               # specs/_org/approvals.json — who signs what
    write_scope: tuple[str, ...] # path globs the steps may touch (each still policy-checked)
    budget_usd: float            # whole-ticket spend cap, inside the CEO's weekly cap
    max_steps: int               # stop and page if the plan needs more

@dataclass(frozen=True)
class Criterion:
    id: str                      # "AC-1"
    text: str                    # the ticket's words, sanitised
    check: str | None            # specs/<name>/ac_test.py::test_ac_1, human-approved
    judged_by: str | None        # a role from approvals.json when no check can be written
```

`gate_profile()` for the contract as a whole is short: **plan-conformance → done
gate**. The real gauntlet work happens per step, in the step's own Class 1 or 2
profile, which is unchanged.

### 3.1 Acceptance criteria become the exam

- Each criterion gets an executable check if one can be written (a test, an
  invariant, a backtest). The ContractAuthor drafts it, the
  **discrimination check** (`contract_author.check_discrimination`) shows it
  fails against a null implementation, and a human promotes it with a reviewed
  digest. This is exactly today's write path into `specs/`.
- A criterion that cannot be checked by code ("the docs explain it plainly")
  says so with `judged_by` instead of pretending. It is listed in the done
  report and only a human, or a role the approval table allows, ticks it.
- **Hidden cases.** The author may mark some cases of a check as held out: the
  proposer sees the criterion and the visible cases, never the held-out ones.
  These spend the D5 holdout budget (`sis/holdout.py`) like a backtest does, so
  a step that keeps failing them stops before it can learn them by probing.

### 3.2 The definition of done

One pinned file in `specs/_dod/`, versioned, shared by every delivery: the full
`ruff`, `mypy --strict` and `pytest` suite green on the integrated head; no
new KNOWN_ISSUES entry without a ticket; CHANGELOG line; docs updated where a
rule or a command changed; the agent's own review approved (`sis/review.py`).
It is the repo's existing PR rules, written down once as data so a gate can
read them. A ticket can add to it, never remove from it.

### 3.3 The plan

The CTO (with the SWE lead) proposes a plan: an ordered list of steps, each
with a kind (`feature`, `optimise`, `docs`, `config-proposal`), the target
path(s), the criteria it serves, and a cost estimate. It is written as a
Confluence page or Jira sub-tasks so you can read and edit it where you
already work, and pinned as `specs/<name>/plan.json` only when a human
approves its digest.

- **Editing** the plan is a new digest. The loop never executes a plan whose
  digest a human has not approved, and an unapproved edit pauses it.
- **Plan-conformance gate:** every step's diff stays inside that step's
  declared paths and inside `write_scope`, and every criterion is served by at
  least one step. A plan that leaves a criterion unserved cannot be approved.
- **Re-planning** happens when a step is closed unmerged twice, when the
  ticket digest changes, or when the futility stop (`sis/futility.py`) fires on
  a step. A re-plan is a new proposal for you, never a silent change.

### 3.4 The org structure decides approvals

`specs/_org/approvals.json` (FORBIDDEN, edited only by a human) says, for each
kind of decision, which role may propose it and who must approve it:

| Decision | Proposed by | Approved by (v1) | Can it ever be an agent? |
|---|---|---|---|
| exam (AC checks, held-out cases) | ContractAuthor / QA | human | no: an implementer that approves its own exam has no exam |
| plan and re-plan | CTO + SWE lead | human | later, for plans under a cost and path limit you set |
| a step's PR opens | SWE | agent review (`review.py`) | already is |
| a step's PR merges to `develop` | SWE | human | not before Phase G's sealed judge |
| a `judged_by` criterion | PM | human | per criterion, if you name a role |
| the ticket is done | PM | human | no |
| release to `main` | DevOps | human | no |

This is the "who can approve which step" part of the idea. It starts with a
human in every approving seat except the one the agent already holds, and
each later loosening is one reviewed edit to this file, not new code.

## 4. Extensions versus new twins

Both are tickets; they differ in what the steps may touch.

- **A new digital twin** (for example the Iowa spirits supply chain,
  [OMNI-39](https://olafzumpe.atlassian.net/browse/OMNI-39)) is mostly new SOFT
  code under its own `runtime/<twin>/` slot plus a new forecasting contract.
  The parts the policy guards stay human-authored: the sensor adapter and its
  sanitiser, the licence rules, the fixtures and the held-out split. The plan
  may *propose* them as `config-proposal` steps (a staged draft a human
  promotes), never write them. This is reachable well before Phase G.
- **An extension to omnibase itself** touches STRICT engine code. Today that
  needs `SIS_ALLOW_STRICT_CHANGES=1` plus a human for every change, and it
  must wait for Phase G's sealed judge: the gauntlet, `specs/`, brakes and
  policy moved into a signed package the loop can only read. Until then a
  delivery contract may only target SOFT paths, and the approval table above
  cannot be loosened for STRICT steps.

## 5. What is reused and what is new

| Piece | Reused as is | New |
|---|---|---|
| Per-step judging | Class 1/2 gate profiles, sandbox worker, verdicts (`sis/verdict.py`) | — |
| Writing the exam | `contract_author` stage/promote with digest, discrimination check | one check per AC, `judged_by` criteria |
| Hidden cases | holdout budget (`sis/holdout.py`) | held-out cases inside an acceptance test |
| Agent review | `sis/review.py` | reads the plan step as its spec |
| Delivery flow | feature → develop → main, observed human merge, develop blue/green | one ticket spans several feature PRs |
| Brakes | spend cap, breaker, futility stop, one loop per box | per-ticket budget and step cap |
| Org | CEO/PM/CTO/SWE/QA/DevOps actors, Jira/Confluence ports | approval table, plan artifact |
| Multi-file change | — | a step may touch several files; today a target is one file |
| Done gate | `pytest`, `mypy`, `ruff` | runs on the integrated develop head, maps results to AC ids |
| Ticket intake | Work Tracker port | AC field read through a sanitiser, ticket digest |

New modules (`sis/delivery.py` for the pure plan/done logic, the sanitiser,
the approval table reader) are FORBIDDEN from the start, like every other part
of the judge.

## 6. Ways this can go wrong, and the guard for each

- **The ticket is an attack surface.** Anyone who can edit a ticket can steer
  the loop. Ticket text is untrusted input, like sensor data: it enters through
  one sanitiser, is treated as data in prompts, and only a human-promoted exam
  can judge anything. Editing the ticket after promotion stops the loop rather
  than changing the exam.
- **Vague criteria make a weak exam.** "Works well" cannot be checked. The
  draft step reports which criteria got a check and which did not, before you
  promote. Anti-gaming is only as strong as the input distribution (CLAUDE.md
  trap), so the held-out cases matter.
- **Plans that grow.** `max_steps` and `budget_usd` stop a plan that keeps
  expanding, and the stop pages you, like the holdout and futility stops.
- **Many green steps, one red whole.** Each step passes alone but together they
  break something. The done gate runs on the integrated head, and develop
  blue/green already judges each merge against the running system.
- **Review fatigue.** Five PRs per ticket is five reviews. Steps stay small,
  and the PR body links every step to its criteria so a review is "does this
  serve AC-2", not "what is this".

## 7. Open questions for you

1. **Where the plan lives:** a Confluence page, Jira sub-tasks, or a file in the
   PR. Recommendation: Jira sub-tasks, because the ticket is already the source
   of truth and you edit there.
2. **How the AC are written in Jira:** a fixed "Acceptance criteria" section
   with one bullet per criterion and worked examples (`input → output`), which
   `contract_author.parse_worked_examples` already reads. Recommendation: yes,
   and a ticket without that section is refused at intake.
3. **Where the definition of done is kept:** `specs/_dod/` as data, or this
   repo's CONTRIBUTING.md as prose that a human transcribes. Recommendation:
   data, generated from the same rules CI checks.
4. **Which seat loosens first:** plan approval for small, SOFT-only plans is
   the natural first one, once a few deliveries have gone through.
5. **Cost:** each ticket gets its own budget inside the weekly cap, or shares
   it. Recommendation: its own, with a low default, since this is a side project.

## 8. First small slice (no code generation)

**Slice 1: dry run from a ticket to a promoted exam and an approved plan.**
Give it one real ticket with an "Acceptance criteria" section. The engine
reads it through the sanitiser, drafts one check per criterion into staging
(with the discrimination check), and proposes a plan as Jira sub-tasks. You
promote the exam and approve or edit the plan. It writes no candidate code
and spends only the drafting calls, so it shows whether Jira tickets can
become good exams before anything is built on that.

Slice 2 executes one step of an approved plan against a SOFT target through
today's Class 2 path, with `ticket_ref` and the AC ids carried into the PR.
Slice 3 adds the done gate. Multi-file steps, hidden cases and the approval
table come after those, and extensions to engine code wait for Phase G.

## 9. Relationship to existing work

- [CLASS2_CONTRACT.md](CLASS2_CONTRACT.md): each step of a delivery is a
  Class 2 (or Class 1) contract; nothing in its gate stack changes.
- [STAGED_DELIVERY.md](STAGED_DELIVERY.md): feature → develop → main, the agent's
  own review and develop blue/green are reused as they are.
- [OMNITRACK_VISION.md](OMNITRACK_VISION.md): D8 (a human decides what the
  modelled actors are) and D12 (the loop may propose its exam, a human
  approves it) are the decisions this design rests on. Phase G's sealed judge
  gates the extension half.
- [ACTORS.md](../ACTORS.md): the roles and the intake flow are the ones already
  described there; the approval table makes them enforceable.
