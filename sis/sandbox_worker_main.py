"""Runs INSIDE the sandbox: serve one candidate over stdin/stdout (OMNI-129).

``sis.sandbox_worker`` copies this file into the worker's directory as
``_sis_worker.py`` and starts it with the candidate's path and the names it
must export, the entry point first. It imports nothing from ``sis``: the
sandbox holds no project code, only this file and the candidate.

Protocol, one JSON object per line:

- out, once: ``{"ready": true, "exports": {name: {"callable": bool, "params":
  [...] | null}}}``, or ``{"ready": false, "error": "...", "missing": [...]}``
  when the candidate does not load or lacks a name;
- in: ``{"id": n, "calls": [[arg, ...], ...]}``, optionally with ``"fn"`` (an
  export other than the entry point), ``"kwargs"`` (one object per call) and
  ``"track_args": true``;
- out: ``{"id": n, "results": [{"ok": value} | {"error": "Type: message",
  "types": [...]}]}``. ``types`` is the exception's class hierarchy by name,
  so the host can raise the nearest builtin (OMNI-146). With ``track_args``, a
  call that changed its arguments also carries them as ``"args"``, so a caller
  that relies on its input being left alone can still see that it was not.

The candidate runs in this process, so the host trusts nothing written here:
it times every exchange on its own clock and decodes every answer itself.
What this file does protect is the channel. The candidate's own prints go to
/dev/null, not into the protocol, so a chatty candidate cannot corrupt it by
accident.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
from typing import Any, TextIO


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _failure(exc: BaseException) -> dict[str, Any]:
    return {"error": _describe(exc), "types": [kind.__name__ for kind in type(exc).__mro__]}


def _shape(obj: Any) -> dict[str, Any]:
    """What the interface gate needs to know about one export."""
    try:
        params: list[str] | None = list(inspect.signature(obj).parameters)
    except (TypeError, ValueError):
        params = None
    return {"callable": callable(obj), "params": params}


def _send(channel: TextIO, message: dict[str, Any]) -> None:
    channel.write(json.dumps(message) + "\n")
    channel.flush()


def _encodable(value: Any) -> dict[str, Any]:
    """One call's answer as it crosses the pipe, or why it cannot."""
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        return {"error": f"not JSON-representable: {_describe(exc)}"}
    return {"ok": value}


def _call(fn: Any, args: list[Any], kwargs: dict[str, Any], track: bool) -> dict[str, Any]:
    """One call's outcome, and its arguments when *track* is set and it changed them."""
    before = json.dumps([args, kwargs]) if track else ""
    try:
        result = _encodable(fn(*args, **kwargs))
    except BaseException as exc:  # noqa: BLE001 - SystemExit included
        result = _failure(exc)
    if track:
        try:
            after = json.dumps([args, kwargs])
        except (TypeError, ValueError):
            after = before  # changed into something that cannot cross; not mirrored
        if after != before:
            result["args"] = json.loads(after)[0]
    return result


def serve(module_path: str, exports: list[str], requests: TextIO, channel: TextIO) -> int:
    """Load the candidate, say whether it loaded, then answer until stdin closes.

    *exports* are the names the host will call, the entry point first. A name
    written ``?name`` is optional: served if the candidate has it, left out of
    the handshake if not.
    """
    required = [name for name in exports if not name.startswith("?")]
    optional = [name[1:] for name in exports if name.startswith("?")]
    try:
        spec = importlib.util.spec_from_file_location("candidate", module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        missing = [name for name in required if not hasattr(module, name)]
        if missing:
            _send(channel, {"ready": False, "missing": missing,
                            "error": f"AttributeError: the candidate does not export "
                                     f"{', '.join(missing)}"})
            return 1
        present = required + [name for name in optional
                              if name not in required and hasattr(module, name)]
        fns = {name: getattr(module, name) for name in present}
        shapes = {name: _shape(fn) for name, fn in fns.items()}
    except BaseException as exc:  # noqa: BLE001 - reported; the host decides
        _send(channel, {"ready": False, "error": _describe(exc)})
        return 1
    _send(channel, {"ready": True, "exports": shapes})
    for line in requests:
        try:
            request = json.loads(line)
            rid, calls = request["id"], request["calls"]
            fn = fns[request.get("fn", required[0])]
            kwargs = request.get("kwargs") or [{} for _ in calls]
            track = request.get("track_args") is True
        except (ValueError, KeyError, TypeError) as exc:
            _send(channel, {"error": f"unreadable request: {_describe(exc)}"})
            continue
        results = [_call(fn, args, kw, track) for args, kw in zip(calls, kwargs, strict=False)]
        _send(channel, {"id": rid, "results": results})
    return 0


def main(argv: list[str]) -> int:
    # The protocol keeps the real stdout; fd 1 and sys.stdout become /dev/null
    # before the candidate is imported, so its prints land nowhere.
    channel = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - lives as long as the process
    return serve(argv[1], argv[2:], sys.stdin, channel)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
