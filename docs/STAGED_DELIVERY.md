# Staged delivery — the agent iterates, humans gate promotion

**Status:** decided 2026-09-27 with Olaf; epic
[OMNI-128](https://olafzumpe.atlassian.net/browse/OMNI-128). Phase 0, the
sandbox worker (`sis/sandbox_worker.py`,
[OMNI-129](https://olafzumpe.atlassian.net/browse/OMNI-129)), and phase 1,
feature branches (`sis/feature.py`,
[OMNI-130](https://olafzumpe.atlassian.net/browse/OMNI-130)), are built. The
other phases are planned.

## Why

Until now, every verified candidate opened one pull request, and the loop then
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
| **Feature** branch | The agent, autonomously | Only versions that passed the local tests, the gauntlet's hard thresholds and the agent's own review. Several iterations per feature; failed versions are never pushed (already true today). Tested hot in the sandbox (phase 0). |
| **`develop`** | A human merges one PR per finished feature. AI approval is optional, behind a `forbidden_` key, off by default. | Integration and autonomous improvement; deployed blue/green. |
| **`main`** | A human, always, by release PR | Production |

Git keeps the history of every version the agent stood behind, and only those.
Nothing reaches production without a human merge; `develop` receives a
feature, not every step of one.

A feature is finished after `N` accepted steps (a config value, default 3), or
as soon as a step finds no further gain. A dynamic budget that follows the
steps' gains is OMNI-134.

## Alternatives considered and rejected

- **Auto-merge to live.** It removes the human gate, and it is unsafe while a
  candidate can forge its own benchmark verdict (KNOWN_ISSUES H2, OMNI-45).
- **Batching on one long-lived loop branch.** Squash merges break its ancestry,
  and the next batch then conflicts on the target file. The PR head also moves
  while a human reviews it, so the merge takes in steps nobody saw.
- **Keeping the lineage outside git.** It avoids those conflicts, but loses the
  history.

## The stories, in order

1. **OMNI-129, phase 0: the sandbox worker** (below). It comes first because
   OMNI-48, OMNI-45 and OMNI-133 all build on it.
2. **OMNI-130, phase 1: feature branches.** Each iteration starts from the
   feature branch's head, and its prompt carries the earlier attempts and why
   they failed (the useful half of OMNI-127). A finished feature becomes one PR
   to `develop`, whose head never moves after it is opened. Holds are per
   contract.
3. **Before AWS run #3:** a `develop` branch on `ozumpe/testrun`, a re-seeded
   naive `sum_of_divisors` (it improved in four steps in July), and
   `github.default_base: develop`. Then v0.3.0 and the run, measuring
   iterations, cost and review time per feature.
4. **Then:**
   - OMNI-48 (the canary onto the worker);
   - OMNI-133 (`develop` blue/green);
   - OMNI-131 (the agent's own review);
   - OMNI-45 (the gates onto the worker);
   - OMNI-132 (optional AI approval, blocked by OMNI-45);
   - OMNI-134 (dynamic feature size).

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
isolation stays whole. That is what a Serve replica lacks (H3). The soft
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

- The Serve canary still runs green as a Ray worker until OMNI-48 moves it
  here, so `--canary serve` stays refused for a real proposer (OMNI-49).
- The SLO gate still judges a candidate inside its own process. Every other
  gate that runs the candidate calls it here (OMNI-45, OMNI-146: H2, M8).
- State is not handed over on a swap. Stateful targets (the omnitrack twin's
  actors) come later, and D2's decision to externalise state makes that
  tractable.

## Risks

- **Local tests and hot deploys run AI-written code no human has reviewed.**
  That is the sandbox's job, and its known gaps are OMNI-45's. They matter most
  once AI approval (OMNI-132) is switched on, which is why that story is
  blocked by OMNI-45.
- **Feature size is a judgement call.** Too small, and review is the
  bottleneck again; too large, and review stops being real. The default `N` = 3
  and a hard cap bound it; OMNI-134 makes it follow the evidence.
