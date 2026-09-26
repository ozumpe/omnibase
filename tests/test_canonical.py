"""sis.canonical: what every comparing gate reduces candidate output to (OMNI-46, H4).

The gate-level regressions live in ``tests/test_adversarial.py``; these pin the
function itself, including the ways a hostile *type* — not just a hostile value —
could try to pass as a builtin.
"""

from __future__ import annotations

import ast
import sys
import types
from typing import Any

import pytest

from sis import canonical as canonical_module
from sis.canonical import MAX_DEPTH, NotPlainError, canonical, wrap_exports
from sis.policy import ChangeTier, classify


class _LiarInt(int):
    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False

    __hash__ = int.__hash__


class _LiarStr(str):
    def __eq__(self, other: object) -> bool:
        return True

    __hash__ = str.__hash__


class _LiarList(list[int]):
    def __eq__(self, other: object) -> bool:
        return True


class _LiarDict(dict[str, int]):
    pass


class _LiarTuple(tuple[int, ...]):
    pass


class _LiarFloat(float):
    pass


class _PosingMeta(type):
    """A metaclass whose *class objects* claim to equal and hash like ``int``."""

    def __eq__(cls, other: object) -> bool:
        return True

    def __hash__(cls) -> int:
        return hash(int)


class _Posing(metaclass=_PosingMeta):
    pass


def _same_shape(a: Any, b: Any) -> bool:
    """Equal *and* built from exactly the same types, all the way down."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return len(a) == len(b) and all(
            _same_shape(k, k2) and _same_shape(a[k], b[k2]) for k, k2 in zip(a, b, strict=True)
        )
    if isinstance(a, list | tuple):
        return len(a) == len(b) and all(_same_shape(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b) or (a != a and b != b)  # NaN equals itself here


@pytest.mark.parametrize(
    "value",
    [
        0, -7, 2**100, 1.5, float("nan"), "", "IV", True, False, None,
        [], [1, 2, 3], (), (1, "a", None), {}, {"k": [1, (2.0, "x")]},
        {(1, 2): {"nested": [True, None]}},
    ],
)
def test_plain_values_come_back_equal_and_of_the_same_types(value: Any) -> None:
    assert _same_shape(canonical(value), value)


def test_containers_are_rebuilt_rather_than_passed_through() -> None:
    value = [1, [2, 3], {"a": (4,)}]
    result = canonical(value)
    assert result is not value
    assert result[1] is not value[1]


@pytest.mark.parametrize(
    "hostile",
    [
        _LiarInt(0), _LiarStr("IV"), _LiarList([1]), _LiarDict(a=1), _LiarTuple((1,)),
        _LiarFloat(1.0), _Posing(), object(), {1, 2}, frozenset({1}), b"bytes", 1j,
    ],
    ids=lambda v: type(v).__name__,
)
def test_anything_but_an_exact_builtin_is_refused(hostile: Any) -> None:
    with pytest.raises(NotPlainError):
        canonical(hostile)


@pytest.mark.parametrize(
    "where",
    [
        lambda h: [1, 2, h],
        lambda h: (h,),
        lambda h: {"key": h},
        lambda h: {h: "value"},
        lambda h: [[[{"deep": [h]}]]],
    ],
    ids=["list item", "tuple item", "dict value", "dict key", "deeply nested"],
)
def test_a_hostile_value_is_found_wherever_it_hides(where: Any) -> None:
    with pytest.raises(NotPlainError):
        canonical(where(_LiarInt(0)))


def test_a_metaclass_cannot_make_its_class_look_like_int() -> None:
    # Membership in a set of types would ask the metaclass's __eq__/__hash__,
    # which is why the checks compare with `is`.
    assert _Posing == int and hash(_Posing) == hash(int)  # noqa: E721 — the point
    with pytest.raises(NotPlainError):
        canonical(_Posing())


def test_a_type_whose_name_cannot_be_read_is_still_refused_cleanly() -> None:
    class _Unnamable(type):
        def __getattribute__(cls, name: str) -> Any:
            raise RuntimeError("no")

    hostile = _Unnamable("Hostile", (), {})()
    with pytest.raises(NotPlainError):
        canonical(hostile)


def test_a_reference_cycle_is_refused_not_a_recursion_error() -> None:
    cyclic: list[Any] = [1]
    cyclic.append(cyclic)
    with pytest.raises(NotPlainError, match="cycle"):
        canonical(cyclic)


def test_shared_but_acyclic_structure_is_fine() -> None:
    shared = [1, 2]
    assert canonical([shared, shared]) == [[1, 2], [1, 2]]


def test_nesting_is_capped_with_a_reason() -> None:
    ok: Any = 0
    for _ in range(MAX_DEPTH - 1):
        ok = [ok]
    canonical(ok)
    too_deep: Any = 0
    for _ in range(MAX_DEPTH + 1):
        too_deep = [too_deep]
    with pytest.raises(NotPlainError, match="nested"):
        canonical(too_deep)


# --- wrap_exports -----------------------------------------------------------


def _module(**attrs: Any) -> types.ModuleType:
    module = types.ModuleType("candidate")
    for name, value in attrs.items():
        setattr(module, name, value)
    return module


def test_wrapped_exports_return_plain_values_and_refuse_liars() -> None:
    module = _module(good=lambda n: [n, n], liar=lambda: _LiarInt(0))
    wrap_exports(module, ["good", "liar"])
    assert module.good(3) == [3, 3]
    with pytest.raises(NotPlainError):
        module.liar()


def test_only_the_named_exports_are_wrapped() -> None:
    helper = staticmethod(lambda: _LiarInt(0))  # callable, but not public API
    module = _module(entry=lambda: 1, helper=helper)
    wrap_exports(module, ["entry"])
    assert module.helper is helper


def test_missing_and_non_callable_names_are_left_for_the_interface_gate() -> None:
    module = _module(CONSTANT=5)
    wrap_exports(module, ["CONSTANT", "absent"])
    assert module.CONSTANT == 5
    assert not hasattr(module, "absent")


def test_a_callable_object_entry_is_wrapped_too() -> None:
    # An entry that is an instance with __call__ rather than a function is
    # exactly how a candidate would try to slip past a function-only wrapper.
    class _Entry:
        def __call__(self) -> Any:
            return _LiarInt(0)

    module = _module(entry=_Entry())
    wrap_exports(module, ["entry"])
    with pytest.raises(NotPlainError):
        module.entry()


def test_a_module_that_ignores_setattr_is_refused() -> None:
    class _Stubborn(types.ModuleType):
        def __setattr__(self, name: str, value: Any) -> None:
            pass

    module = _Stubborn("candidate")
    types.ModuleType.__setattr__(module, "entry", lambda: _LiarInt(0))
    with pytest.raises(NotPlainError, match="refused"):
        wrap_exports(module, ["entry"])


def test_arguments_reach_the_candidate_unchanged() -> None:
    # No copying in the wrapper: an acceptance test that checks the candidate
    # does not mutate its input must see the candidate's real behaviour.
    def mutate(values: list[int]) -> list[int]:
        values.append(99)
        return []

    module = _module(entry=mutate)
    wrap_exports(module, ["entry"])
    given = [1]
    module.entry(given)
    assert given == [1, 99]


# --- where the module lives -------------------------------------------------


def test_the_loop_may_never_rewrite_the_canonicaliser() -> None:
    assert classify("sis/canonical.py") is ChangeTier.FORBIDDEN


def test_the_sandbox_copy_needs_nothing_from_sis() -> None:
    # The gauntlet copies this file into the sandbox as a standalone module, so
    # an import of `sis` would fail there (and only there).
    tree = ast.parse(open(canonical_module.__file__, encoding="utf-8").read())
    imported = {
        (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert not any(name == "sis" or name.startswith("sis.") for name in imported)
    assert all(name.split(".")[0] in sys.stdlib_module_names | {"__future__"}
               for name in imported)
