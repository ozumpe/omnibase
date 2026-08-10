"""Adversarial regression corpus for the gauntlet.

Each case is a plausible bad output a real LLM proposer could return — they
look reasonable and most pass earlier gates — and asserts the gauntlet
rejects it. This is the safety net that lets us trust an untrusted proposer:
the deterministic gates, not the model, decide what ships.

Every bug found in the wild should become a new case here.
"""

import pytest

from sis import gauntlet

_BASELINE = 0.05


def _validate(code: str) -> gauntlet.Result:
    return gauntlet.validate(code, _BASELINE)


def test_subtly_wrong_fast_impl_is_rejected() -> None:
    # O(√n) but double-counts the square root on perfect squares — fast, typed,
    # passes the fixed pytest cases that happen to avoid the bug, but wrong on
    # squares. The differential check on random inputs must catch it.
    code = '''
import math
import time


def sum_of_divisors(n: int) -> int:
    total = 0
    root = int(math.isqrt(n))
    for i in range(1, root + 1):
        if n % i == 0:
            total += i + n // i  # BUG: adds the paired divisor even when i == n//i
    return total


def benchmark(n: int = 10_000, repetitions: int = 5) -> float:
    return 1e-9
'''
    # Rejected for being wrong — caught by the contract's acceptance cases
    # and/or the random differential check (whichever trips first). The point: a
    # plausible, typed, fast-but-incorrect diff does not ship. Which of the two
    # catches it is deliberately not asserted; pinning that would make the test
    # about gate ordering rather than about the candidate being rejected.
    result = _validate(code)
    assert not result.passed
    assert (
        result.reason == "acceptance tests failed"
        or "correctness mismatch" in result.reason
    )


def test_correct_but_not_faster_is_rejected() -> None:
    # Perfectly correct, fully typed — but it's the naive O(n) version, so it
    # can't beat the baseline by the required margin.
    code = '''
import time


def sum_of_divisors(n: int) -> int:
    return sum(i for i in range(1, n + 1) if n % i == 0)


def benchmark(n: int = 10_000, repetitions: int = 5) -> float:
    return 1.0
'''
    result = _validate(code)
    assert not result.passed
    assert "no improvement" in result.reason


def test_untyped_fast_impl_is_rejected() -> None:
    # Correct and fast, but missing annotations → mypy --strict rejects it.
    code = '''
import math


def sum_of_divisors(n):
    total = 0
    for i in range(1, int(math.isqrt(n)) + 1):
        if n % i == 0:
            total += i
            if i != n // i:
                total += n // i
    return total


def benchmark(n=10_000, repetitions=5):
    return 1e-9
'''
    result = _validate(code)
    assert not result.passed
    assert "mypy" in result.reason


def test_raises_on_some_inputs_is_rejected() -> None:
    # Looks fast and is typed, but blows up on inputs divisible by 7. The
    # static pytest cases (1,6,12,28,7,13,...) include 7 → caught at pytest,
    # and the random differential check would catch it regardless.
    code = '''
import math


def sum_of_divisors(n: int) -> int:
    if n % 7 == 0:
        raise ValueError("nope")
    total = 0
    for i in range(1, int(math.isqrt(n)) + 1):
        if n % i == 0:
            total += i
            if i != n // i:
                total += n // i
    return total


def benchmark(n: int = 10_000, repetitions: int = 5) -> float:
    return 1e-9
'''
    result = _validate(code)
    assert not result.passed


def test_infinite_loop_is_killed_by_timeout(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A non-terminating candidate must be killed, not hang the loop. Use a tiny
    # timeout so the test stays fast; the candidate sleeps far longer.
    monkeypatch.setenv("SIS_GAUNTLET_TIMEOUT", "2")
    code = '''
import time


def sum_of_divisors(n: int) -> int:
    time.sleep(30)  # stand-in for an infinite loop
    return n


def benchmark(n: int = 10_000, repetitions: int = 5) -> float:
    return 1e-9
'''
    result = _validate(code)
    assert not result.passed
    assert any("timed out" in line for line in result.errors)
    # L12: the timeout must be attributed to the `timeout` episodic gate, not
    # misreported as the generic failure of whichever gate happened to run.
    from sis import episodic
    assert episodic.gate_from_reason(result.reason) == "timeout"


@pytest.mark.parametrize("evil", [
    "def sum_of_divisors(n: int) -> int:\n    return 0\n",          # constant
    "def sum_of_divisors(n: int) -> int:\n    return n\n",          # identity
])
def test_trivially_wrong_impls_are_rejected(evil: str) -> None:
    benchmark = "\ndef benchmark(n: int = 1, repetitions: int = 1) -> float:\n    return 1e-9\n"
    result = _validate(evil + benchmark)
    assert not result.passed
