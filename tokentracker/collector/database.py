from __future__ import annotations

import json
import socket
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tokentracker.collector.models import UsageEvent

if TYPE_CHECKING:
    from tokentracker.collector.pricing import Pricing


SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL UNIQUE,
    timestamp TEXT NOT NULL,
    thread_id TEXT,
    conversation_id TEXT,
    project TEXT,
    provider TEXT NOT NULL,
    host TEXT,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    cost_usd REAL,
    synced INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_usage_events_timestamp ON usage_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_events_project ON usage_events(project);
CREATE INDEX IF NOT EXISTS idx_usage_events_model ON usage_events(model);
CREATE INDEX IF NOT EXISTS idx_usage_events_thread ON usage_events(thread_id);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# One-time migration marker: Claude rows ingested before parse_claude_jsonl
# deduplicated responses hold one row per content block of each response.
CLAUDE_DEDUPE_MARKER = "claude_response_dedupe_v1"
# Lines of one response are usually written within milliseconds, but a long
# generation can spread them over several minutes (472 s observed). This is the
# widest gap still treated as the same response when the transcript is gone.
CLAUDE_DEDUPE_WINDOW_SECONDS = 1800

INSERT_COLUMNS = (
    "source_id",
    "timestamp",
    "thread_id",
    "conversation_id",
    "project",
    "provider",
    "host",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
    "latency_ms",
    "cost_usd",
    "metadata_json",
)


class UsageDatabase:
    def __init__(self, path: Path, hostname: str | None = None):
        self.path = path
        self.hostname = hostname or socket.gethostname()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, commit on success, and always close it.

        A sqlite3 connection's own ``with`` block only commits; the connection
        stays open until the cyclic garbage collector finds it.
        """
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def init(self) -> None:
        with self.connect() as connection:
            try:
                # Persistent: readers and the collector's writes no longer block each other.
                connection.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass  # another process holds a lock; the next start tries again
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(usage_events)")}
            if "cost_usd" not in columns:
                # Databases created before cost estimates existed.
                connection.execute("ALTER TABLE usage_events ADD COLUMN cost_usd REAL")
            if "host" not in columns:
                connection.execute("ALTER TABLE usage_events ADD COLUMN host TEXT")
            if "synced" not in columns:
                connection.execute("ALTER TABLE usage_events ADD COLUMN synced INTEGER NOT NULL DEFAULT 0")
            # Covers hosts(), so it reads only the index; also serves host = ? filters.
            connection.execute("DROP INDEX IF EXISTS idx_usage_events_host")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_events_host_totals ON usage_events(host, timestamp, total_tokens)"
            )
            connection.execute("UPDATE usage_events SET host = ? WHERE host IS NULL", (self.hostname,))
        self._migrate_claude_duplicates()

    def _migrate_claude_duplicates(self) -> None:
        """Collapse legacy per-content-block Claude rows into one row per response, once.

        A copy of the database is written next to it first. Transcripts that
        still exist are re-parsed, so exactly the rows the current parser would
        produce survive. Claude Code deletes transcripts after 30 days, so rows
        from missing files are deduplicated in place by a heuristic: within one
        transcript, consecutive rows with identical model and token counts
        written within `CLAUDE_DEDUPE_WINDOW_SECONDS` are one response.
        """
        with self.connect() as connection:
            if connection.execute("SELECT 1 FROM meta WHERE key = ?", (CLAUDE_DEDUPE_MARKER,)).fetchone():
                return
            has_rows = connection.execute("SELECT 1 FROM usage_events WHERE provider = 'claude' LIMIT 1").fetchone()
        result: dict[str, Any] = {"removed": 0, "backup": None, "at": datetime.now(timezone.utc).isoformat()}
        if has_rows:
            backup = self.path.with_name(f"{self.path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
            with self.connect() as source, sqlite3.connect(backup) as target:
                source.backup(target)
            result["backup"] = str(backup)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM meta WHERE key = ?", (CLAUDE_DEDUPE_MARKER,)).fetchone():
                return  # another process finished first
            if has_rows:
                result["removed"] = dedupe_claude_rows(connection)
            connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (CLAUDE_DEDUPE_MARKER, json.dumps(result))
            )

    def migration_info(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = ?", (CLAUDE_DEDUPE_MARKER,)).fetchone()
        return json.loads(row["value"]) if row else None

    def insert_events(self, events: Iterable[UsageEvent]) -> int:
        """Insert new events; rows whose ``source_id`` already exists are left alone."""
        return self._write(_INSERT_SQL, events)

    def upsert_events(self, events: Iterable[UsageEvent]) -> int:
        """Insert new events and refresh existing rows whose counts changed.

        Use this for sources that report one growing aggregate per
        conversation, such as Hermes sessions.
        """
        return self._write(_UPSERT_SQL, events)

    def ingest_events(self, host: str, events: Iterable[UsageEvent]) -> int:
        """Store events pushed from another machine.

        Their `source_id` is namespaced with the sending host so two machines
        can never collide, and they are written with an upsert so a resend
        (or a grown Hermes session) refreshes the row.
        """
        host = host.strip()
        if not host:
            return 0
        stamped = (
            event.model_copy(update={"host": host, "source_id": f"{host}::{event.source_id}"})
            for event in events
            if not event.source_id.startswith(f"{host}::")
        )
        return self._write(_UPSERT_SQL, stamped)

    def hosts(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT COALESCE(host, '') AS host, COUNT(*) AS events,
                       SUM(total_tokens) AS total_tokens, MAX(timestamp) AS last_timestamp
                FROM usage_events GROUP BY host ORDER BY total_tokens DESC
                """
            ).fetchall()
        return _dicts(rows)

    def pending_forward(self, limit: int = 500) -> list[tuple[int, UsageEvent]]:
        """Local events not yet pushed to the remote tracker, oldest first."""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT id, {', '.join(INSERT_COLUMNS)} FROM usage_events "
                "WHERE synced = 0 AND host = ? ORDER BY id LIMIT ?",
                (self.hostname, limit),
            ).fetchall()
        return [(row["id"], _event_from_row(row)) for row in rows]

    def pending_forward_count(self) -> int:
        with self.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM usage_events WHERE synced = 0 AND host = ?", (self.hostname,)
            ).fetchone()[0]

    def mark_forwarded(self, ids: Iterable[int]) -> None:
        ids = list(ids)
        if not ids:
            return
        with self.connect() as connection:
            connection.executemany("UPDATE usage_events SET synced = 1 WHERE id = ?", [(i,) for i in ids])

    def set_meta(self, key: str, value: Any) -> None:
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    def get_meta(self, key: str) -> Any:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def _write(self, sql: str, events: Iterable[UsageEvent]) -> int:
        rows = [_event_row(event) for event in events]
        if not rows:
            return 0
        with self.connect() as connection:
            before = connection.total_changes
            connection.executemany(sql, rows)
            return connection.total_changes - before

    def reprice(self, pricing: "Pricing") -> int:
        """Recompute `cost_usd` for every stored event. Returns the number of rows changed."""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT id, {', '.join(INSERT_COLUMNS)} FROM usage_events"
            ).fetchall()
            updates = []
            for row in rows:
                event = _event_from_row(row)
                priced = pricing.price_event(event)
                if priced.cost_usd != event.cost_usd or priced.metadata_json != event.metadata_json:
                    updates.append((priced.cost_usd, priced.metadata_json, row["id"]))
            if updates:
                connection.executemany(
                    "UPDATE usage_events SET cost_usd = ?, metadata_json = ? WHERE id = ?", updates
                )
            return len(updates)

    def unpriced_models(self) -> list[dict[str, Any]]:
        """Models with events that have no cost estimate, largest token volume first."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT COALESCE(model, 'unknown') AS model, COUNT(*) AS events,
                       SUM(total_tokens) AS total_tokens, MAX(timestamp) AS last_timestamp
                FROM usage_events
                WHERE cost_usd IS NULL
                GROUP BY model
                ORDER BY total_tokens DESC
                """
            ).fetchall()
        return _dicts(rows)

    def latest_by_provider(self) -> dict[str, dict[str, Any]]:
        """Return event count and newest timestamp per provider."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT provider, COUNT(*) AS events, MAX(timestamp) AS last_timestamp
                FROM usage_events
                GROUP BY provider
                """
            ).fetchall()
        return {row["provider"]: _dict(row) for row in rows}

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
                    COALESCE(SUM(cost_usd), 0) AS cost_usd,
                    COUNT(cost_usd) AS priced_requests,
                    COUNT(*) AS requests,
                    COUNT(DISTINCT thread_id) AS threads,
                    COUNT(DISTINCT project) AS projects,
                    COUNT(DISTINCT model) AS models
                FROM usage_events {where}
                """,
                values,
            ).fetchone()
        return _dict(row)

    def daily(self, **filters: Any) -> list[dict[str, Any]]:
        return self._grouped("date(timestamp)", "day", filters)

    def series(self, **filters: Any) -> dict[str, Any]:
        """Token totals over time, bucketed to suit the window, in the viewer's timezone.

        Today and yesterday get 24 hourly buckets; 7 and 30 days get daily
        buckets; 90 days gets weeks starting on Monday; all time gets months.
        Every bucket in the range is returned, empty ones with zeros, so the
        chart keeps a stable axis.

        SQLite only knows UTC, so rows are first summed into 15-minute UTC
        slots (fine enough for every real timezone offset) and then mapped to
        local buckets with zoneinfo, which also handles daylight-saving changes.
        The slots are cut from the text with substr, which is safe because
        `_event_row` always stores UTC ``isoformat()`` timestamps.
        """
        window = filters.get("window")
        tz = resolve_tz(filters.get("tz"))
        unit = bucket_unit(window)
        where, values = self._filters(filters)
        with self.connect() as connection:
            slots = connection.execute(
                f"""
                SELECT substr(timestamp, 1, 14)
                         || printf('%02d', (CAST(substr(timestamp, 15, 2) AS INTEGER) / 15) * 15) AS slot,
                       SUM(total_tokens) AS total_tokens,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(cost_usd) AS cost_usd,
                       COUNT(cost_usd) AS priced_requests,
                       COUNT(*) AS requests
                FROM usage_events {where}
                GROUP BY slot
                """,
                values,
            ).fetchall()

        buckets: dict[str, dict[str, Any]] = {}
        first_local: datetime | None = None
        for slot in slots:
            if slot["slot"] is None:
                continue
            local = datetime.fromisoformat(slot["slot"] + "+00:00").astimezone(tz)
            first_local = local if first_local is None or local < first_local else first_local
            bucket = buckets.setdefault(_bucket_key(local, unit), _empty_bucket())
            for column in ("total_tokens", "prompt_tokens", "completion_tokens", "priced_requests", "requests"):
                bucket[column] += slot[column] or 0
            if slot["cost_usd"] is not None:
                bucket["cost_usd"] = (bucket["cost_usd"] or 0.0) + slot["cost_usd"]

        since, until = _window_bounds(window, tz)
        if since is None:
            if first_local is None:
                return {"unit": unit, "tz": _tz_name(tz), "rows": []}
            since = first_local
        end = until - timedelta(microseconds=1) if until is not None else datetime.now(tz)
        if unit == "hour":
            # Whole local day, so today's chart shows the hours still to come.
            start_day = since.astimezone(tz).date()
            end = _local_midnight(start_day + timedelta(days=1), tz) - timedelta(microseconds=1)
        keys = _bucket_range(since.astimezone(tz), end.astimezone(tz), unit, tz)
        for key in buckets:  # never drop data that falls outside the generated range
            if key not in keys:
                keys.append(key)
        keys.sort()
        rows = [{"period": key, **buckets.get(key, _empty_bucket())} for key in keys]
        return {"unit": unit, "tz": _tz_name(tz), "rows": rows}

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
                    SUM(cost_usd) AS cost_usd,
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
                SELECT source_id, timestamp, project, provider, host, model, total_tokens, cost_usd, thread_id
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
        # GROUP BY 1 groups by the shown label, so NULL and a literal 'unknown'
        # share one row. GROUP BY model would also let SQLite walk the whole
        # model index in order and skip the timestamp index of the window.
        where, values = self._filters(filters)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    {expression} AS {label},
                    SUM(total_tokens) AS total_tokens,
                    SUM(prompt_tokens) AS prompt_tokens,
                    SUM(completion_tokens) AS completion_tokens,
                    SUM(cost_usd) AS cost_usd,
                    COUNT(cost_usd) AS priced_requests,
                    COUNT(*) AS requests,
                    COUNT(DISTINCT thread_id) AS threads,
                    AVG(latency_ms) AS average_latency_ms
                FROM usage_events {where}
                GROUP BY 1
                ORDER BY total_tokens DESC
                LIMIT 200
                """,
                values,
            ).fetchall()
        return _dicts(rows)

    def _filters(self, filters: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
        clauses: list[str] = []
        values: list[Any] = []
        since, until = _window_bounds(filters.get("window"), resolve_tz(filters.get("tz")))
        if since is not None:
            clauses.append("timestamp >= ?")
            values.append(since.isoformat())
        if until is not None:
            clauses.append("timestamp < ?")
            values.append(until.isoformat())
        for key in ("project", "model", "thread_id", "provider", "host"):
            value = filters.get(key)
            if value:
                clauses.append(f"{key} = ?")
                values.append(value)
        if not clauses:
            return "", tuple()
        return "WHERE " + " AND ".join(clauses), tuple(values)


_INSERT_SQL = f"""
INSERT OR IGNORE INTO usage_events ({", ".join(INSERT_COLUMNS)})
VALUES ({", ".join("?" for _ in INSERT_COLUMNS)})
"""

_UPDATE_COLUMNS = tuple(column for column in INSERT_COLUMNS if column != "source_id")

# Only rewrite a row when its counts or metadata moved, so `total_changes`
# reports real updates and unchanged rows cost nothing.
_UPSERT_SQL = f"""
INSERT INTO usage_events ({", ".join(INSERT_COLUMNS)})
VALUES ({", ".join("?" for _ in INSERT_COLUMNS)})
ON CONFLICT(source_id) DO UPDATE SET
    {", ".join(f"{column} = excluded.{column}" for column in _UPDATE_COLUMNS)},
    synced = 0
WHERE excluded.total_tokens != usage_events.total_tokens
   OR excluded.metadata_json != usage_events.metadata_json
   OR excluded.cost_usd IS NOT usage_events.cost_usd
"""


def _event_row(event: UsageEvent) -> tuple[Any, ...]:
    event = event.normalized()
    values = event.model_dump()
    if not values.get("host"):
        values["host"] = None  # filled by the caller; NULL rows are stamped with the local hostname on init
    values["timestamp"] = event.timestamp.astimezone(timezone.utc).isoformat()
    return tuple(values[column] for column in INSERT_COLUMNS)


def dedupe_claude_rows(connection: sqlite3.Connection) -> int:
    """Delete legacy duplicate Claude rows; return how many were removed.

    Rows whose transcript still exists are matched against a fresh parse of
    that transcript (exact). Rows whose transcript is gone are collapsed by
    `_heuristic_duplicates`. Also sets `thread_id` to the session id, which
    older rows filled with the per-line `uuid`.
    """
    from tokentracker.collector.parser import parse_claude_jsonl  # local import: parser is a leaf module

    rows = connection.execute(
        """
        SELECT id, source_id, timestamp, model, prompt_tokens, completion_tokens,
               cache_read_tokens, cache_write_tokens
        FROM usage_events WHERE provider = 'claude'
        """
    ).fetchall()
    by_file: dict[str, list[tuple[int, sqlite3.Row]]] = defaultdict(list)
    for row in rows:
        file, _, line = row["source_id"].rpartition(":")
        if line.isdigit():
            by_file[file].append((int(line), row))

    to_delete: list[tuple[int]] = []
    for file, items in by_file.items():
        path = Path(file)
        if path.is_file():
            keep = {event.source_id for event in parse_claude_jsonl(path, path.parent)}
            to_delete.extend((row["id"],) for _, row in items if row["source_id"] not in keep)
        else:
            to_delete.extend(_heuristic_duplicates(items))
    if to_delete:
        connection.executemany("DELETE FROM usage_events WHERE id = ?", to_delete)
    connection.execute(
        """
        UPDATE usage_events SET thread_id = conversation_id
        WHERE provider = 'claude' AND conversation_id IS NOT NULL AND thread_id IS NOT conversation_id
        """
    )
    return len(to_delete)


def _heuristic_duplicates(items: list[tuple[int, sqlite3.Row]]) -> list[tuple[int]]:
    """Ids of rows that repeat the previous kept row's model and counts within the window."""
    duplicates: list[tuple[int]] = []
    kept_signature: tuple | None = None
    kept_time: datetime | None = None
    for _, row in sorted(items, key=lambda item: item[0]):
        signature = (
            row["model"], row["prompt_tokens"], row["completion_tokens"],
            row["cache_read_tokens"], row["cache_write_tokens"],
        )
        when = datetime.fromisoformat(row["timestamp"])
        if (
            kept_signature == signature
            and kept_time is not None
            and abs((when - kept_time).total_seconds()) <= CLAUDE_DEDUPE_WINDOW_SECONDS
        ):
            duplicates.append((row["id"],))
            continue
        kept_signature, kept_time = signature, when
    return duplicates


def _event_from_row(row: sqlite3.Row) -> UsageEvent:
    values = {column: row[column] for column in INSERT_COLUMNS}
    values["timestamp"] = datetime.fromisoformat(values["timestamp"])
    return UsageEvent(**values)


def _dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [_dict(row) for row in rows]


def bucket_unit(window: str | None) -> str:
    match window:
        case "today" | "yesterday":
            return "hour"
        case "7d" | "week" | "30d" | "month":
            return "day"
        case "90d":
            return "week"
        case _:
            return "month"


def resolve_tz(name: str | None) -> tzinfo:
    """The viewer's timezone from an IANA name (e.g. 'America/Los_Angeles').

    Falls back to this machine's local timezone when the name is missing or unknown.
    """
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def _tz_name(tz: tzinfo) -> str:
    return getattr(tz, "key", None) or str(tz)


def _local_midnight(day: date, tz: tzinfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def _window_bounds(
    window: str | None, tz: tzinfo | None = None, now: datetime | None = None
) -> tuple[datetime | None, datetime | None]:
    """UTC bounds of a window measured in whole local calendar days.

    "7d" means today plus the six days before it, so the chart shows exactly
    seven daily bars and the totals match the bars.
    """
    tz = tz or resolve_tz(None)
    today = (now or datetime.now(timezone.utc)).astimezone(tz).date()

    def start(days_back: int) -> datetime:
        return _local_midnight(today - timedelta(days=days_back), tz).astimezone(timezone.utc)

    match window:
        case "today":
            return start(0), None
        case "yesterday":
            return start(1), start(0)
        case "7d" | "week":
            return start(6), None
        case "30d" | "month":
            return start(29), None
        case "90d":
            return start(89), None
        case _:
            return None, None


def _bucket_key(local: datetime, unit: str) -> str:
    match unit:
        case "hour":
            return local.strftime("%Y-%m-%dT%H:00")
        case "day":
            return local.date().isoformat()
        case "week":
            return (local.date() - timedelta(days=local.weekday())).isoformat()
        case _:
            return local.strftime("%Y-%m-01")


def _bucket_range(start: datetime, end: datetime, unit: str, tz: tzinfo) -> list[str]:
    """Every bucket key from *start* to *end* (local datetimes), in order, without duplicates."""
    keys: list[str] = []
    if unit == "hour":
        # Align to the local hour (UTC alignment would be wrong for +05:30 zones),
        # then step in UTC so a repeated or skipped hour at a DST change is handled.
        cursor = start.astimezone(tz).replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        stop = end.astimezone(timezone.utc)
        while cursor <= stop:
            key = _bucket_key(cursor.astimezone(tz), unit)
            if not keys or keys[-1] != key:
                keys.append(key)
            cursor += timedelta(hours=1)
        return keys
    day, last = start.date(), end.date()
    if unit == "week":
        day -= timedelta(days=day.weekday())
    elif unit == "month":
        day = day.replace(day=1)
    while day <= last:
        keys.append(day.isoformat())
        if unit == "day":
            day += timedelta(days=1)
        elif unit == "week":
            day += timedelta(days=7)
        else:
            day = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return keys


def _empty_bucket() -> dict[str, Any]:
    return {
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": None,
        "priced_requests": 0,
        "requests": 0,
    }
