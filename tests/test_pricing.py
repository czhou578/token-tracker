from __future__ import annotations

import json

import pytest

from tokentracker.collector.models import UsageEvent
from tokentracker.collector.pricing import DEFAULT_PRICING, Pricing, normalize_model


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("claude-fable-5-1[1m]", "claude-fable-5-1"),
        ("anthropic/claude-opus-5-5", "claude-opus-5-5"),
        ("us.anthropic.claude-opus-4-6-v1:0", "claude-opus-4-6"),
        ("anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5-20250929"),
        ("claude-opus-4-5@20251101", "claude-opus-4-5-20251101"),
        ("  Claude-Haiku-5-5 ", "claude-haiku-5-5"),
        (None, ""),
    ],
)
def test_normalize_model(raw, expected) -> None:
    assert normalize_model(raw) == expected


@pytest.mark.parametrize(
    ("model", "key"),
    [
        ("claude-opus-5-5", "claude-opus-5-5"),
        ("claude-opus-5", "claude-opus-5"),
        ("claude-opus-5-20260101", "claude-opus-5"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
        ("claude-opus-4-20250514", "claude-opus-4"),
        ("claude-opus-4-1-20250805", "claude-opus-4-1"),
        ("claude-fable-5-1[1m]", "claude-fable-5-1"),
        ("claude-3-5-sonnet-20241022", "claude-3-5-sonnet"),
    ],
)
def test_resolve_picks_longest_matching_prefix(model, key) -> None:
    assert Pricing().resolve(model) is DEFAULT_PRICING[key]


def test_resolve_returns_none_for_local_models() -> None:
    pricing = Pricing()
    assert pricing.resolve("qwen3.8-27b") is None
    assert pricing.resolve("deepseek-v4-flash-0731") is None
    assert pricing.resolve("claude-opus-55") is None  # no separator after the key
    assert pricing.resolve(None) is None


def test_rate_cards_match_the_pricing_page() -> None:
    opus = DEFAULT_PRICING["claude-opus-5-5"]
    assert (opus["input"], opus["cache_write_5m"], opus["cache_write_1h"], opus["cache_read"], opus["output"]) == (4, 5, 8, 0.2, 20)
    fable = DEFAULT_PRICING["claude-fable-5-1"]
    assert (fable["input"], fable["cache_write_5m"], fable["cache_write_1h"], fable["cache_read"], fable["output"]) == (10, 12.5, 20, 0.25, 50)
    sonnet = DEFAULT_PRICING["claude-sonnet-5-5"]
    assert (sonnet["cache_read"], sonnet["output"]) == (0.1, 10)
    haiku = DEFAULT_PRICING["claude-haiku-5-5"]
    assert (haiku["input"], haiku["cache_write_5m"], haiku["cache_write_1h"], haiku["cache_read"], haiku["output"]) == (0.1, 0.125, 0.2, 0.01, 0.5)
    assert haiku["over_100k"]["input"] == 0.5 and haiku["over_100k"]["output"] == 2.5


def test_estimate_applies_cache_split() -> None:
    cost = Pricing().estimate(
        "claude-opus-5-5",
        prompt_tokens=1000,
        completion_tokens=500,
        cache_read_tokens=10_000,
        cache_write_tokens=2_000,
        cache_write_1h_tokens=1_500,
    )
    # 1000*4 + 500*20 + 10000*0.2 + 500*5 + 1500*8 = 30,500 micro-dollars
    assert cost == pytest.approx(0.0305)


def test_estimate_prices_unknown_split_at_five_minute_rate() -> None:
    assert Pricing().estimate("claude-opus-5-5", cache_write_tokens=1_000_000) == pytest.approx(5.0)


def test_estimate_fast_mode_and_us_inference_and_web_search() -> None:
    pricing = Pricing()
    assert pricing.estimate("claude-opus-5-5", prompt_tokens=1_000_000, speed="fast") == pytest.approx(8.0)
    assert pricing.estimate("claude-opus-5-5", prompt_tokens=1_000_000, speed="fast", cache_read_tokens=1_000_000) == pytest.approx(8.4)
    assert pricing.estimate("claude-opus-5-5", prompt_tokens=1_000_000, inference_geo="us") == pytest.approx(4.4)
    assert pricing.estimate("claude-opus-5-5", web_search_requests=3) == pytest.approx(0.03)
    # Fast mode on a model without fast rates falls back to standard rates.
    assert pricing.estimate("claude-sonnet-5-5", prompt_tokens=1_000_000, speed="fast") == pytest.approx(2.0)


def test_estimate_haiku_long_prompt_tier() -> None:
    pricing = Pricing()
    assert pricing.estimate("claude-haiku-5-5", prompt_tokens=50_000) == pytest.approx(0.005)
    assert pricing.estimate("claude-haiku-5-5", prompt_tokens=150_000) == pytest.approx(0.075)
    # Cache reads count toward the prompt length.
    assert pricing.estimate("claude-haiku-5-5", prompt_tokens=1_000, cache_read_tokens=120_000) == pytest.approx(0.0005 + 0.006)


def test_estimate_returns_none_for_unpriced_model() -> None:
    assert Pricing().estimate("qwen3.8-27b", prompt_tokens=1_000_000) is None


def test_overrides_add_and_remove_models(tmp_path) -> None:
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps({"deepseek-v4-flash": {"input": 0.14, "output": 0.28, "cache_read": 0.0028}, "claude-opus-5-5": None}))
    pricing = Pricing.load(path)
    assert pricing.estimate("deepseek-v4-flash-0731", prompt_tokens=1_000_000, completion_tokens=1_000_000) == pytest.approx(0.42)
    # A null override blocks the model and its dated variants without falling back to "claude-opus-5".
    assert pricing.estimate("claude-opus-5-5", prompt_tokens=1_000_000) is None
    assert pricing.estimate("claude-opus-5-5-20260401", prompt_tokens=1_000_000) is None
    assert pricing.estimate("claude-opus-5", prompt_tokens=1_000_000) == pytest.approx(5.0)
    assert Pricing.load(tmp_path / "missing.json").table == DEFAULT_PRICING


def test_load_ignores_invalid_override_file(tmp_path) -> None:
    path = tmp_path / "pricing.json"
    path.write_text("not json")
    assert Pricing.load(path).table == DEFAULT_PRICING


def test_price_event_prefers_reported_cost_and_records_source() -> None:
    pricing = Pricing()
    reported = UsageEvent(source_id="a", model="deepseek-v4-flash", prompt_tokens=10, metadata_json=json.dumps({"reported_cost_usd": 0.5}))
    priced = pricing.price_event(reported)
    assert priced.cost_usd == 0.5
    assert json.loads(priced.metadata_json)["cost_source"] == "reported"

    estimated = UsageEvent(source_id="b", model="claude-opus-5-5", prompt_tokens=1_000_000)
    priced = pricing.price_event(estimated)
    assert priced.cost_usd == pytest.approx(4.0)
    assert json.loads(priced.metadata_json)["cost_source"] == "table"

    unknown = UsageEvent(source_id="c", model="qwen3.8-27b", prompt_tokens=5, metadata_json=json.dumps({"cost_source": "table"}))
    priced = pricing.price_event(unknown)
    assert priced.cost_usd is None
    assert "cost_source" not in json.loads(priced.metadata_json)
