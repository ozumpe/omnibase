"""Acceptance tests for the ``sort_numbers`` target.

Run **inside the gauntlet sandbox** against whichever candidate is being
validated, where ``import target`` resolves to that candidate — not to
``runtime/sort_target.py``. Both the naive baseline and any generated
replacement must pass.

POLICY-FORBIDDEN (``specs/`` is in ``sis.policy.GUARDRAIL_DIRS``): the
implementer cannot edit its own exam.

Note there is deliberately no ``benchmark()`` test here, unlike the
``sum_of_divisors`` contract. The required public API is a property of the
contract, not of the engine.
"""

import random

import pytest
import target


def test_sorts_a_simple_list() -> None:
    assert target.sort_numbers([3, 1, 2]) == [1, 2, 3]
    assert target.sort_numbers([5, 4, 3, 2, 1]) == [1, 2, 3, 4, 5]


def test_handles_empty_and_single() -> None:
    # The classic off-by-one hiding places.
    assert target.sort_numbers([]) == []
    assert target.sort_numbers([42]) == [42]


def test_handles_duplicates_and_negatives() -> None:
    assert target.sort_numbers([3, -1, 3, 0, -1]) == [-1, -1, 0, 3, 3]


def test_already_sorted_is_unchanged() -> None:
    assert target.sort_numbers([1, 2, 3, 4]) == [1, 2, 3, 4]


@pytest.mark.parametrize("length", [3, 6, 50, 1200])
def test_does_not_mutate_its_input(length: int) -> None:
    # Callers rely on getting a new list. An in-place sort would also let a
    # candidate quietly pre-sort the very list the reference is handed next.
    # Several sizes, spanning random_input's range: a 3-element list alone let
    # through a candidate that emptied any input longer than five (M10).
    original = random.Random(length).sample(range(-5000, 5000), length)
    given = list(original)
    target.sort_numbers(given)
    assert given == original


def test_output_is_a_sorted_permutation_of_the_input() -> None:
    # The defining property, checked on random inputs rather than fixtures: a
    # candidate can special-case the cases above, but not this.
    #
    # Judged against a copy taken *before* the call. Checked against `values`
    # itself, a candidate that emptied its input and returned [] satisfied
    # every assertion here — the empty list is a sorted permutation of itself.
    rng = random.Random(1234)
    for _ in range(50):
        values = [rng.randint(-100, 100) for _ in range(rng.randint(0, 80))]
        before = list(values)
        result = target.sort_numbers(values)
        assert len(result) == len(before)
        assert sorted(before) == result
        assert all(result[i] <= result[i + 1] for i in range(len(result) - 1))
