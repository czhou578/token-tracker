from __future__ import annotations

from datetime import datetime


def compact_number(value: int | float | None) -> str:
    value = value or 0
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(int(value))


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
