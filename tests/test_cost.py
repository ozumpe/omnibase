"""Tests for LLM cost accounting (no Ray, no network)."""

import pytest

from sis.cost import UnpricedModel, cost_from_usage, cost_usd, price


def test_cost_usd_opus_pricing() -> None:
    # 1M input + 1M output on opus-4-8 = $5 + $25 = $30.
    assert cost_usd(1_000_000, 1_000_000) == 30.0


def test_cache_tokens_billed_at_reduced_rates() -> None:
    # cache write ~1.25x input, cache read ~0.1x input.
    c = cost_usd(0, 0, cache_creation_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert round(c, 4) == round(1.25 * 5.0 + 0.1 * 5.0, 4)


def test_cost_from_usage_duck_typed() -> None:
    class Usage:
        input_tokens = 500_000
        output_tokens = 200_000
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0

    # 0.5M*$5 + 0.2M*$25 = 2.5 + 5.0
    assert cost_from_usage(Usage()) == 7.5


def test_an_unpriced_model_is_refused_not_billed_at_opus_rates() -> None:
    # L25 (OMNI-71): an unknown id used to be priced at claude-opus-4-8 rates,
    # which under-counts any pricier model against the hard spend cap.
    with pytest.raises(UnpricedModel, match="not-a-model"):
        cost_usd(1_000_000, 0, model="not-a-model")
    with pytest.raises(UnpricedModel, match="claude-opus-4-8-20260101"):
        price("claude-opus-4-8-20260101")   # a dated id is not the alias it resembles


def test_the_current_models_are_priced() -> None:
    # Published first-party rates, $/MTok, checked 2026-10-27.
    assert price("claude-fable-5-1") == (10.0, 50.0)
    assert price("claude-fable-5") == (10.0, 50.0)
    assert price("claude-opus-5-5") == (4.0, 20.0)
    assert price("claude-opus-5") == (5.0, 25.0)
    assert price("claude-sonnet-5-5") == (2.0, 10.0)
    # Haiku 5.5 bills prompts over 100K tokens at a higher rate; the table holds
    # that one, so a long prompt is never under-counted.
    assert price("claude-haiku-5-5") == (0.5, 2.5)


def test_pricing_table_matches_published_rates() -> None:
    # L3: verified against published $/MTok on 2026-07-25. The loop prices its
    # spend with proposer.MODEL (claude-opus-4-8) — guard that one especially.
    assert cost_usd(1_000_000, 1_000_000, model="claude-opus-4-8") == 30.0    # $5 + $25
    assert cost_usd(1_000_000, 1_000_000, model="claude-sonnet-5") == 18.0    # $3 + $15
    assert cost_usd(1_000_000, 1_000_000, model="claude-haiku-4-5") == 6.0    # $1 + $5
