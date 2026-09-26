"""sis.canonical — rebuild a candidate's output from plain builtins before it is compared.

Every correctness gate ends in ``==``. Python hands ``a == b`` to ``a.__eq__``
first — or to ``b``'s reflected method first when ``type(b)`` subclasses
``type(a)`` — so a candidate returning ``class _Liar(int)`` whose ``__eq__`` is
always ``True`` agreed with the reference, with every acceptance assertion, with
the roman round-trip law and with the backtest comparators, all at once. It also
type-checks: the declared ``int`` return type is honest. KNOWN_ISSUES H4,
reproduced against the default contract, where ``_Liar(0)`` got "all gates
passed" (OMNI-46).

:func:`canonical` accepts only values whose type **is** one of a few builtins —
never a subclass — and rebuilds containers element by element, so whatever
reaches a comparison is made of types whose ``__eq__`` the candidate did not
write. Anything else raises :class:`NotPlainError`, which every gate reports as
the candidate's fault. :func:`wrap_exports` applies it to the contract's public
API, for gates where trusted code (an acceptance test, a round-trip law) calls
the candidate itself.

The type checks compare with ``is``, never ``in`` or ``==``: a metaclass can
override ``__eq__`` and ``__hash__`` on the class object itself, and set
membership would ask it.

The gauntlet copies this file into the sandbox as a standalone module
(:data:`SANDBOX_MODULE`), which is why it imports nothing from ``sis``.

What it does not do: stop a candidate that tampers with the *harness* — patches
this module or the gate script from inside the process it shares with them
(KNOWN_ISSUES H2/M9; OMNI-45 moves the candidate into its own worker process).
It defends against a hostile value, not a hostile process.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

# The module name the sandbox imports this file under.
SANDBOX_MODULE = "_sis_canonical"

# Deep enough for any honest output; shallow enough that a pathological one is
# refused with a reason instead of a RecursionError that reads like a harness bug.
MAX_DEPTH = 100

_SCALARS: tuple[type, ...] = (int, float, str, bool, type(None))
_CONTAINERS: tuple[type, ...] = (list, tuple, dict)


class NotPlainError(TypeError):
    """A candidate produced something other than a plain builtin value."""


def canonical(value: Any) -> Any:
    """*value*, rebuilt from exact builtins. Raises :class:`NotPlainError` otherwise.

    Scalars (``int``, ``float``, ``str``, ``bool``, ``None``) are returned as they
    are — an object whose type is exactly ``int`` has ``int``'s ``__eq__``.
    ``list``, ``tuple`` and ``dict`` are rebuilt, keys included, because their
    elements are where a hostile value would hide.
    """
    return _canonical(value, 0, set())


def _canonical(value: Any, depth: int, open_containers: set[int]) -> Any:
    kind = type(value)
    if any(kind is scalar for scalar in _SCALARS):
        return value
    if not any(kind is container for container in _CONTAINERS):
        raise NotPlainError(
            f"output contains a {_type_name(kind)}, not a plain builtin value "
            "(int, float, str, bool, None, or a list/tuple/dict of those)"
        )
    if depth >= MAX_DEPTH:
        raise NotPlainError(f"output is nested more than {MAX_DEPTH} levels deep")
    ident = id(value)
    if ident in open_containers:
        raise NotPlainError("output contains a reference cycle")
    open_containers.add(ident)
    try:
        if kind is dict:
            return {
                _canonical(key, depth + 1, open_containers): _canonical(
                    item, depth + 1, open_containers
                )
                for key, item in value.items()
            }
        items = [_canonical(item, depth + 1, open_containers) for item in value]
        return items if kind is list else tuple(items)
    finally:
        open_containers.discard(ident)


def _type_name(kind: type) -> str:
    # type.__getattribute__ rather than kind.__qualname__: the name is read
    # while reporting a hostile type, and its metaclass could make an ordinary
    # attribute lookup raise something other than NotPlainError.
    try:
        module = type.__getattribute__(kind, "__module__")
        name = type.__getattribute__(kind, "__qualname__")
        return f"{module}.{name}"
    except Exception:
        return "value of an unnamed type"


def wrap_exports(module: Any, names: Iterable[str]) -> None:
    """Make each named callable on *module* return :func:`canonical` output.

    Only the names the contract declares public, never "every callable": typing
    aliases and helper classes are callable too, and wrapping those would change
    behaviour honest code relies on. A missing or non-callable name is left
    alone — the interface gate and each script's own entry check report those.

    Raises :class:`NotPlainError` if the wrapper does not stick, e.g. because the
    candidate swapped its module's class for one that ignores ``setattr``.
    """
    for name in names:
        original = getattr(module, name, None)
        if not callable(original):
            continue
        wrapper = _canonicalising(original)
        setattr(module, name, wrapper)
        if getattr(module, name, None) is not wrapper:
            raise NotPlainError(
                f"{name!r}: the candidate module refused the harness's output check"
            )


def _canonicalising(fn: Callable[..., Any]) -> Callable[..., Any]:
    def call(*args: Any, **kwargs: Any) -> Any:
        return canonical(fn(*args, **kwargs))

    return call
