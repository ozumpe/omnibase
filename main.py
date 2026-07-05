"""main.py — bootstrap the actor org and run one intake→deploy cycle.

Usage:
    poetry run python main.py

The org cycle exercises the whole hierarchy on a simulated intake: a
non-technical user drops a proposal into Confluence → PM writes a spec →
CTO plans a Jira epic/story → SWE implements a validated change on a feature
branch + PR (reusing the proposer + gauntlet) → QA verifies → DevOps
canary-deploys to the green slot. Promotion to live is the human PR merge,
intentionally left pending.
"""

from __future__ import annotations

import ray

from sis import org


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


def main() -> None:
    run_org_cycle()


if __name__ == "__main__":
    main()
