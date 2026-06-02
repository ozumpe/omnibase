"""sis.proposer — code proposer (stub or real Claude).

Selected by the ``SIS_PROPOSER`` env var:

- ``stub`` (default): returns the hand-written O(√n) variant from
  runtime/candidates/. Zero API calls — keeps the loop runnable offline and
  in tests.
- ``claude``: calls the Anthropic API (model ``claude-opus-4-8``) with the
  current source + baseline, and returns a fully type-annotated optimised
  version. The candidate then goes through the full gauntlet exactly like
  the stub's — the LLM is never trusted, the deterministic gates are.

The ``anthropic`` SDK is an optional dependency, imported lazily, so the
default path needs neither the package nor an API key.
"""

from __future__ import annotations

import os
import re

from sis.cost import cost_from_usage
from sis.paths import OPTIMISED_CANDIDATE_PATH

MODEL = "claude-opus-4-8"
MAX_TOKENS = 8000

# Dollar cost of the most recent propose() call (0.0 for the stub). The CEO
# reads this to enforce the spend cap and the cost-per-accepted SLO.
_last_cost_usd: float = 0.0


def last_cost_usd() -> float:
    """USD cost of the most recent propose() call (0.0 for the stub)."""
    return _last_cost_usd

# Stable, cacheable instructions (the system prompt). Kept byte-frozen — no
# timestamps or per-request data — so prompt caching reuses it across cycles.
_SYSTEM_PROMPT = """\
You optimise a single Python module for speed without changing its behaviour.

Hard requirements for your output:
- Return ONLY the complete replacement module source — no prose, no markdown
  fences, no explanation.
- Preserve the public API exactly: a function `sum_of_divisors(n: int) -> int`
  that returns the sum of all divisors of n (including 1 and n), and a
  `benchmark(n: int = 10_000, repetitions: int = 5) -> float`.
- Results must be identical to the input for every input; only the
  implementation may change.
- The module MUST be fully type-annotated and pass `mypy --strict`.
- Use only the Python standard library.

The candidate you return will be validated by a strict gauntlet
(ast.parse → mypy --strict → pytest → differential correctness on random
inputs → benchmark vs baseline). Anything that changes results, fails typing,
or isn't faster is rejected, so prioritise correctness, then speed."""


def propose(current_source: str, baseline_latency: float) -> str:
    """Return a candidate replacement for the target module."""
    global _last_cost_usd
    _last_cost_usd = 0.0  # reset; the stub is free
    mode = os.getenv("SIS_PROPOSER", "stub")
    if mode == "claude":
        return _claude_proposal(current_source, baseline_latency)
    return _stub_proposal()


def _stub_proposal() -> str:
    """Hand-written O(√n) replacement read from runtime/candidates/."""
    return OPTIMISED_CANDIDATE_PATH.read_text(encoding="utf-8")


def _claude_proposal(current_source: str, baseline_latency: float) -> str:
    """Ask Claude for a typed, optimised variant (model: claude-opus-4-8)."""
    import anthropic

    client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY from the env
    user_prompt = (
        f"Current module source (baseline mean latency {baseline_latency:.6f}s):\n\n"
        f"{current_source}\n\n"
        "Return an optimised replacement module that is correct, fully typed, "
        "and faster."
    )
    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=[{"type": "text", "text": _SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_prompt}],
    )
    global _last_cost_usd
    _last_cost_usd = cost_from_usage(message.usage, MODEL)
    text = "".join(block.text for block in message.content if block.type == "text")
    return _extract_code(text)


def _extract_code(text: str) -> str:
    """Strip optional markdown fences and return the module source."""
    fence = re.search(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
    return (fence.group(1) if fence else text).strip() + "\n"
