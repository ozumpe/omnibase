"""optimised_target.py — O(√n) replacement candidate for the live target.

This is what the proposer stub returns as its "generated" candidate
(Milestone 1). The gauntlet validates it; the supervisor deploys it by
overwriting runtime/target.py.
"""

import math
import time


def sum_of_divisors(n: int) -> int:
    """Return the sum of all divisors of n, including 1 and n.

    O(√n) implementation: iterate only up to √n and pair each small
    divisor with its complement.
    """
    if n <= 0:
        return 0
    total = 0
    sqrt_n = int(math.isqrt(n))
    for i in range(1, sqrt_n + 1):
        if n % i == 0:
            total += i
            if i != n // i:
                total += n // i
    return total


def benchmark(n: int = 10_000, repetitions: int = 5) -> float:
    """Return the mean wall-clock time (seconds) over *repetitions* calls."""
    times: list[float] = []
    for _ in range(repetitions):
        start = time.perf_counter()
        sum_of_divisors(n)
        times.append(time.perf_counter() - start)
    return sum(times) / len(times)
