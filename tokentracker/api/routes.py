from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

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
    tz: str | None = Query(None, max_length=64),
) -> dict[str, str | None]:
    return {
        "window": window,
        "model": model,
        "project": project,
        "thread_id": thread_id,
        "provider": provider,
        "host": host,
        "tz": tz,
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
MAX_INGEST_BYTES = 8 * 1024 * 1024  # a 500-event batch is about 0.3 MB


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
async def ingest(request: Request) -> dict[str, int]:
    """Accept usage events from another machine (see `tracker link`).

    This path is public when exposed through Tailscale Funnel, so the token is
    checked before the body is read or validated: an unauthenticated caller
    learns nothing about the request format.
    """
    token = load_ingest_token(get_settings())
    if token is None:
        raise HTTPException(status_code=404, detail="Not Found")
    if not authorized(request.headers.get("authorization"), token):
        raise HTTPException(status_code=401, detail="bad or missing bearer token")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_INGEST_BYTES):
        raise HTTPException(status_code=413, detail="batch too large")
    body = await request.body()
    if len(body) > MAX_INGEST_BYTES:
        raise HTTPException(status_code=413, detail="batch too large")
    try:
        batch = IngestBatch.model_validate_json(body)
    except ValidationError as error:
        raise HTTPException(status_code=422, detail=error.errors(include_url=False, include_input=False)) from None
    database = get_database()
    accepted = await run_in_threadpool(database.ingest_events, batch.host, batch.events)
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