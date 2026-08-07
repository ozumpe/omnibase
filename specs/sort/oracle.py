"""Contract oracle for the ``sort_numbers`` target.

**Runs inside the gauntlet sandbox**, next to untrusted candidate code, so it is
standard-library only and imports nothing from ``sis/``.

POLICY-FORBIDDEN (``specs/`` is in ``sis.policy.GUARDRAIL_DIRS``): this is the
exam, and the implementer must never be able to edit it.
"""

import random


def reference(values: list[int]) -> list[int]:
    """Trusted, independent, obviously-correct implementation.

    ``sorted()`` rather than a hand-rolled sort on purpose. The oracle's job is
    to be *unarguably right*, and a battle-tested stdlib primitive is a stronger
    guarantee than an insertion sort I could get subtly wrong — an oracle with a
    bug silently inverts every verdict downstream.

    The independence that matters is independence from *the target's
    implementation* (a bubble sort), not from every possible candidate.
    ``sorted()`` satisfies that: it shares no code with the target and cannot be
    influenced by it. A candidate that also reaches for ``sorted()`` is simply
    correct — there is nothing to game, since matching the oracle everywhere is
    exactly what correctness means here. (The shipped stub candidate is a
    hand-written merge sort, so the default path exercises a genuine
    implementation-vs-implementation comparison rather than a tautology.)
    """
    return sorted(values)


def random_input(rng: random.Random) -> tuple[list[int]]:
    """One unpredictable valid input, as an args tuple for the entry function.

    Unpredictable is the point: a candidate that special-cases the known
    benchmark and test inputs but is wrong elsewhere ("benchmark gaming") is
    only caught by inputs it could not have seen. The value range is narrow
    relative to the length so duplicates occur naturally — sort bugs love ties.

    **The length distribution is deliberately wide**, and that is load-bearing
    rather than decorative. An earlier version drew only 60–120 elements, and a
    probe candidate reading ``return v if len(v) > 500 else sorted(v)`` — i.e.
    silently unsorted on large inputs — passed every gate, because the broken
    branch was never reached. The differential gate can only catch what the
    distribution covers, and this target was chosen *because* its input size
    scales freely, so size-conditional behaviour is precisely the cheat it must
    detect. Tiny lengths are included for the same reason from the other end:
    empty and single-element lists are where off-by-one bugs live.
    """
    bucket = rng.random()
    if bucket < 0.15:
        length = rng.randint(0, 5)        # empty / tiny: off-by-one territory
    elif bucket < 0.75:
        length = rng.randint(50, 200)     # the common case
    else:
        length = rng.randint(500, 1200)   # large: catches size-conditional cheats
    return ([rng.randint(-500, 500) for _ in range(length)],)


def _shuffled(seed: int, length: int) -> list[int]:
    rng = random.Random(seed)
    return [rng.randint(-10_000, 10_000) for _ in range(length)]


# The fixed workload both candidate and baseline are timed over, so the
# comparison is like-for-like. Deliberately mixed: random, already-sorted,
# reverse-sorted and heavy-duplicate shapes, because sort implementations have
# wildly different best/worst cases and timing only one shape would flatter
# whichever algorithm happens to suit it.
BENCH_INPUTS: list[tuple[list[int]]] = [
    (_shuffled(1, 200),),
    (_shuffled(2, 200),),
    (_shuffled(3, 250),),
    (sorted(_shuffled(4, 200)),),                    # already sorted
    (sorted(_shuffled(5, 200), reverse=True),),      # reverse sorted
    ([7] * 200,),                                    # all equal
    ([i % 5 for i in range(200)],),                  # few distinct values
    (_shuffled(6, 150),),
]
