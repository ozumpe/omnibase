"""Tests for LLM cost accounting (no Ray, no network)."""

from sis.cost import cost_from_usage, cost_usd


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


def test_unknown_model_falls_back_to_opus() -> None:
    assert cost_usd(1_000_000, 0, model="not-a-model") == 5.0


def test_pricing_table_matches_published_rates() -> None:
    # L3: verified against published $/MTok on 2026-07-25. The loop prices its
    # spend with proposer.MODEL (claude-opus-4-8) — guard that one especially.
    assert cost_usd(1_000_000, 1_000_000, model="claude-opus-4-8") == 30.0    # $5 + $25
    assert cost_usd(1_000_000, 1_000_000, model="claude-sonnet-5") == 18.0    # $3 + $15
    assert cost_usd(1_000_000, 1_000_000, model="claude-haiku-4-5") == 6.0    # $1 + $5
