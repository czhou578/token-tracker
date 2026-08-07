from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tokentracker.collector.parser import (
    parse_claude_record,
    parse_cline_record,
    parse_cline_sqlite,
    parse_cline_task_history,
)


def test_parse_claude_record_extracts_usage() -> None:
    payload = {
        "timestamp": "2026-07-23T10:00:00Z",
        "sessionId": "session-1",
        "uuid": "message-1",
        "message": {
            "model": "claude-3-5-sonnet",
            "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 7},
        },
    }

    event = parse_claude_record(payload, Path("chat.jsonl"), 3, Path("."))

    assert event is not None
    assert event.provider == "claude"
    assert event.conversation_id == "session-1"
    assert event.thread_id == "message-1"
    assert event.total_tokens == 132


def test_parse_claude_record_ignores_records_without_usage() -> None:
    event = parse_claude_record({"message": {"model": "claude-3-5-sonnet"}}, Path("chat.jsonl"), 1, Path("."))
    assert event is None


def test_parse_cline_record_extracts_nested_usage() -> None:
    payload = {
        "timestamp": "2026-08-01T12:00:00Z",
        "taskId": "task-1",
        "message": {
            "message": {
                "model": "deepseek/deepseek-v4-flash",
                "usage": {"input_tokens": 200, "output_tokens": 40, "cacheReadInputTokens": 10, "cacheCreationInputTokens": 15},
            }
        },
    }

    event = parse_cline_record(payload, "cline:1")

    assert event is not None
    assert event.provider == "cline"
    assert event.thread_id == "task-1"
    assert event.model == "deepseek/deepseek-v4-flash"
    assert event.prompt_tokens == 200
    assert event.completion_tokens == 40
    assert event.cache_read_tokens == 10
    assert event.cache_write_tokens == 15
    assert event.total_tokens == 265
    assert event.timestamp == datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def test_parse_cline_record_handles_snake_case_keys() -> None:
    payload = {
        "message": {
            "usage": {
                "input_tokens": 50,
                "output_tokens": 10,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 3,
                "reasoning_tokens": 4,
            }
        }
    }

    event = parse_cline_record(payload, "cline:2")

    assert event is not None
    assert event.total_tokens == 69


def test_parse_cline_record_ignores_messages_without_usage() -> None:
    event = parse_cline_record({"message": {"message": {"model": "model"}}}, "cline:3")
    assert event is None


def test_parse_cline_sqlite_reads_cline_messages_table(tmp_path) -> None:
    db_path = tmp_path / "cline.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE cline_messages (id TEXT PRIMARY KEY, timestamp TEXT, message TEXT)")
    message = {
        "timestamp": "2026-08-01T10:00:00Z",
        "message": {
            "model": "claude-3-7-sonnet",
            "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 7},
        },
    }
    connection.execute("INSERT INTO cline_messages (id, timestamp, message) VALUES (?, ?, ?)", ("m1", "2026-08-01T10:00:00Z", json.dumps(message)))
    connection.commit()
    connection.close()

    events = list(parse_cline_sqlite(db_path))

    assert len(events) == 1
    event = events[0]
    assert event.provider == "cline"
    assert event.model == "claude-3-7-sonnet"
    assert event.total_tokens == 132


def test_parse_cline_sqlite_ignores_non_cline_databases(tmp_path) -> None:
    db_path = tmp_path / "other.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE something_else (id TEXT)")
    connection.commit()
    connection.close()

    assert list(parse_cline_sqlite(db_path)) == []


def test_parse_cline_task_history_extracts_usage(tmp_path) -> None:
    history = [
        {
            "id": "1785782429653",
            "ulid": "01KZ4ETSZ1KC1Y6V7B02RR11DT",
            "ts": 1785782943608,
            "task": "write a poem",
            "tokensIn": 73347,
            "tokensOut": 1881,
            "cacheWrites": 0,
            "cacheReads": 43930,
            "totalCost": 0,
            "size": 55944,
            "cwdOnTaskInitialization": "/home/colin-spark/Projects/blog",
            "isFavorited": False,
            "modelId": "deepseek-v4-flash",
        },
        {
            "id": "1785782429654",
            "ulid": "01KZ4ETSZ1KC1Y6V7B02RR11DT",
            "ts": 1785782944000,
            "task": "empty task",
            "tokensIn": 0,
            "tokensOut": 0,
            "cacheWrites": 0,
            "cacheReads": 0,
            "modelId": "deepseek-v4-flash",
        },
    ]
    path = tmp_path / "taskHistory.json"
    path.write_text(json.dumps(history))

    events = list(parse_cline_task_history(path))

    assert len(events) == 1
    event = events[0]
    assert event.provider == "cline"
    assert event.model == "deepseek-v4-flash"
    assert event.thread_id == "01KZ4ETSZ1KC1Y6V7B02RR11DT"
    assert event.project == "blog"
    assert event.prompt_tokens == 73347
    assert event.completion_tokens == 1881
    assert event.cache_read_tokens == 43930
    assert event.cache_write_tokens == 0
    assert event.total_tokens == 73347 + 1881 + 43930
    assert event.timestamp == datetime(2026, 8, 3, 18, 49, 3, 608000, tzinfo=timezone.utc)


def test_parse_cline_task_history_skips_missing_file(tmp_path) -> None:
    assert list(parse_cline_task_history(tmp_path / "nope.json")) == []
