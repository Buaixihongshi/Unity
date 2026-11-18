"""Turn cost: provider-reported dollars when we have them, an estimate otherwise.

The provider reports `usage` on every turn (tokens, and sometimes a dollar
figure). Tokens are the durable fact; dollars are a view. This module keeps
that split: the price table lives here, and a cost is computed at read time, so
fixing a rate silently corrects every historical turn instead of leaving old
numbers in the database.

The estimate is deliberately rough and labelled "estimate" wherever it is
shown. A provider that reports `total_cost_usd` always wins over it.
"""
from __future__ import annotations

from typing import Optional

# Rough $ per 1M tokens, (input, output), per provider. Deliberately
# approximate: providers change prices and this file is not a billing system.
# The point is a number a human can feel, not a number to invoice with.
PROVIDER_PRICING: dict[str, tuple[float, float]] = {
    "claude": (3.0, 15.0),
    "codex": (2.5, 10.0),
    "gemini": (1.25, 5.0),
    "antigravity": (1.25, 5.0),
    "fake": (0.0, 0.0),
}

# Cached input is billed at a fraction of the fresh input rate. Anthropic's
# cache reads are ~10%; using one number for every provider keeps the estimate
# honest about being an estimate.
CACHE_DISCOUNT = 0.1

SOURCE_PROVIDER = "provider"
SOURCE_ESTIMATE = "estimate"


def _tokens(usage: dict, *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(int(value), 0)
    return 0


def estimate_cost(provider_id: str, usage: Optional[dict]) -> Optional[float]:
    """$ for one turn's tokens, or None when the provider has no rate here.

    None is meaningful: it says "we cannot price this", which the UI must show
    as a dash rather than $0.00. A zero would quietly understate spend.
    """
    if not usage:
        return None
    rates = PROVIDER_PRICING.get(provider_id)
    if rates is None:
        return None
    fresh_in = _tokens(usage, "input_tokens", "in")
    cached_in = _tokens(usage, "cached_input_tokens")
    cache_write = _tokens(usage, "cache_creation_input_tokens")
    output = _tokens(usage, "output_tokens", "out") + _tokens(usage, "reasoning_output_tokens")
    price_in, price_out = rates
    usd = (
        fresh_in / 1_000_000 * price_in
        + cached_in / 1_000_000 * price_in * CACHE_DISCOUNT
        + cache_write / 1_000_000 * price_in * 1.25
        + output / 1_000_000 * price_out
    )
    return round(usd, 6)


def turn_cost(provider_id: str, usage: Optional[dict]) -> tuple[Optional[float], Optional[str]]:
    """(dollars, source) for one turn. A provider-reported figure wins."""
    if not usage:
        return None, None
    reported = usage.get("total_cost_usd")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool):
        return round(float(reported), 6), SOURCE_PROVIDER
    if total_tokens(usage) == 0:
        # A usage record of all zeros means the provider reported nothing, not
        # that the turn was free. Pricing it at $0 would count as "known".
        return None, None
    estimated = estimate_cost(provider_id, usage)
    return estimated, (SOURCE_ESTIMATE if estimated is not None else None)


def total_tokens(usage: Optional[dict]) -> int:
    """Every token the turn spent, including cache reads and reasoning."""
    if not usage:
        return 0
    return (
        _tokens(usage, "input_tokens", "in")
        + _tokens(usage, "cached_input_tokens")
        + _tokens(usage, "cache_creation_input_tokens")
        + _tokens(usage, "output_tokens", "out")
        + _tokens(usage, "reasoning_output_tokens")
    )
