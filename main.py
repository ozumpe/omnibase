"""main.py — bootstrap the actor org and run intake→deploy cycle(s).

Usage:
    poetry run python main.py                    # one cycle, then exit (the demo)
    poetry run python main.py --contract sort    # optimise a different target
    poetry run python main.py --canary serve     # judge the canary against real traffic
    poetry run python main.py --loop             # run as a server until Ctrl-C
    poetry run python main.py --show-config      # effective config + where each value came from

Every key in ``config.yml`` also has a ``--section-name`` flag (see
``sis/config.py``), e.g. ``--sandbox-mode docker`` or ``--brakes-budget-usd 0.05``.

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

import ray

from sis import config, loop, org


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
    pacing = config.config().loop
    # repeat() never runs dry, so loop.max_cycles is a clean bound and an
    # unbounded run keeps improving until Ctrl-C (rather than idling after one).
    results = loop.serve(
        handles,
        loop.repeat(
            "Speed up divisor-sum endpoint",
            "The divisor-sum computation is too slow under load. "
            "Please make it faster without changing results.",
        ),
        interval_s=pacing.interval_seconds,
        max_cycles=pacing.max_cycles,
        canary_backend=canary_backend,
    )
    print(f"[main] loop stopped after {len(results)} cycle(s)")


def main() -> None:
    import sys

    # Every config key has a --section-name flag; `--contract` and `--canary`
    # are the two older spellings. Applied BEFORE bootstrap() on purpose: the
    # role actors are separate OS processes that snapshot the environment when
    # they are created, so an override installed after bootstrap would configure
    # this process and silently leave the actors on the old value.
    overrides = config.parse_cli(sys.argv[1:])
    config.apply_cli_overrides(overrides)

    if "--show-config" in sys.argv:
        for item in config.effective():
            print(f"{item.key.path:<38} {str(item.value):<24} "
                  f"[{item.key.tier.value}, from {item.source.value}]")
        return

    # Still passed down as explicit arguments rather than re-read inside the
    # actors: a per-cycle choice belongs to the cycle, not to a file or an
    # environment that outlives it.
    settings = config.config()
    contract_name = settings.contracts.default
    canary_backend = settings.canary.backend

    if "--loop" in sys.argv:
        run_server_loop(canary_backend)
    else:
        run_org_cycle(contract_name, canary_backend)


if __name__ == "__main__":
    main()
