from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from tokentracker.collector.config import CLAUDE_PROVIDER, CLINE_PROVIDER, HERMES_PROVIDER
from tokentracker.collector.models import UsageEvent


TOKEN_KEYS = {
    "prompt_tokens": "input_tokens",
    "completion_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "cache_write_tokens": "cache_creation_input_tokens",
    "reasoning_tokens": "reasoning_tokens",
}


def parse_claude_jsonl(path: Path, root: Path | None = None) -> Iterable[UsageEvent]:
    root = root or path.parent
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            event = parse_claude_record(payload, path, line_no, root)
            if event is not None:
                yield event


def parse_claude_record(payload: dict, path: Path, line_no: int, root: Path) -> UsageEvent | None:
    message = payload.get("message")
    usage = message.get("usage") if isinstance(message, dict) else payload.get("usage")
    if not isinstance(usage, dict):
        return None

    token_counts = {field: _int(usage.get(key)) for field, key in TOKEN_KEYS.items()}
    total_tokens = sum(token_counts.values())
    if total_tokens == 0:
        return None

    timestamp = _parse_timestamp(payload.get("timestamp") or (message or {}).get("timestamp"))
    model = str(message.get("model") or payload.get("model") or "unknown")
    conversation_id = _first_string(payload, "sessionId", "session_id", "conversation_id", "conversationId")
    thread_id = _first_string(payload, "uuid", "requestId", "request_id") or conversation_id
    project = _project_from_path(path, root)
    source_id = f"{path.resolve()}:{line_no}"

    return UsageEvent(
        source_id=source_id,
        timestamp=timestamp,
        thread_id=thread_id,
        conversation_id=conversation_id,
        project=project,
        provider=CLAUDE_PROVIDER,
        model=model,
        **token_counts,
        total_tokens=total_tokens,
    )


# ── Cline ────────────────────────────────────────────────────────────────
# Cline writes its conversation history into a SQLite database. The
# `cline_messages` table stores each message as JSON in a `message` column.
# The token usage lives inside that JSON (camelCase or snake_case keys).

CLINE_TOKEN_ALIASES = {
    "prompt_tokens": ("input_tokens", "inputTokenCount", "prompt_tokens", "prompt_tokens_count"),
    "completion_tokens": ("output_tokens", "outputTokenCount", "completion_tokens", "completion_tokens_count"),
    "cache_read_tokens": ("cache_read_input_tokens", "cacheReadInputTokens", "cache_read_tokens"),
    "cache_write_tokens": ("cache_creation_input_tokens", "cacheCreationInputTokens", "cache_write_tokens"),
    "reasoning_tokens": ("reasoning_tokens", "reasoningTokens"),
}


def parse_cline_sqlite(path: Path) -> Iterable[UsageEvent]:
    """Yield UsageEvent records from Cline's SQLite message database."""
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        table_names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "cline_messages" not in table_names:
            return
        columns = [row[1] for row in connection.execute("PRAGMA table_info(cline_messages)")]
        rows = list(connection.execute("SELECT * FROM cline_messages"))
    finally:
        connection.close()

    message_col = "message" if "message" in columns else columns[1]
    id_col = "id" if "id" in columns else columns[0]
    timestamp_col = "timestamp" if "timestamp" in columns else None

    for row in rows:
        raw = row[message_col]
        payload = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(payload, dict):
            continue
        source_id = f"{path.resolve()}:{row[id_col]}"
        timestamp = row[timestamp_col] if timestamp_col else None
        event = parse_cline_record(payload, source_id, timestamp)
        if event is not None:
            yield event


def parse_cline_record(payload: dict, source_id: str, timestamp: object = None) -> UsageEvent | None:
    usage = _extract_cline_usage(payload)
    if not isinstance(usage, dict):
        return None

    token_counts = {field: _first_int(usage, aliases) for field, aliases in CLINE_TOKEN_ALIASES.items()}
    total_tokens = sum(token_counts.values())
    if total_tokens == 0:
        return None

    message = payload.get("message")
    inner = message.get("message") if isinstance(message, dict) else None
    model = (
        _first_string(payload, "model")
        or _first_string(message, "model")
        or _first_string(inner, "model")
        or "unknown"
    )
    parsed_timestamp = _parse_timestamp(
        timestamp or _first_string(payload, "timestamp") or _first_string(message, "timestamp")
    )
    thread_id = _first_string(payload, "taskId", "sessionId", "conversation_id", "conversationId")

    return UsageEvent(
        source_id=source_id,
        timestamp=parsed_timestamp,
        thread_id=thread_id,
        provider=CLINE_PROVIDER,
        model=model,
        **token_counts,
        total_tokens=total_tokens,
    )


def parse_cline_task_history(path: Path) -> Iterable[UsageEvent]:
    """Parse Cline's `taskHistory.json` — the 4.x task-level token aggregate format.

    Each record is a single task (one API conversation) with aggregate usage:

        {
          "id": "1785782429653",
          "ulid": "01KZ4ETSZ1KC1Y6V7B02RR11DT",
          "ts": 1785782943608,          # epoch milliseconds
          "task": "write a poem",
          "tokensIn": 73347,
          "tokensOut": 1881,
          "cacheWrites": 0,
          "cacheReads": 43930,
          "totalCost": 0,
          "size": 55944,
          "cwdOnTaskInitialization": "/home/.../blog",
          "isFavorited": false,
          "modelId": "deepseek-v4-flash"
        }
    """
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            records = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(records, list):
        return

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        token_counts = {
            "prompt_tokens": _int(record.get("tokensIn")),
            "completion_tokens": _int(record.get("tokensOut")),
            "cache_read_tokens": _int(record.get("cacheReads")),
            "cache_write_tokens": _int(record.get("cacheWrites")),
        }
        total_tokens = sum(token_counts.values())
        if total_tokens == 0:
            continue

        ts = record.get("ts")
        if isinstance(ts, (int, float)):
            try:
                timestamp = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                timestamp = datetime.now(timezone.utc)
        else:
            timestamp = _parse_timestamp(ts)

        project = _project_from_cwd(record.get("cwdOnTaskInitialization"))
        thread_id = _first_string(record, "ulid", "id")
        model = _first_string(record, "modelId", "model") or "unknown"
        source_id = f"{path.resolve()}:{record.get('id') or index}"

        yield UsageEvent(
            source_id=source_id,
            timestamp=timestamp,
            thread_id=thread_id,
            project=project,
            provider=CLINE_PROVIDER,
            model=model,
            **token_counts,
            total_tokens=total_tokens,
        )


def parse_hermes_state_db(path: Path) -> Iterable[UsageEvent]:
    """Parse Hermes token usage from `~/.hermes/state.db` (SQLite).

    Each row in the `sessions` table is one finished conversation with its
    aggregate token usage — the analog to Cline's `taskHistory.json`.
    In-progress sessions (ended_at IS NULL) are skipped because their counts
    are still changing and the usage database dedupes by `source_id`.
    """
    if not path.is_file():
        return
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        rows = list(
            connection.execute(
                "SELECT id, source, model, title, started_at, ended_at, cwd, git_repo_root, "
                "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens "
                "FROM sessions WHERE ended_at IS NOT NULL"
            )
        )
    except sqlite3.Error:
        return
    finally:
        connection.close()

    for row in rows:
        event = _hermes_record(row)
        if event is not None:
            yield event


def _hermes_record(row: sqlite3.Row) -> UsageEvent | None:
    token_counts = {
        "prompt_tokens": _int(row["input_tokens"]),
        "completion_tokens": _int(row["output_tokens"]),
        "cache_read_tokens": _int(row["cache_read_tokens"]),
        "cache_write_tokens": _int(row["cache_write_tokens"]),
        "reasoning_tokens": _int(row["reasoning_tokens"]),
    }
    total_tokens = sum(token_counts.values())
    if total_tokens == 0:
        return None

    started = row["started_at"]
    if isinstance(started, (int, float)) and started:
        try:
            timestamp = datetime.fromtimestamp(started, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            timestamp = datetime.now(timezone.utc)
    else:
        timestamp = _parse_timestamp(started)

    thread_id = str(row["id"])
    model = str(row["model"] or "unknown")
    project = _project_from_cwd(row["git_repo_root"] or row["cwd"])

    metadata = {
        "title": row["title"],
        "source": row["source"],
        "cwd": row["cwd"],
        "git_repo_root": row["git_repo_root"],
        "ended_at": row["ended_at"],
    }

    return UsageEvent(
        source_id=f"hermes:{thread_id}",
        timestamp=timestamp,
        thread_id=thread_id,
        conversation_id=thread_id,
        project=project,
        provider=HERMES_PROVIDER,
        model=model,
        **token_counts,
        total_tokens=total_tokens,
        metadata_json=json.dumps(metadata),
    )


def _extract_cline_usage(payload: dict) -> object:
    """Locate the `usage` dict inside Cline's message JSON (handles nesting)."""
    message = payload.get("message")
    if isinstance(message, dict):
        # Cline sometimes wraps the message: { "message": { "message": { "usage": ... } } }
        usage = message.get("usage") or (message.get("message") or {}).get("usage")
        if isinstance(usage, dict):
            return usage
    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage
    return None


def _first_int(container: dict, keys: tuple[str, ...]) -> int:
    for key in keys:
        value = container.get(key)
        parsed = _int(value)
        if parsed:
            return parsed
    return 0


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _first_string(payload: dict | None, *keys: str) -> str | None:
    if payload is None:
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, str) and value:
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).astimezone(timezone.utc)
        except ValueError:
            pass
    if isinstance(value, (int, float)) and value:
        try:
            # Cline stores `ts` as epoch milliseconds
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now(timezone.utc)


def _project_from_path(path: Path, root: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return path.stem
    return relative.parts[0] if relative.parts else path.stem


def _project_from_cwd(cwd: object) -> str | None:
    """Derive a project label from a Cline `cwdOnTaskInitialization` path."""
    if not isinstance(cwd, str) or not cwd:
        return None
    parts = Path(cwd).expanduser().parts
    if not parts:
        return None
    # Prefer the last meaningful component (the project folder name).
    return parts[-1]
