"""sis.cost — LLM cost accounting for the budget gate.

Turns Anthropic token usage into dollars so the CEO can enforce a hard
spend cap and track cost-per-accepted-improvement. Prices are $/1M tokens
(see the claude-api model table); cache writes bill ~1.25x and cache reads
~0.1x of the input price.
"""

from __future__ import annotations

from typing import Any

# model id -> (input $/1M, output $/1M). Verified against published rates
# 2026-07-25; the Claude 5 family's rows on 2026-10-27. Where a model has more
# than one rate, the higher is used so the spend cap never *under*-counts:
# Sonnet 5's standard rate rather than its intro price, Haiku 5.5's rate for
# prompts over 100K tokens.
PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-5-5": (0.5, 2.5),
    "claude-haiku-4-5": (1.0, 5.0),
}


class UnpricedModel(ValueError):
    """A model id with no row in :data:`PRICING` (L25, OMNI-71).

    Its spend cannot be counted against the hard cap, so it is refused rather
    than priced at another model's rate, which under-counts any pricier one.
    """


def price(model: str) -> tuple[float, float]:
    """``(input, output)`` $/1M tokens for *model*; raises :class:`UnpricedModel`."""
    try:
        return PRICING[model]
    except KeyError:
        known = ", ".join(sorted(PRICING))
        raise UnpricedModel(
            f"model {model!r} has no price in sis/cost.py, so its spend could not be "
            f"counted against the cap (known: {known})") from None


def cost_usd(
    input_tokens: int,
    output_tokens: int,
    *,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    model: str = "claude-opus-4-8",
) -> float:
    """Dollar cost of one request given its token counts."""
    in_price, out_price = price(model)
    billed_input = input_tokens + cache_creation_tokens * 1.25 + cache_read_tokens * 0.1
    return billed_input / 1_000_000 * in_price + output_tokens / 1_000_000 * out_price


def cost_from_usage(usage: Any, model: str = "claude-opus-4-8") -> float:
    """Dollar cost from an Anthropic ``message.usage`` object (duck-typed)."""
    return cost_usd(
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
        cache_creation_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        model=model,
    )
