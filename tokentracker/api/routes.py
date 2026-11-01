from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from tokentracker.collector.config import Settings, get_settings
from tokentracker.collector.models import UsageEvent
from tokentracker.collector.database import UsageDatabase
from tokentracker.collector.pricing import (
    LONG_PROMPT_THRESHOLD,
    PRICING_SOURCE_DATE,
    US_INFERENCE_MULTIPLIER,
    WEB_SEARCH_USD_PER_REQUEST,
    Pricing,
)

__all__ = ["router"]

router = APIRouter()


def get_database() -> UsageDatabase:
    settings = get_settings()
    return UsageDatabase(settings.db_path, hostname=settings.hostname)


def _filters(
    window: str | None = Query(None),
    model: str | None = Query(None),
    project: str | None = Query(None),
    thread_id: str | None = Query(None),
    provider: str | None = Query(None),
    host: str | None = Query(None),
) -> dict[str, str | None]:
    return {
        "window": window,
        "model": model,
        "project": project,
        "thread_id": thread_id,
        "provider": provider,
        "host": host,
    }


# Endpoints to auto-register (route name = database method name)
_ROUTE_METHODS = {
    "stats",
    "daily",
    "series",
    "models",
    "projects",
    "threads",
    "timeline",
}


def _make_route(method: str):
    """Factory that generates a FastAPI route for *method*."""

    @router.get(f"/{method}")
    def _route(
        criteria: dict = Depends(_filters),
        database: UsageDatabase = Depends(get_database),
    ) -> Any:
        return getattr(database, method)(**criteria)

    return _route


# Auto-register all CRUD routes
for _method in _ROUTE_METHODS:
    _make_route(_method)


# ── special endpoints (don't follow the pattern) ────────────────────────


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/hosts")
def hosts(database: UsageDatabase = Depends(get_database)) -> list[dict]:
    return database.hosts()


# ── ingest: events pushed from another machine ──────────────────────────

MAX_INGEST_BATCH = 1000


class IngestBatch(BaseModel):
    host: str = Field(min_length=1, max_length=120)
    events: list[UsageEvent] = Field(max_length=MAX_INGEST_BATCH)


def load_ingest_token(settings: Settings) -> str | None:
    """The shared secret remote trackers must present; None means ingest is off."""
    token = os.environ.get("TOKEN_TRACKER_INGEST_TOKEN")
    if token:
        return token.strip() or None
    path = settings.ingest_token_file
    if path is None or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def authorized(authorization: str | None, token: str) -> bool:
    if not authorization:
        return False
    scheme, _, presented = authorization.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(presented.strip(), token)


@router.post("/events")
def ingest(
    batch: IngestBatch,
    authorization: str | None = Header(default=None),
    database: UsageDatabase = Depends(get_database),
) -> dict[str, int]:
    """Accept usage events from another machine (see `tracker link`)."""
    token = load_ingest_token(get_settings())
    if token is None:
        raise HTTPException(status_code=404, detail="ingest is not enabled on this tracker")
    if not authorized(authorization, token):
        raise HTTPException(status_code=401, detail="bad or missing bearer token")
    accepted = database.ingest_events(batch.host, batch.events)
    return {"accepted": accepted, "received": len(batch.events)}


@router.get("/pricing")
def pricing() -> dict:
    """The rate table behind `cost_usd`, for the dashboard's cost explainer."""
    values = get_settings()
    table = Pricing.load(values.pricing_file)
    override = values.pricing_file
    return {
        "source_date": PRICING_SOURCE_DATE,
        "source_url": "https://platform.claude.com/docs/en/about-claude/pricing",
        "override_file": str(override) if override else None,
        "override_active": bool(override and override.is_file()),
        "long_prompt_threshold": LONG_PROMPT_THRESHOLD,
        "us_inference_multiplier": US_INFERENCE_MULTIPLIER,
        "web_search_usd_per_request": WEB_SEARCH_USD_PER_REQUEST,
        "rates": {model: table.table[model] for model in sorted(table.table)},
        "unpriced_models": get_database().unpriced_models(),
    }


@router.get("/settings")
def settings() -> dict:
    database = get_database()
    values = get_settings()
    return database.settings() | {
        "claude_dir": str(values.claude_dir),
        "host": values.host,
        "port": values.port,
    }