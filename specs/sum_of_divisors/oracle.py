"""Contract oracle for the ``sum_of_divisors`` target.

**Runs inside the gauntlet sandbox**, next to untrusted candidate code, so it is
standard-library only and imports nothing from ``sis/``.

POLICY-FORBIDDEN (``specs/`` is in ``sis.policy.GUARDRAIL_DIRS``): this is the
exam, and the implementer must never be able to edit it. Everything here was
previously interpolated into the gauntlet's benchmark script as literals, which
is what made the gauntlet answer only for this one target — see L5 in
docs/KNOWN_ISSUES.md.
"""

import random


def reference(n: int) -> int:
    """Trusted, independent, obviously-correct implementation.

    Deliberately the naive O(n) definition rather than anything clever: its job
    is to be *unarguably right*, not fast. It must stay independent of whatever
    the target currently does, or differential correctness degenerates into
    comparing a candidate with itself.
    """
    return sum(i for i in range(1, n + 1) if n % i == 0)


def random_input(rng: random.Random) -> tuple[int]:
    """One unpredictable valid input, as an args tuple for the entry function.

    Unpredictable is the point: a candidate that special-cases the known test
    and benchmark inputs but is wrong elsewhere ("benchmark gaming") is only
    caught by inputs it could not have seen.
    """
    return (rng.randint(2, 20_000),)


# The fixed workload both candidate and baseline are timed over, so the
# comparison is like-for-like. Spread across small/prime/perfect/large so no
# single algorithmic shape dominates the measurement.
BENCH_INPUTS: list[tuple[int]] = [
    (3,), (97,), (997,), (5000,), (8128,),
    (9973,), (10_000,), (12345,), (16384,), (19_999,),
]
