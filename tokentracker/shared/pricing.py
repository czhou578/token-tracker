from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float
    cache_read_per_million: float = 0.0
    cache_write_per_million: float = 0.0


PRICES: dict[str, ModelPrice] = {
    "claude-3-5-haiku": ModelPrice(0.80, 4.00, 0.08, 1.00),
    "claude-3-5-sonnet": ModelPrice(3.00, 15.00, 0.30, 3.75),
    "claude-3-7-sonnet": ModelPrice(3.00, 15.00, 0.30, 3.75),
    "claude-sonnet-4": ModelPrice(3.00, 15.00, 0.30, 3.75),
    "claude-opus-4": ModelPrice(15.00, 75.00, 1.50, 18.75),
}


def estimate_cost_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    key = model.lower()
    price = next((value for prefix, value in PRICES.items() if key.startswith(prefix)), None)
    if price is None:
        return 0.0

    cost = (
        prompt_tokens * price.input_per_million
        + completion_tokens * price.output_per_million
        + cache_read_tokens * price.cache_read_per_million
        + cache_write_tokens * price.cache_write_per_million
    ) / 1_000_000
    return round(cost, 6)
