"""Hand-written optimised replacement for ``sort_target.sort_numbers``.

What the stub proposer (``SIS_PROPOSER=stub``, the offline/CI default) returns
for the ``sort`` contract, standing in for what a real LLM would produce. Zero
API calls, so the loop stays runnable with no key.

A merge sort rather than a one-line ``return sorted(values)`` on purpose: the
contract's oracle *is* ``sorted()``, so a ``sorted()`` candidate would make the
differential-correctness gate compare a function with itself and pass
tautologically. A genuine second implementation keeps the default path
exercising the gate for real. (A real proposer answering ``sorted()`` would
still be correct and would still be accepted — it just wouldn't demonstrate
anything.)

O(n log n) against the baseline's O(n²).
"""


def sort_numbers(values: list[int]) -> list[int]:
    """Return a new list with *values* in ascending order.

    Top-down merge sort. Stable, and never mutates the caller's list.
    """
    if len(values) <= 1:
        return list(values)

    middle = len(values) // 2
    left = sort_numbers(values[:middle])
    right = sort_numbers(values[middle:])
    return _merge(left, right)


def _merge(left: list[int], right: list[int]) -> list[int]:
    """Merge two ascending lists into one. ``<=`` keeps the sort stable."""
    merged: list[int] = []
    i = j = 0
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:
            merged.append(left[i])
            i += 1
        else:
            merged.append(right[j])
            j += 1
    merged.extend(left[i:])
    merged.extend(right[j:])
    return merged
