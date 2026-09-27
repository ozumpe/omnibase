"""Runs INSIDE the sandbox: serve one candidate over stdin/stdout (OMNI-129).

``sis.sandbox_worker`` copies this file into the worker's directory as
``_sis_worker.py`` and starts it with the candidate's path and entry point. It
imports nothing from ``sis``: the sandbox holds no project code, only this file
and the candidate.

Protocol, one JSON object per line:

- out, once: ``{"ready": true}``, or ``{"ready": false, "error": "..."}`` when
  the candidate does not load;
- in: ``{"id": n, "calls": [[arg, ...], ...]}``;
- out: ``{"id": n, "results": [{"ok": value} | {"error": "Type: message"}]}``.

The candidate runs in this process, so the host trusts nothing written here:
it times every exchange on its own clock and decodes every answer itself.
What this file does protect is the channel. The candidate's own prints go to
/dev/null, not into the protocol, so a chatty candidate cannot corrupt it by
accident.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from typing import Any, TextIO


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


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


def serve(module_path: str, entry: str, requests: TextIO, channel: TextIO) -> int:
    """Load the candidate, say whether it loaded, then answer until stdin closes."""
    try:
        spec = importlib.util.spec_from_file_location("candidate", module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn = getattr(module, entry)
    except BaseException as exc:  # noqa: BLE001 - reported; the host decides
        _send(channel, {"ready": False, "error": _describe(exc)})
        return 1
    _send(channel, {"ready": True})
    for line in requests:
        try:
            request = json.loads(line)
            rid, calls = request["id"], request["calls"]
        except (ValueError, KeyError, TypeError) as exc:
            _send(channel, {"error": f"unreadable request: {_describe(exc)}"})
            continue
        results: list[dict[str, Any]] = []
        for args in calls:
            try:
                results.append(_encodable(fn(*args)))
            except BaseException as exc:  # noqa: BLE001 - SystemExit included
                results.append({"error": _describe(exc)})
        _send(channel, {"id": rid, "results": results})
    return 0


def main(argv: list[str]) -> int:
    # The protocol keeps the real stdout; fd 1 and sys.stdout become /dev/null
    # before the candidate is imported, so its prints land nowhere.
    channel = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - lives as long as the process
    return serve(argv[1], argv[2], sys.stdin, channel)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
