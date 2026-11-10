# Staged delivery — the agent iterates, humans gate promotion

**Status:** decided 2026-09-27 with Olaf; epic
[OMNI-128](https://olafzumpe.atlassian.net/browse/OMNI-128). Phase 0, the
sandbox worker (`sis/sandbox_worker.py`,
[OMNI-129](https://olafzumpe.atlassian.net/browse/OMNI-129)), and phase 1,
feature branches (`sis/feature.py`,
[OMNI-130](https://olafzumpe.atlassian.net/browse/OMNI-130)), are built. The
gate separation built on that worker also shipped (OMNI-45/146), and Class-2
construction is integrated (OMNI-147/149). Develop blue/green
([OMNI-133](https://olafzumpe.atlassian.net/browse/OMNI-133),
`sis/develop_platform.py`) is built behind `canary.develop_blue_green`, off by
default. Agent review
([OMNI-131](https://olafzumpe.atlassian.net/browse/OMNI-131), `sis/review.py`)
is built. AI approval and dynamic feature sizing remain planned.

## Why

Before staged delivery, every verified candidate opened one pull request, and the loop then
held until a human merged or closed it. OMNI-126 made that hold survive
restarts, but the loop still did nothing while it waited. So it improved only
as fast as a human reviewed.

The second AWS run (2026-09-27) needed about a minute per candidate. A real
review takes at least ten minutes, and only when someone has time. At one
improvement per review, a self-improving system is not viable.

## The model

The engine adopts the flow this repository already uses for its own work:

| Stage | Who acts | What reaches it |
|---|---|---|
| **Feature** branch | The agent, autonomously | Passing versions are committed; failed attempts remain feedback. Optimizations can take several steps; Class 2 finishes at its first passing step. The finished feature is reviewed by the agent before its PR opens (OMNI-131); automatic hot feature testing is planned. |
| **`develop`** | A human merges one fixed-head PR per finished feature. | The next feature starts from merged code. With `canary.develop_blue_green` on, each merge is served as green and judged against blue before it replaces it (below). Optional AI approval is planned. |
| **`main`** | A human, always, by release PR | Production |

Git keeps the history of every version the agent stood behind, and only those.
Nothing reaches production without a human merge; `develop` receives a
feature, not every step of one.

An optimization feature finishes at the configured accepted-step limit or when
a step finds no further gain. Class 2 finishes at its first passing step; a
built feature is revalidated before planning and returns neutral `no_gain` if
still correct. See `config.yml` for the step limit and
[CLASS2_CONTRACT.md](CLASS2_CONTRACT.md) for feature construction. Dynamic
sizing based on gains is OMNI-134, still planned.

## Alternatives considered and rejected

- **Auto-merge to live.** It removes the mandatory human gate. At decision time
  the benchmark-verdict forgery H2 was also open; OMNI-45 fixed that defect,
  but did not authorize auto-merge or make the Serve canary a sandbox.
- **Batching on one long-lived loop branch.** Squash merges break its ancestry,
  and the next batch then conflicts on the target file. The PR head also moves
  while a human reviews it, so the merge takes in steps nobody saw.
- **Keeping the lineage outside git.** It avoids those conflicts, but loses the
  history.

## Delivery status (2026-10-13)

| Work | State |
|---|---|
| OMNI-129: sandbox worker and hot-slot primitive | Built |
| OMNI-130: accepted feature steps, attempt feedback and fixed-head PRs | Built; incomplete work can resume after restart |
| AWS preparation: testrun develop branch, target re-seeding and base setting | Completed before run #3; subsequent runs recorded in [AWS_RUN.md](AWS_RUN.md) |
| OMNI-45/146: benchmark and correctness gates separated from candidate workers | Built; SLO remains in-process |
| OMNI-147/149: Class-2 construction and already-built convergence | Built |
| OMNI-48: move Serve green onto the worker | Built 2026-10-27; non-stub Serve refusal remains until a human lifts it |
| OMNI-133: develop blue/green | Built 2026-11-03, off by default (`canary.develop_blue_green`); Class-1 contracts, `--loop` only |
| OMNI-131: agent review | Built 2026-11-03: no feature PR without an approving review (below) |
| OMNI-132: optional AI approval | Planned; no implemented configuration switch |
| OMNI-134: dynamic feature size | Planned |

Each optimization iteration uses the feature's accepted head and earlier attempt
feedback. Rejected attempts are also retained before a branch exists for Class 2.
The finished PR head does not move while a human reviews it. Recovery asks real
version control about outstanding loop PRs as well as reading persisted state.

## Phase 0: the sandbox worker

A long-lived sandboxed process holds one candidate and answers calls as JSON
lines on stdin/stdout (`sis/sandbox_worker_main.py`, which imports nothing from
`sis`). The host side (`sis/sandbox_worker.py`) starts it, sends the inputs,
times each exchange and decodes each answer.

**It is the gauntlet's sandbox, not a second one.** It reuses:

- the container flags (`--network none`, `--cap-drop ALL`, `--read-only`,
  the host user's uid, never root, CPU and memory caps);
- the credential-free environment and the network guard;
- the docker kill on timeout;
- the self-check that tells a broken sandbox from a failing candidate
  (OMNI-37).

A worker reached over a pipe needs no network, so the kernel-enforced
isolation stays whole. That is what a Serve replica lacks (H3), so a replica
serves its candidate through a worker too (OMNI-48). The soft
subprocess sandbox is refused for a real proposer, exactly as for the gates
(M1).

**What the host decides, not the candidate:**

- **Timing** is the host's clock around each exchange. The candidate owns its
  own process, including its `time` module, but not the clock that measures
  it.
- **Answers** are decoded by the host's `json` module, so they are plain data by
  construction. A value with its own `__eq__` cannot cross the pipe, which
  closes H4 here without `canonical()`. Tuples arrive as lists, so a trusted
  value compared with an answer goes through `as_wire()` first.
- **Failure** has three forms: a hang past the per-call timeout (the worker is
  killed, and in docker the container too), an exit, or a malformed reply. Each
  is a failure of that version. A failure before the handshake blames the
  sandbox only if the trusted self-check fails too.

**Hot swap.** `HotSlot.deploy()` starts the new version *before* taking it into
service. A version that does not load never replaces a working one, and the old
worker finishes any call in flight before it is closed.

**Pipe cost, measured 2026-09-27 on a Mac.** The median round trip per call was
12 µs in subprocess mode and about 310 µs in docker, where Docker Desktop routes
stdio through its VM. Starting a worker took 0.05 s and 0.3 s respectively. The
`sort` target itself takes 3.6 µs, so calls to fast targets must be **batched**
(`call()` takes a list), and blue must be served the same way so both sides pay
the same overhead. Native Linux, the AWS box, should be far cheaper than Docker
Desktop; measure it before relying on either number.

**What it does not do yet:**

- `--canary serve` stays refused for a real proposer (OMNI-49, M19), although
  green now serves its candidate from here (OMNI-48). Lifting the refusal is a
  human decision.
- The SLO gate still judges a candidate inside its own process. Every other
  gate that runs the candidate calls it here (OMNI-45, OMNI-146: H2, M8).
- State is not handed over on a swap. Stateful targets (the omnitrack twin's
  actors) come later, and D2's decision to externalise state makes that
  tractable.

## The agent's own review (OMNI-131)

A finished feature is reviewed before its PR opens (`sis/review.py`,
POLICY-FORBIDDEN: a loop able to edit its reviewer could approve itself). The
review reads the whole diff, head against the base branch, and has two parts.
Both must approve.

- **The rules**, deterministic, always run, and the only part under the stub
  proposer:
  - the target must be a path the loop may write (the policy's SOFT tier);
  - the diff may add no I/O or egress, meaning an import of a module that
    reaches files, processes, the network or the interpreter's internals
    (`os`, `socket`, `subprocess`, `importlib`, …), or a call to `open`,
    `exec`, `eval`, `__import__` and the like, that the base did not already
    have.

  The sandbox stops such code at run time; the rules stop it reaching a
  human's queue, and no model's answer overrides them. A rule finding
  sends the feature back without calling the model.
- **The reviewer model**, with a real (non-stub) proposer: the configured LLM,
  asked whether the diff stays within the contract and the system's rules, is
  typed and readable, and does not game the checks. Under D3 it is a runtime
  LLM, so:
  - it has its own interface, stubbed in tests with canned replies;
  - its reply must be one JSON object, `{"approved": bool, "findings":
    [str]}`, and anything else, an approval that also lists findings, or an
    answer cut off at the token limit, is not an approval;
  - findings are capped in number and length, stripped of control
    characters, and quoted as data in the next prompt;
  - the diff is another model's text, so the prompt marks it as data, and
    the rules do not depend on the model at all;
  - its cost is added to the step's, so the CEO's brakes meter it like the
    proposer's, and a failure after the review still reports it.

**What happens next:**

- **Approved:** the PR opens, and its body records the review (who
  approved it, in which round, at what cost). Provenance gets a `review`
  event for every round.
- **Changes requested:** the head stays on its branch and the findings stay
  on the feature. The next cycle revises the head: the proposer is shown the
  findings and the head, and the revision is judged against the **base**, not
  the head, so it must keep the feature's gain over the base but need not
  beat the version it fixes. A passing revision is committed as one more step
  and reviewed again. The cycle's status is `review_requested`: neutral (spend
  recorded, no bug, no breaker count) and no evidence of convergence. A
  revision identical to the head is `no change`, and is not reviewed again.
- **Rounds used up:** a feature still not approved after
  `loop.review_max_rounds` reviews (default 3) is given up. Its branch is kept
  for a person to read, no PR opens, and the cycle is a counted `review:`
  failure with a bug filed.

**Limits:** a feature rebuilt from its branch after a restart (OMNI-135) has
lost its review state and is reviewed afresh. A given-up branch is remembered
by the SelfModel, so the next plan does not resume it; a restart forgets that
too, so a given-up branch the base has not moved past can be resumed once
more after a restart, for at most another `loop.review_max_rounds` reviews,
each counted toward the breaker. The reviewer model is not
deterministic (D3's known gap), so the same diff can be judged differently on
a second round; the rules are what is guaranteed. The reviewer uses the
proposer's provider and model.

## Develop blue/green (OMNI-133)

`develop` is a running platform, not just a branch. With
`canary.develop_blue_green` on, `main.py --loop` starts a
`DevelopPlatform` (`sis/develop_platform.py`, POLICY-FORBIDDEN) before its
first cycle:

- **Blue** is the head of `develop` as the run finds it (the target as merged
  on the base branch, else the local file), served from a sandbox worker.
- **Green.** When the loop observes a human merge (`DevOps.observe_merge`),
  the target as merged on the base branch is started as green in a second
  worker: what `develop` serves, never the PR's head, which can differ from
  the merge or be gone with its branch (OMNI-200). Only a backend with no base
  branch (the in-memory adapter) uses the PR's file. Nothing to read is a
  harness fault: no revert PR, a page.
- **The judgment.** The host draws 120 batches of 16 fresh inputs from the
  contract's oracle and sends each batch to both sides, alternating which goes
  first. `evaluate_canary` then decides in `SHADOW` mode:
  - every answer must equal the reference and blue's (plain values, JSON-decoded);
  - no call may fail (the error rate);
  - green's total time over the 120 exchanges may be at most 25% above blue's,
    decided on the paired `(green, blue)` time of each exchange with a
    bootstrap interval (OMNI-50): over the limit fails, an interval that
    straddles it is inconclusive.

  A merged version already beat its baseline offline, so the latency check
  only has to catch a regression. An inconclusive interval is recorded as not
  judged: green is retired without a revert PR, and a human is paged.
- **Pass:** blue and green switch, automatically, because a human already
  approved the merge.
- **Fail:** green is retired before it serves anything, and blue keeps serving.
  A revert PR (`revert/<version>`, blue's file) is opened against the base
  branch and a human is paged (CRITICAL). Nothing reverts automatically.
- **A fault on blue's side** (blue errs or dies under the window) is the
  harness's, not green's (OMNI-37). Nothing switches and nothing is reverted;
  a human is paged.

Every outcome is a provenance event (`develop.switch`, `develop.rollback`,
`develop.unjudged`) and an episodic row (`develop_switched`,
`develop_rolled_back`, `develop_unjudged`), with blue's and green's p95. The
SelfModel's `develop_blue` slot names what blue serves.

**Limits of this slice:**

- Class-1 contracts only. A feature has no oracle to draw traffic from, so
  for a Class-2 contract the platform stays off and says so.
- Only the loop's own contract is judged. A merge for another contract is
  logged as not judged.
- The traffic is the oracle's, not production's: nothing outside calls
  `develop` yet.
- Blue lives as long as the loop process. A restart serves the base branch's
  head again, which then includes every merge, judged or not.
- A revert PR does not hold the loop (only `feature/` PRs do, OMNI-136).
- `main`'s own blue/green comes later, with the same machinery.

## Risks

- **The reviewer is a model reviewing a model.** It can be talked round by
  the code it reads, which is why its prompt marks the diff as data and why
  the rules that matter most (no I/O, no egress, a SOFT target) are
  deterministic and checked first. A human still merges every feature.
- **Gates run AI-written code no human has reviewed.** That is the sandbox's
  job. OMNI-45/146 separate the trusted verdict from the candidate, but the
  SLO and Serve limitations remain; see [KNOWN_ISSUES.md](KNOWN_ISSUES.md).
  AI approval (OMNI-132) is planned, not something operators can switch on.
- **Feature size is a judgement call.** Too small, and review is the
  bottleneck again; too large, and review stops being real. The configured step
  limit bounds it; OMNI-134 proposes sizing it from evidence.
