# Staged delivery — the agent iterates, humans gate promotion

**Status:** decided 2026-09-27 with Olaf; epic
[OMNI-128](https://olafzumpe.atlassian.net/browse/OMNI-128). Phase 0, the
sandbox worker (`sis/sandbox_worker.py`,
[OMNI-129](https://olafzumpe.atlassian.net/browse/OMNI-129)), and phase 1,
feature branches (`sis/feature.py`,
[OMNI-130](https://olafzumpe.atlassian.net/browse/OMNI-130)), are built. The
gate separation built on that worker also shipped (OMNI-45/146), and Class-2
construction is integrated (OMNI-147/149). Serve isolation, agent review,
develop blue/green, AI approval and dynamic feature sizing remain planned.

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
| **Feature** branch | The agent, autonomously | Passing versions are committed; failed attempts remain feedback. Optimizations can take several steps; Class 2 finishes at its first passing step. The sandbox worker exists; agent self-review and automatic hot feature testing are planned. |
| **`develop`** | A human merges one fixed-head PR per finished feature. | The next feature starts from merged code. Automatic develop blue/green and optional AI approval are planned, not available switches today. |
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
| OMNI-131/133: agent review and develop blue/green | Planned |
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

## Risks

- **Gates run AI-written code no human has reviewed.** That is the sandbox's
  job. OMNI-45/146 separate the trusted verdict from the candidate, but the
  SLO and Serve limitations remain; see [KNOWN_ISSUES.md](KNOWN_ISSUES.md).
  AI approval (OMNI-132) is planned, not something operators can switch on.
- **Feature size is a judgement call.** Too small, and review is the
  bottleneck again; too large, and review stops being real. The configured step
  limit bounds it; OMNI-134 proposes sizing it from evidence.
