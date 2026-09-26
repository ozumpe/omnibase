"""sort_target.py — the second thing being optimized.

A deliberately slow sort, so the loop has a target whose shape differs from
``target.py``'s ``sum_of_divisors`` in the ways that matter:

- **Input size scales freely.** ``sum_of_divisors`` takes one int; per-call work
  is capped by that int's size. A sort takes a *list*, so per-request work can
  be made to dominate Ray Serve's ~ms dispatch overhead when this becomes the
  served canary target (OMNI-11). Without that, a live p95 comparison measures
  the framework rather than the candidate.
- **The entry takes and returns a collection**, exercising the contract
  machinery on a non-scalar signature.
- **No ``benchmark()`` helper.** ``sum_of_divisors``' contract requires one
  because *its acceptance tests* call it; this contract's don't. That the two
  targets need different public APIs is the point — the required interface is a
  property of the contract, not of the engine.

This file is RUNTIME-MUTABLE: the loop overwrites it on a successful promotion.
The naive O(n²) bubble sort here is the starting baseline.
"""


def sort_numbers(values: list[int]) -> list[int]:
    """Return a new list with *values* in ascending order.

    Intentionally naive O(n²) bubble sort — the loop has an obvious
    O(n log n) optimisation to propose.

    Returns a new list; the caller's input is never mutated. Callers rely on
    that (the acceptance tests pin it), and an in-place sort would also let a
    candidate quietly pre-sort the input the reference is about to be handed.
    """
    result = list(values)
    n = len(result)
    for i in range(n):
        for j in range(n - i - 1):
            if result[j] > result[j + 1]:
                result[j], result[j + 1] = result[j + 1], result[j]
    return result
