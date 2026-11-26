from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone


TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


# A plain dataclass, not a pydantic model: the collector never imports pydantic,
# which saves about 16 MB per process. The API still validates incoming events,
# because pydantic validates stdlib dataclasses used as field types.
@dataclass(slots=True, kw_only=True)
class UsageEvent:
    source_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    thread_id: str | None = None
    conversation_id: str | None = None
    project: str | None = None
    provider: str = "claude"
    host: str | None = None
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int | None = None
    cost_usd: float | None = None
    metadata_json: str = "{}"

    def normalized(self) -> "UsageEvent":
        if self.total_tokens:
            return self
        return replace(self, total_tokens=sum(getattr(self, name) for name in TOKEN_FIELDS))


ACTIVITY_KINDS = ("reply", "tool_call", "scheduled_run")


@dataclass(slots=True, kw_only=True)
class ActivityEvent:
    """One action that the dashboard counts and that is not a model request.

    `kind` is one of `ACTIVITY_KINDS`: a final answer of the assistant, a tool
    call of a model, or a run that a schedule started. `name` is the tool name
    of a tool call. `source_id` is the id the source gives the action (the
    tool call id, the message id), so a second read adds nothing.
    """

    source_id: str
    kind: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    name: str | None = None
    thread_id: str | None = None
    project: str | None = None
    provider: str = "claude"
    host: str | None = None
    model: str | None = None
