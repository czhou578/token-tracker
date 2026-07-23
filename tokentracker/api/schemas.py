from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


Window = Literal["today", "yesterday", "7d", "30d", "90d", "week", "month"]


class Filters(BaseModel):
    window: Window | None = None
    model: str | None = None
    project: str | None = None
    thread_id: str | None = None
    provider: str | None = None
