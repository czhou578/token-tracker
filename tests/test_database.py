from __future__ import annotations

from datetime import datetime, timezone

from tokentracker.collector.database import UsageDatabase
from tokentracker.collector.models import UsageEvent


def test_database_inserts_idempotently_and_aggregates(tmp_path) -> None:
    database = UsageDatabase(tmp_path / "usage.db")
    event = UsageEvent(
        source_id="file:1",
        timestamp=datetime(2026, 7, 23, tzinfo=timezone.utc),
        thread_id="thread",
        conversation_id="conversation",
        project="project",
        model="claude-3-5-sonnet",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cost_usd=0.001,
    )

    assert database.insert_events([event]) == 1
    assert database.insert_events([event]) == 0

    stats = database.stats()
    assert stats["total_tokens"] == 15
    assert stats["requests"] == 1
    assert stats["threads"] == 1

    assert database.projects()[0]["project"] == "project"
    assert database.models()[0]["model"] == "claude-3-5-sonnet"
