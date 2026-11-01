"""List-price cost estimates for usage events.

Rates are USD per million tokens, copied from Anthropic's pricing page
(https://platform.claude.com/docs/en/about-claude/pricing) on 2026-10-31.
Rules applied from that page:

- 5-minute cache writes cost 1.25x the input rate, 1-hour writes 2x.
- Cache reads cost 0.1x the input rate, except 0.025x on Claude Fable 5.1 and
  Claude Mythos 5.1, and 0.05x on Claude Opus 5.5 and Claude Sonnet 5.5.
- Fast mode has its own input and output rates; cache multipliers stack on them.
- Claude Haiku 5.5 charges higher rates when a request's prompt (input plus
  cache reads plus cache writes) exceeds 100,000 tokens.
- US-only inference (`inference_geo: "us"`) multiplies token prices by 1.1.
- Web search costs $10 per 1,000 searches. Web fetch is free beyond tokens.
- Output tokens already include thinking tokens.

Local models have no list price. Add one to `~/.tokentracker/pricing.json` to
see what the same tokens would cost at a hosted provider; until then their
events carry `cost_usd = NULL` and the dashboard reports them as unpriced.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from tokentracker.collector.models import UsageEvent

PRICING_SOURCE_DATE = "2026-10-31"
LONG_PROMPT_THRESHOLD = 100_000
WEB_SEARCH_USD_PER_REQUEST = 10.0 / 1000
US_INFERENCE_MULTIPLIER = 1.1

Rates = dict[str, Any]


def standard(input_usd: float, output_usd: float, cache_read_multiplier: float = 0.1) -> Rates:
    """Build a rate card from the base input and output prices per MTok."""
    return {
        "input": input_usd,
        "output": output_usd,
        "cache_read": round(input_usd * cache_read_multiplier, 6),
        "cache_write_5m": round(input_usd * 1.25, 6),
        "cache_write_1h": round(input_usd * 2, 6),
    }


DEFAULT_PRICING: dict[str, Rates] = {
    "claude-fable-5-1": standard(10, 50, 0.025),
    "claude-mythos-5-1": standard(10, 50, 0.025),
    "claude-fable-5": standard(10, 50),
    "claude-mythos-5": standard(10, 50),
    "claude-opus-5-5": standard(4, 20, 0.05) | {"fast": standard(8, 40, 0.05)},
    "claude-opus-5": standard(5, 25) | {"fast": standard(10, 50)},
    "claude-opus-4-8": standard(5, 25) | {"fast": standard(10, 50)},
    "claude-opus-4-7": standard(5, 25),
    "claude-opus-4-6": standard(5, 25),
    "claude-opus-4-5": standard(5, 25),
    "claude-opus-4-1": standard(15, 75),
    "claude-opus-4": standard(15, 75),
    "claude-sonnet-5-5": standard(2, 10, 0.05),
    "claude-sonnet-5": standard(2, 10),
    "claude-sonnet-4-6": standard(3, 15),
    "claude-sonnet-4-5": standard(3, 15),
    "claude-sonnet-4": standard(3, 15),
    "claude-haiku-5-5": standard(0.10, 0.50) | {"over_100k": standard(0.50, 2.50)},
    "claude-haiku-4-5": standard(1, 5),
    "claude-3-7-sonnet": standard(3, 15),
    "claude-3-5-sonnet": standard(3, 15),
    "claude-3-5-haiku": standard(0.80, 4),
}

_PROVIDER_PREFIX = re.compile(r"^(?:[a-z]{2}\.)?anthropic[./]")
_BEDROCK_SUFFIX = re.compile(r"-v\d+:\d+$")


def normalize_model(model: str | None) -> str:
    """Reduce a model id to the bare Anthropic form used as a pricing key.

    Handles Claude Code aliases (`claude-fable-5-1[1m]`), OpenRouter-style
    prefixes (`anthropic/claude-opus-5-5`), Bedrock ids
    (`us.anthropic.claude-opus-5-5-v1:0`) and Vertex ids
    (`claude-opus-4-5@20251101`).
    """
    if not model:
        return ""
    name = model.strip().lower()
    name = name.split("[", 1)[0]
    name = name.rsplit("/", 1)[-1] if name.startswith("anthropic/") else name
    name = _PROVIDER_PREFIX.sub("", name)
    name = _BEDROCK_SUFFIX.sub("", name)
    name = name.replace("@", "-")
    return name


class Pricing:
    """A rate table: bundled defaults, optionally merged with a JSON override file."""

    def __init__(self, table: Mapping[str, Rates | None] | None = None, source: Path | None = None):
        merged: dict[str, Rates] = dict(DEFAULT_PRICING)
        blocked: set[str] = set()
        for key, value in (table or {}).items():
            name = normalize_model(key)
            if value is None:
                # A null override unprices the model and its dated variants, and
                # stops the lookup from falling back to a shorter key.
                merged.pop(name, None)
                blocked.add(name)
            else:
                merged[name] = dict(value)
                blocked.discard(name)
        self.table = merged
        self.blocked = blocked
        self.source = source

    @classmethod
    def load(cls, path: Path | None) -> "Pricing":
        """Load overrides from *path* when it exists; otherwise return the defaults."""
        if path is None or not path.is_file():
            return cls(source=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(source=path)
        if not isinstance(data, dict):
            return cls(source=path)
        return cls(data, source=path)

    def fingerprint(self) -> str:
        """A digest that changes whenever a stored cost could: the rates, the overrides, or this code."""
        digest = hashlib.sha256(Path(__file__).read_bytes())
        digest.update(json.dumps([self.table, sorted(self.blocked)], sort_keys=True, default=str).encode())
        return digest.hexdigest()

    def resolve(self, model: str | None) -> Rates | None:
        """Return the rate card whose key is the longest prefix of *model*."""
        name = normalize_model(model)
        if not name:
            return None
        best: str | None = None
        for key in (*self.table, *self.blocked):
            if name == key or (name.startswith(key) and name[len(key)] in "-:@"):
                if best is None or len(key) > len(best):
                    best = key
        if best is None or best in self.blocked:
            return None
        return self.table[best]

    def estimate(
        self,
        model: str | None,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_write_1h_tokens: int | None = None,
        speed: str | None = None,
        inference_geo: str | None = None,
        web_search_requests: int = 0,
    ) -> float | None:
        """Estimate the list-price cost in USD, or None when *model* has no rate card.

        When the 5-minute / 1-hour cache-write split is unknown, every cache
        write is priced at the 5-minute rate, which is the API default TTL.
        """
        rates = self.resolve(model)
        if rates is None:
            return None
        if speed == "fast" and isinstance(rates.get("fast"), dict):
            rates = rates["fast"]
        prompt_length = prompt_tokens + cache_read_tokens + cache_write_tokens
        if prompt_length > LONG_PROMPT_THRESHOLD and isinstance(rates.get("over_100k"), dict):
            rates = rates["over_100k"]

        write_1h = min(max(cache_write_1h_tokens or 0, 0), cache_write_tokens)
        write_5m = cache_write_tokens - write_1h
        tokens_usd = (
            prompt_tokens * _rate(rates, "input")
            + completion_tokens * _rate(rates, "output")
            + cache_read_tokens * _rate(rates, "cache_read", rates.get("input", 0) * 0.1)
            + write_5m * _rate(rates, "cache_write_5m", rates.get("input", 0) * 1.25)
            + write_1h * _rate(rates, "cache_write_1h", rates.get("input", 0) * 2)
        ) / 1_000_000
        if inference_geo == "us":
            tokens_usd *= US_INFERENCE_MULTIPLIER
        return tokens_usd + web_search_requests * WEB_SEARCH_USD_PER_REQUEST

    def price_event(self, event: UsageEvent) -> UsageEvent:
        """Return *event* with `cost_usd` and `metadata.cost_source` filled in.

        A cost the source tool reported itself (`metadata.reported_cost_usd`,
        from Cline or Hermes) wins over the table estimate.
        """
        metadata = _metadata(event)
        reported = metadata.get("reported_cost_usd")
        if isinstance(reported, (int, float)) and reported > 0:
            cost, source = float(reported), "reported"
        else:
            cost = self.estimate(
                event.model,
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
                cache_read_tokens=event.cache_read_tokens,
                cache_write_tokens=event.cache_write_tokens,
                cache_write_1h_tokens=_int(metadata.get("cache_write_1h_tokens")),
                speed=metadata.get("speed"),
                inference_geo=metadata.get("inference_geo"),
                web_search_requests=_int(metadata.get("web_search_requests")) or 0,
            )
            source = "table" if cost is not None else None
        if metadata.get("cost_source") != source:
            if source is None:
                metadata.pop("cost_source", None)
            else:
                metadata["cost_source"] = source
        return replace(event, cost_usd=cost, metadata_json=json.dumps(metadata, separators=(",", ":")))

    def price_events(self, events):
        for event in events:
            yield self.price_event(event)


def _rate(rates: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    value = rates.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _metadata(event: UsageEvent) -> dict[str, Any]:
    try:
        data = json.loads(event.metadata_json or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
