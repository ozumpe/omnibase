"""main.py — bootstrap the actor org and run intake→deploy cycle(s).

Usage:
    poetry run python main.py            # one cycle, then exit (the demo)
    poetry run python main.py --loop     # run as a server until Ctrl-C / SIGTERM

The org cycle exercises the whole hierarchy on a simulated intake: a
non-technical user drops a proposal into Confluence → PM writes a spec →
CTO plans a Jira epic/story → SWE implements a validated change on a feature
branch + PR (reusing the proposer + gauntlet) → QA verifies → DevOps
canary-deploys to the green slot. Promotion to live is the human PR merge,
intentionally left pending.

``--loop`` runs the same cycle continuously via :mod:`sis.loop`, stopping
gracefully on SIGINT/SIGTERM (or after ``SIS_LOOP_MAX_CYCLES`` cycles).
"""

from __future__ import annotations

import os

import ray

from sis import loop, org


def run_org_cycle() -> None:
    handles = org.bootstrap()
    print("[main] org bootstrapped:", ", ".join(handles))

    result = org.run_cycle(
        handles,
        proposal_title="Speed up divisor-sum endpoint",
        proposal_body=(
            "The divisor-sum computation is too slow under load. "
            "Please make it faster without changing results."
        ),
    )

    print("\n[main] cycle status:", result["status"])
    if "baseline_latency" in result:
        print(
            f"  baseline={result['baseline_latency']:.6f}s"
            f"  candidate={result['candidate_latency']:.6f}s"
            f"  PR={result['pr_id']}  (awaiting human merge)"
        )

    print("\n[main] provenance graph:")
    for event in result.get("provenance", []):
        print(f"  {event['kind']:<8} {event['ref']}")

    print("\n[main] actor registry:")
    for info in ray.get(handles["SelfModel"].registry.remote()):
        print(f"  {info['role']:<9} {info['name']:<10} state={info['state']}"
              f" parent={info['parent']}")


def run_server_loop() -> None:
    handles = org.bootstrap()
    print("[main] server loop starting (Ctrl-C to stop gracefully)")
    max_cycles_env = os.getenv("SIS_LOOP_MAX_CYCLES")
    # repeat() never runs dry, so SIS_LOOP_MAX_CYCLES is a clean bound and an
    # unbounded run keeps improving until Ctrl-C (rather than idling after one).
    results = loop.serve(
        handles,
        loop.repeat(
            "Speed up divisor-sum endpoint",
            "The divisor-sum computation is too slow under load. "
            "Please make it faster without changing results.",
        ),
        interval_s=float(os.getenv("SIS_LOOP_INTERVAL", "30")),
        max_cycles=int(max_cycles_env) if max_cycles_env else None,
    )
    print(f"[main] loop stopped after {len(results)} cycle(s)")


def main() -> None:
    import sys

    if "--loop" in sys.argv:
        run_server_loop()
    else:
        run_org_cycle()


if __name__ == "__main__":
    main()
