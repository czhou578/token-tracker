from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from tokentracker.collector.config import get_settings
from tokentracker.collector.database import UsageDatabase

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


# Map of endpoint name → database method name
# Each entry produces a route that calls db.<method>(**filters)
_ROUTE_METHODS: dict[str, str] = {
    "stats": "stats",
    "daily": "daily",
    "models": "models",
    "projects": "projects",
    "threads": "threads",
    "timeline": "timeline",
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


@router.get("/settings")
def settings() -> dict:
    database = get_database()
    values = get_settings()
    return database.settings() | {
        "claude_dir": str(values.claude_dir),
        "host": values.host,
        "port": values.port,
    }