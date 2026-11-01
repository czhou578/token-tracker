from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tokentracker.collector.parser import (
    parse_claude_jsonl,
    parse_claude_record,
    parse_cline_record,
    parse_cline_sqlite,
    parse_cline_task_history,
    parse_hermes_state_db,
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
    assert event.thread_id == "session-1"  # a thread is the session, not one line
    assert event.total_tokens == 132


def test_parse_claude_jsonl_counts_each_response_once(tmp_path) -> None:
    """Claude Code writes one line per content block; all lines repeat the same usage."""
    usage = {"input_tokens": 4, "output_tokens": 562, "cache_read_input_tokens": 24174, "cache_creation_input_tokens": 0}
    lines = [
        {"type": "user", "uuid": "u0", "sessionId": "s", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "uuid": "u1", "requestId": "req_1", "sessionId": "s", "timestamp": "2026-10-31T10:00:00.100Z",
         "message": {"id": "msg_1", "model": "claude-opus-5-5", "usage": usage, "content": [{"type": "thinking"}]}},
        {"type": "assistant", "uuid": "u2", "requestId": "req_1", "sessionId": "s", "timestamp": "2026-10-31T10:00:00.200Z",
         "message": {"id": "msg_1", "model": "claude-opus-5-5", "usage": usage, "content": [{"type": "text"}]}},
        {"type": "assistant", "uuid": "u3", "requestId": "req_1", "sessionId": "s", "timestamp": "2026-10-31T10:00:00.300Z",
         "message": {"id": "msg_1", "model": "claude-opus-5-5", "usage": usage, "content": [{"type": "tool_use"}]}},
        {"type": "assistant", "uuid": "u4", "requestId": "req_2", "sessionId": "s", "timestamp": "2026-10-31T10:00:05Z",
         "message": {"id": "msg_2", "model": "claude-opus-5-5", "usage": usage, "content": [{"type": "text"}]}},
        # No ids at all: counted per line, as before.
        {"type": "assistant", "uuid": "u5", "sessionId": "s", "timestamp": "2026-10-31T10:00:06Z",
         "message": {"model": "claude-opus-5-5", "usage": usage}},
    ]
    path = tmp_path / "chat.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    events = list(parse_claude_jsonl(path, tmp_path))

    assert [event.source_id.rsplit(":", 1)[1] for event in events] == ["2", "5", "6"]
    assert events[0].total_tokens == 4 + 562 + 24174
    assert json.loads(events[0].metadata_json)["message_id"] == "msg_1"
    assert {event.thread_id for event in events} == {"s"}


def test_parse_claude_record_keeps_pricing_fields() -> None:
    payload = {
        "timestamp": "2026-10-31T10:00:00Z",
        "sessionId": "s",
        "uuid": "m",
        "message": {
            "id": "msg_x",
            "model": "claude-opus-5-5",
            "usage": {
                "input_tokens": 4,
                "output_tokens": 10,
                "cache_creation_input_tokens": 1718,
                "cache_read_input_tokens": 195596,
                "cache_creation": {"ephemeral_1h_input_tokens": 1718, "ephemeral_5m_input_tokens": 0},
                "speed": "fast",
                "inference_geo": "us",
                "service_tier": "standard",
                "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 1},
            },
        },
    }

    event = parse_claude_record(payload, Path("chat.jsonl"), 1, Path("."))

    assert event is not None
    assert json.loads(event.metadata_json) == {
        "message_id": "msg_x",
        "cache_write_1h_tokens": 1718,
        "speed": "fast",
        "inference_geo": "us",
        "web_search_requests": 2,
    }


def test_parse_claude_record_omits_default_pricing_fields() -> None:
    payload = {"message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 1, "output_tokens": 1, "speed": "standard", "inference_geo": "global"}}}
    event = parse_claude_record(payload, Path("chat.jsonl"), 1, Path("."))
    assert event is not None
    assert event.metadata_json == "{}"


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
            "id": "1785782429655",
            "ulid": "01KZ4ETSZ1KC1Y6V7B02RR11DU",
            "ts": 1785782945000,
            "task": "paid task",
            "tokensIn": 1000,
            "tokensOut": 100,
            "cacheWrites": 0,
            "cacheReads": 0,
            "totalCost": 0.0123,
            "modelId": "anthropic/claude-sonnet-5-5",
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

    assert len(events) == 2
    assert json.loads(events[1].metadata_json) == {"reported_cost_usd": 0.0123}
    event = events[0]
    assert event.metadata_json == "{}"
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


def test_parse_hermes_state_db_reads_finished_and_open_sessions(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, model TEXT, title TEXT,
            started_at REAL, ended_at REAL, cwd TEXT, git_repo_root TEXT,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, reasoning_tokens INTEGER
        )"""
    )
    connection.execute(
        """INSERT INTO sessions (id, source, model, title, started_at, ended_at, cwd, git_repo_root,
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "20260807_182535_554eb9", "cli", "deepseek-v4-flash", "my session",
            1785782943.608, 1785783000.0, "/home/colin-spark/Projects/blog",
            "/home/colin-spark/Projects/blog", 100, 20, 5, 7, 3,
        ),
    )
    # In-progress session (ended_at NULL) is included so usage shows live.
    connection.execute(
        """INSERT INTO sessions (id, source, model, started_at, ended_at, cwd, git_repo_root,
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("live_session", "cli", "deepseek-v4-flash", 1785782943.608, None,
         "/tmp/x", "/tmp/x", 999, 999, 0, 0, 0),
    )
    connection.commit()
    connection.close()

    events = {event.thread_id: event for event in parse_hermes_state_db(db_path)}

    assert set(events) == {"20260807_182535_554eb9", "live_session"}
    live = events["live_session"]
    assert live.total_tokens == 1998
    assert json.loads(live.metadata_json)["ended_at"] is None
    event = events["20260807_182535_554eb9"]
    assert event.provider == "hermes"
    assert event.model == "deepseek-v4-flash"
    assert event.thread_id == "20260807_182535_554eb9"
    assert event.conversation_id == "20260807_182535_554eb9"
    assert event.project == "blog"
    assert event.prompt_tokens == 100
    assert event.completion_tokens == 20
    assert event.cache_read_tokens == 5
    assert event.cache_write_tokens == 7
    assert event.reasoning_tokens == 3
    assert event.total_tokens == 135
    assert event.timestamp == datetime(2026, 8, 3, 18, 49, 3, 608000, tzinfo=timezone.utc)


def test_parse_hermes_state_db_keeps_reported_cost(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, model TEXT, started_at REAL, ended_at REAL,
            input_tokens INTEGER, output_tokens INTEGER,
            estimated_cost_usd REAL, actual_cost_usd REAL, cost_source TEXT
        )"""
    )
    connection.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("hosted", "cli", "anthropic/claude-sonnet-5-5", 1785782943.6, None, 100, 20, 0.25, 0.31, "provider"),
            ("local", "cli", "deepseek-v4-flash-0731", 1785782943.6, None, 100, 20, 0.0, None, "none"),
        ],
    )
    connection.commit()
    connection.close()

    events = {event.thread_id: json.loads(event.metadata_json) for event in parse_hermes_state_db(db_path)}

    assert events["hosted"]["reported_cost_usd"] == 0.31
    assert events["hosted"]["hermes_cost_source"] == "provider"
    assert "reported_cost_usd" not in events["local"]
    assert events["local"]["cwd"] is None  # column absent in this schema


def test_parse_hermes_state_db_skips_missing_file(tmp_path) -> None:
    assert list(parse_hermes_state_db(tmp_path / "nope.db")) == []
