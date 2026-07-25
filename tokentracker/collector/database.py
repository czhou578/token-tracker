from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tokentracker.collector.models import UsageEvent


SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL UNIQUE,
    timestamp TEXT NOT NULL,
    thread_id TEXT,
    conversation_id TEXT,
    project TEXT,
    provider TEXT NOT NULL,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_usage_events_timestamp ON usage_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_events_project ON usage_events(project);
CREATE INDEX IF NOT EXISTS idx_usage_events_model ON usage_events(model);
CREATE INDEX IF NOT EXISTS idx_usage_events_thread ON usage_events(thread_id);
"""

INSERT_COLUMNS = (
    "source_id",
    "timestamp",
    "thread_id",
    "conversation_id",
    "project",
    "provider",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
    "latency_ms",
    "metadata_json",
)


class UsageDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)

    def _migrate(self, connection: sqlite3.Connection) -> None:
        # Drop the old cost_usd column from existing databases.
        columns = connection.execute("PRAGMA table_info(usage_events)").fetchall()
        if any(row["name"] == "cost_usd" for row in columns):
            connection.execute("ALTER TABLE usage_events DROP COLUMN cost_usd")

    def insert_events(self, events: Iterable[UsageEvent]) -> int:
        rows = [_event_row(event) for event in events]
        if not rows:
            return 0
        with self.connect() as connection:
            before = connection.total_changes
            connection.executemany(
                _insert_sql(),
                rows,
            )
            return connection.total_changes - before

    def stats(self, **filters: Any) -> dict[str, Any]:
        where, values = self._filters(filters)
        with self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                    COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                    COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                    COUNT(*) AS requests,
                    COUNT(DISTINCT thread_id) AS threads,
                    COUNT(DISTINCT project) AS projects,
                    COUNT(DISTINCT model) AS models
                FROM usage_events {where}
                """,
                values,
            ).fetchone()
        result = _dict(row)
        result["api_equivalent_tokens"] = result["total_tokens"]
        return result

    def daily(self, **filters: Any) -> list[dict[str, Any]]:
        return self._grouped("date(timestamp)", "day", filters)

    def models(self, **filters: Any) -> list[dict[str, Any]]:
        return self._grouped("COALESCE(model, 'unknown')", "model", filters)

    def projects(self, **filters: Any) -> list[dict[str, Any]]:
        return self._grouped("COALESCE(project, 'unknown')", "project", filters)

    def threads(self, **filters: Any) -> list[dict[str, Any]]:
        where, values = self._filters(filters)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    COALESCE(thread_id, conversation_id, 'unknown') AS thread,
                    MIN(timestamp) AS started_at,
                    MAX(timestamp) AS ended_at,
                    COALESCE(project, 'unknown') AS project,
                    COALESCE(model, 'unknown') AS model,
                    SUM(total_tokens) AS total_tokens,
                    COUNT(*) AS messages
                FROM usage_events {where}
                GROUP BY thread
                ORDER BY ended_at DESC
                LIMIT 100
                """,
                values,
            ).fetchall()
        return _dicts(rows)

    def timeline(self, **filters: Any) -> list[dict[str, Any]]:
        return self._grouped("strftime('%Y-%m-%d %H:00:00', timestamp)", "hour", filters)

    def recent(self, limit: int = 25, **filters: Any) -> list[dict[str, Any]]:
        where, values = self._filters(filters)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT timestamp, project, provider, model, total_tokens, thread_id
                FROM usage_events {where}
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (*values, limit),
            ).fetchall()
        return _dicts(rows)

    def settings(self) -> dict[str, Any]:
        return {"database": str(self.path), "exists": self.path.exists()}

    def _grouped(self, expression: str, label: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
        where, values = self._filters(filters)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    {expression} AS {label},
                    SUM(total_tokens) AS total_tokens,
                    SUM(prompt_tokens) AS prompt_tokens,
                    SUM(completion_tokens) AS completion_tokens,
                    COUNT(*) AS requests,
                    COUNT(DISTINCT thread_id) AS threads,
                    AVG(latency_ms) AS average_latency_ms
                FROM usage_events {where}
                GROUP BY {label}
                ORDER BY total_tokens DESC
                LIMIT 200
                """,
                values,
            ).fetchall()
        return _dicts(rows)

    def _filters(self, filters: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
        clauses: list[str] = []
        values: list[Any] = []
        since, until = _window_bounds(filters.get("window"))
        if since is not None:
            clauses.append("timestamp >= ?")
            values.append(since.isoformat())
        if until is not None:
            clauses.append("timestamp < ?")
            values.append(until.isoformat())
        for key in ("project", "model", "thread_id", "provider"):
            value = filters.get(key)
            if value:
                clauses.append(f"{key} = ?")
                values.append(value)
        if not clauses:
            return "", tuple()
        return "WHERE " + " AND ".join(clauses), tuple(values)


def _insert_sql() -> str:
    columns = ", ".join(INSERT_COLUMNS)
    placeholders = ", ".join("?" for _ in INSERT_COLUMNS)
    return f"INSERT OR IGNORE INTO usage_events ({columns}) VALUES ({placeholders})"


def _event_row(event: UsageEvent) -> tuple[Any, ...]:
    event = event.normalized()
    values = event.model_dump()
    values["timestamp"] = event.timestamp.astimezone(timezone.utc).isoformat()
    return tuple(values[column] for column in INSERT_COLUMNS)


def _dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [_dict(row) for row in rows]


def _window_bounds(window: str | None) -> tuple[datetime | None, datetime | None]:
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    match window:
        case "today":
            return today, None
        case "yesterday":
            return today - timedelta(days=1), today
        case "7d" | "week":
            return now - timedelta(days=7), None
        case "30d" | "month":
            return now - timedelta(days=30), None
        case "90d":
            return now - timedelta(days=90), None
        case _:
            return None, None
