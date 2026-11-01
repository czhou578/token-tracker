from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from tokentracker.collector.config import get_settings
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
    return UsageDatabase(get_settings().db_path)


def _filters(
    window: str | None = Query(None),
    model: str | None = Query(None),
    project: str | None = Query(None),
    thread_id: str | None = Query(None),
    provider: str | None = Query(None),
) -> dict[str, str | None]:
    return {
        "window": window,
        "model": model,
        "project": project,
        "thread_id": thread_id,
        "provider": provider,
    }


# Endpoints to auto-register (route name = database method name)
_ROUTE_METHODS = {
    "stats",
    "daily",
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