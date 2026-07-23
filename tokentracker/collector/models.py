from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


class UsageEvent(BaseModel):
    source_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    thread_id: str | None = None
    conversation_id: str | None = None
    project: str | None = None
    provider: str = "claude"
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int | None = None
    cost_usd: float = 0.0
    metadata_json: str = "{}"

    def normalized(self) -> "UsageEvent":
        total = self.total_tokens or sum(getattr(self, field) for field in TOKEN_FIELDS)
        return self.model_copy(update={"total_tokens": total})
