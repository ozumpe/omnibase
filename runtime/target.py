"""target.py — the thing being optimized.

A deliberately slow pure function (naive sum-of-divisors) alongside a
baseline benchmark so the self-improvement loop has something measurable
to beat.

This file is RUNTIME-MUTABLE: the loop overwrites it on a successful
promotion. The naive O(n) version here is the starting baseline.
"""

import time


def sum_of_divisors(n: int) -> int:
    """Return the sum of all divisors of n, including 1 and n.

    Intentionally naive O(n) implementation — the loop has an obvious
    O(√n) optimisation to propose.
    """
    total = 0
    for i in range(1, n + 1):
        if n % i == 0:
            total += i
    return total


def benchmark(n: int = 10_000, repetitions: int = 5) -> float:
    """Return the mean wall-clock time (seconds) over *repetitions* calls."""
    times: list[float] = []
    for _ in range(repetitions):
        start = time.perf_counter()
        sum_of_divisors(n)
        times.append(time.perf_counter() - start)
    return sum(times) / len(times)
