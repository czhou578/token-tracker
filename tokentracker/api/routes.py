from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from tokentracker.collector.config import get_settings
from tokentracker.collector.database import UsageDatabase

router = APIRouter()


def get_database() -> UsageDatabase:
    return UsageDatabase(get_settings().db_path)


def filters(
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


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/stats")
def stats(criteria: dict = Depends(filters), database: UsageDatabase = Depends(get_database)) -> dict:
    result = database.stats(**criteria)
    result["recent"] = database.recent(**criteria)
    return result


@router.get("/daily")
def daily(criteria: dict = Depends(filters), database: UsageDatabase = Depends(get_database)) -> list[dict]:
    return database.daily(**criteria)


@router.get("/models")
def models(criteria: dict = Depends(filters), database: UsageDatabase = Depends(get_database)) -> list[dict]:
    return database.models(**criteria)


@router.get("/projects")
def projects(criteria: dict = Depends(filters), database: UsageDatabase = Depends(get_database)) -> list[dict]:
    return database.projects(**criteria)


@router.get("/threads")
def threads(criteria: dict = Depends(filters), database: UsageDatabase = Depends(get_database)) -> list[dict]:
    return database.threads(**criteria)


@router.get("/timeline")
def timeline(criteria: dict = Depends(filters), database: UsageDatabase = Depends(get_database)) -> list[dict]:
    return database.timeline(**criteria)


@router.get("/settings")
def settings(database: UsageDatabase = Depends(get_database)) -> dict:
    values = get_settings()
    return database.settings() | {
        "claude_dir": str(values.claude_dir),
        "host": values.host,
        "port": values.port,
    }
