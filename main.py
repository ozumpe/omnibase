"""main.py — bootstrap the actor org and run intake→deploy cycle(s).

Usage:
    poetry run python main.py                    # one cycle, then exit (the demo)
    poetry run python main.py --contract sort    # optimise a different target
    poetry run python main.py --canary serve     # judge the canary against real traffic
    poetry run python main.py --loop             # run as a server until Ctrl-C

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


def run_org_cycle(contract_name: str | None = None, canary_backend: str | None = None) -> None:
    handles = org.bootstrap()
    print("[main] org bootstrapped:", ", ".join(handles))

    result = org.run_cycle(
        handles,
        proposal_title="Speed up divisor-sum endpoint",
        proposal_body=(
            "The divisor-sum computation is too slow under load. "
            "Please make it faster without changing results."
        ),
        contract_name=contract_name,
        canary_backend=canary_backend,
    )

    print("\n[main] cycle status:", result["status"])
    if "baseline_latency" in result:
        print(
            f"  baseline={result['baseline_latency']:.6f}s"
            f"  candidate={result['candidate_latency']:.6f}s"
            f"  PR={result['pr_id']}  (awaiting human merge)"
        )
    if result.get("canary", {}).get("verdict"):
        verdict = result["canary"]["verdict"]
        print(f"  live canary: {'PASS' if verdict['passed'] else 'FAIL'} — {verdict['reason']}")

    print("\n[main] provenance graph:")
    for event in result.get("provenance", []):
        print(f"  {event['kind']:<8} {event['ref']}")

    print("\n[main] actor registry:")
    for info in ray.get(handles["SelfModel"].registry.remote()):
        print(f"  {info['role']:<9} {info['name']:<10} state={info['state']}"
              f" parent={info['parent']}")


def run_server_loop(canary_backend: str | None = None) -> None:
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
        canary_backend=canary_backend,
    )
    print(f"[main] loop stopped after {len(results)} cycle(s)")


def _flag_value(argv: list[str], flag: str) -> str | None:
    """``--flag value`` from argv, or None if absent. Fails loudly if the flag
    is present with nothing after it, rather than silently taking the next
    flag as its value."""
    if flag not in argv:
        return None
    index = argv.index(flag)
    if index + 1 >= len(argv):
        raise SystemExit(f"{flag} needs a value")
    return argv[index + 1]


def main() -> None:
    import sys

    # --contract/--canary are passed down as arguments rather than read from
    # the environment inside the actors: they are separate processes and only
    # see the env as it was when they were created (SIS_CONTRACT/SIS_CANARY
    # still work, but only if exported before launch).
    contract_name = _flag_value(sys.argv, "--contract")
    canary_backend = _flag_value(sys.argv, "--canary")

    if "--loop" in sys.argv:
        run_server_loop(canary_backend)
    else:
        run_org_cycle(contract_name, canary_backend)


if __name__ == "__main__":
    main()
