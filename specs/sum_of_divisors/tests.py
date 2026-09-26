"""Tests for target.sum_of_divisors.

These run against whichever target.py is on sys.path — both the baseline
naive version and any generated replacement must pass.
"""

import target


def test_sum_of_divisors_basic() -> None:
    assert target.sum_of_divisors(1) == 1
    assert target.sum_of_divisors(6) == 12   # 1+2+3+6
    assert target.sum_of_divisors(12) == 28  # 1+2+3+4+6+12
    assert target.sum_of_divisors(28) == 56  # perfect number: σ(28)=56


def test_sum_of_divisors_prime() -> None:
    # divisors of a prime p are 1 and p
    assert target.sum_of_divisors(7) == 8
    assert target.sum_of_divisors(13) == 14


def test_sum_of_divisors_perfect_numbers() -> None:
    # perfect number: σ(n) == 2n
    for p in (6, 28, 496, 8128):
        assert target.sum_of_divisors(p) == 2 * p


def test_benchmark_returns_float() -> None:
    t = target.benchmark(n=100, repetitions=2)
    assert isinstance(t, float)
    assert t > 0
