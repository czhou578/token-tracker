from __future__ import annotations

from pathlib import Path

from tokentracker.collector.parser import parse_claude_record


def test_parse_claude_record_extracts_usage() -> None:
    payload = {
        "timestamp": "2026-07-23T10:00:00Z",
        "sessionId": "session-1",
        "uuid": "message-1",
        "message": {
            "role": "assistant",
            "model": "claude-3-5-sonnet-20241022",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 7,
            },
        },
    }

    event = parse_claude_record(payload, Path("/tmp/root/project/chat.jsonl"), 3, Path("/tmp/root"))

    assert event is not None
    assert event.conversation_id == "session-1"
    assert event.thread_id == "message-1"
    assert event.project == "project"
    assert event.total_tokens == 132
    assert event.cost_usd > 0


def test_parse_claude_record_ignores_records_without_usage() -> None:
    event = parse_claude_record({"type": "user", "message": {"role": "user"}}, Path("x.jsonl"), 1, Path("."))
    assert event is None
