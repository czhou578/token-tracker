from __future__ import annotations

from datetime import datetime, timezone

import json
import sqlite3

from tokentracker.collector.config import get_settings
from tokentracker.collector.database import UsageDatabase
from tokentracker.collector.models import UsageEvent
from tokentracker.collector.pricing import Pricing


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
    )

    assert database.insert_events([event]) == 1
    assert database.insert_events([event]) == 0

    stats = database.stats()
    assert stats["total_tokens"] == 15
    assert stats["requests"] == 1
    assert stats["threads"] == 1

    assert database.projects()[0]["project"] == "project"
    assert database.models()[0]["model"] == "claude-3-5-sonnet"


def test_hermes_db_env_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_TRACKER_HERMES_DB", str(tmp_path / "custom.db"))

    settings = get_settings()

    assert settings.hermes_db == tmp_path / "custom.db"


def test_upsert_refreshes_changed_rows_only(tmp_path) -> None:
    database = UsageDatabase(tmp_path / "usage.db")
    first = UsageEvent(source_id="hermes:s1", provider="hermes", prompt_tokens=100, completion_tokens=20)

    assert database.upsert_events([first]) == 1
    assert database.upsert_events([first]) == 0

    grown = first.model_copy(update={"prompt_tokens": 300, "completion_tokens": 50, "total_tokens": 0})
    assert database.upsert_events([grown]) == 1

    stats = database.stats(provider="hermes")
    assert stats["requests"] == 1
    assert stats["total_tokens"] == 350
    assert database.insert_events([first]) == 0  # insert never overwrites


def test_latest_by_provider(tmp_path) -> None:
    database = UsageDatabase(tmp_path / "usage.db")
    database.insert_events(
        [
            UsageEvent(source_id="a", provider="claude", prompt_tokens=1, timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc)),
            UsageEvent(source_id="b", provider="claude", prompt_tokens=1, timestamp=datetime(2026, 10, 2, tzinfo=timezone.utc)),
            UsageEvent(source_id="c", provider="hermes", prompt_tokens=1, timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc)),
        ]
    )

    latest = database.latest_by_provider()

    assert latest["claude"]["events"] == 2
    assert latest["claude"]["last_timestamp"].startswith("2026-10-02")
    assert latest["hermes"]["events"] == 1


def test_init_adds_cost_column_to_old_databases(tmp_path) -> None:
    path = tmp_path / "usage.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL UNIQUE, timestamp TEXT NOT NULL,
            thread_id TEXT, conversation_id TEXT, project TEXT, provider TEXT NOT NULL, model TEXT,
            prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL DEFAULT 0,
            latency_ms INTEGER, metadata_json TEXT NOT NULL DEFAULT '{}'
        )"""
    )
    connection.execute(
        "INSERT INTO usage_events (source_id, timestamp, provider, model, prompt_tokens, total_tokens) VALUES ('old', '2026-10-01T00:00:00+00:00', 'claude', 'claude-opus-5-5', 1000000, 1000000)"
    )
    connection.commit()
    connection.close()

    database = UsageDatabase(path)

    stats = database.stats()
    assert stats["requests"] == 1 and stats["priced_requests"] == 0 and stats["cost_usd"] == 0
    assert database.reprice(Pricing()) == 1
    stats = database.stats()
    assert stats["priced_requests"] == 1
    assert abs(stats["cost_usd"] - 4.0) < 1e-9
    assert database.reprice(Pricing()) == 0


def test_cost_aggregates_and_unpriced_models(tmp_path) -> None:
    database = UsageDatabase(tmp_path / "usage.db")
    pricing = Pricing()
    database.insert_events(
        pricing.price_events(
            [
                UsageEvent(source_id="a", provider="claude", model="claude-opus-5-5", project="p", prompt_tokens=1_000_000),
                UsageEvent(source_id="b", provider="claude", model="claude-opus-5-5", project="p", completion_tokens=100_000),
                UsageEvent(source_id="c", provider="claude", model="qwen3.8-27b", project="q", prompt_tokens=5_000),
            ]
        )
    )

    stats = database.stats()
    assert stats["requests"] == 3 and stats["priced_requests"] == 2
    assert abs(stats["cost_usd"] - 6.0) < 1e-9
    models = {row["model"]: row for row in database.models()}
    assert abs(models["claude-opus-5-5"]["cost_usd"] - 6.0) < 1e-9
    assert models["qwen3.8-27b"]["cost_usd"] is None
    assert models["qwen3.8-27b"]["priced_requests"] == 0
    assert database.unpriced_models() == [
        {"model": "qwen3.8-27b", "events": 1, "total_tokens": 5_000, "last_timestamp": database.recent()[0]["timestamp"]}
    ] or database.unpriced_models()[0]["model"] == "qwen3.8-27b"
    assert database.recent()[0]["cost_usd"] in (None, 4.0, 2.0)


def _legacy_database(path, rows):
    """A database as written before the collector deduplicated responses: no meta table, no cost column."""
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL UNIQUE, timestamp TEXT NOT NULL,
            thread_id TEXT, conversation_id TEXT, project TEXT, provider TEXT NOT NULL, model TEXT,
            prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL DEFAULT 0,
            latency_ms INTEGER, metadata_json TEXT NOT NULL DEFAULT '{}'
        )"""
    )
    connection.executemany(
        "INSERT INTO usage_events (source_id, timestamp, thread_id, conversation_id, provider, model, prompt_tokens, completion_tokens, cache_read_tokens, total_tokens) VALUES (?, ?, ?, ?, 'claude', ?, ?, ?, ?, ?)",
        rows,
    )
    connection.commit()
    connection.close()


def test_init_collapses_legacy_duplicate_claude_rows_once(tmp_path) -> None:
    path = tmp_path / "usage.db"
    f = "/logs/proj/a.jsonl"
    _legacy_database(
        path,
        [
            # One response written as thinking + text + tool_use lines.
            (f"{f}:10", "2026-10-31T10:00:00.100000+00:00", "u1", "s1", "claude-opus-5-5", 4, 562, 24174, 24740),
            (f"{f}:11", "2026-10-31T10:00:00.200000+00:00", "u2", "s1", "claude-opus-5-5", 4, 562, 24174, 24740),
            (f"{f}:12", "2026-10-31T10:00:00.300000+00:00", "u3", "s1", "claude-opus-5-5", 4, 562, 24174, 24740),
            # A different response with different counts.
            (f"{f}:20", "2026-10-31T10:00:05+00:00", "u4", "s1", "claude-opus-5-5", 4, 90, 24740, 24834),
            # Same counts as the first response but far later: a separate response.
            (f"{f}:99", "2026-10-31T11:00:00+00:00", "u5", "s1", "claude-opus-5-5", 4, 562, 24174, 24740),
            # Another file is handled independently.
            ("/logs/proj/b.jsonl:3", "2026-10-31T10:00:00.400000+00:00", "u6", "s2", "claude-opus-5-5", 4, 562, 24174, 24740),
        ],
    )

    database = UsageDatabase(path)

    info = database.migration_info()
    assert info["removed"] == 2
    assert info["backup"] and sqlite3.connect(info["backup"]).execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 6
    remaining = sorted(row["source_id"] for row in database.recent(limit=10))
    assert remaining == [f"{f}:10", f"{f}:20", f"{f}:99", "/logs/proj/b.jsonl:3"]
    assert database.stats()["threads"] == 2  # thread_id now equals the session id
    # Re-opening does not run the migration again, even if new duplicates appear.
    sqlite3.connect(path).execute(
        "INSERT INTO usage_events (source_id, timestamp, provider, model, prompt_tokens, total_tokens) VALUES (?, ?, 'claude', 'claude-opus-5-5', 1, 1), (?, ?, 'claude', 'claude-opus-5-5', 1, 1)",
        (f"{f}:200", "2026-10-31T12:00:00+00:00", f"{f}:201", "2026-10-31T12:00:00+00:00"),
    ).connection.commit()
    assert UsageDatabase(path).stats()["requests"] == 6


def test_migration_uses_the_transcript_when_it_still_exists(tmp_path) -> None:
    usage = {"input_tokens": 4, "output_tokens": 562, "cache_read_input_tokens": 24174}
    transcript = tmp_path / "proj" / "a.jsonl"
    transcript.parent.mkdir()
    lines = [
        {"type": "assistant", "uuid": "u1", "sessionId": "s1", "timestamp": "2026-10-31T10:00:00Z",
         "message": {"id": "msg_1", "model": "claude-opus-5-5", "usage": usage}},
        {"type": "assistant", "uuid": "u2", "sessionId": "s1", "timestamp": "2026-10-31T10:00:01Z",
         "message": {"id": "msg_1", "model": "claude-opus-5-5", "usage": usage}},
        {"type": "assistant", "uuid": "u3", "sessionId": "s1", "timestamp": "2026-10-31T10:00:02Z",
         "message": {"id": "msg_2", "model": "claude-opus-5-5", "usage": usage}},  # same counts, new response
        {"type": "assistant", "uuid": "u4", "sessionId": "s1", "timestamp": "2026-10-31T10:00:03Z",
         "message": {"id": "msg_2", "model": "claude-opus-5-5", "usage": usage}},
    ]
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))
    f = str(transcript.resolve())
    path = tmp_path / "usage.db"
    _legacy_database(
        path,
        [(f"{f}:{n}", f"2026-10-31T10:00:0{n - 1}+00:00", f"u{n}", "s1", "claude-opus-5-5", 4, 562, 24174, 24740) for n in (1, 2, 3, 4)],
    )

    database = UsageDatabase(path)

    # The heuristic alone would keep one row; the transcript says there are two responses.
    assert database.migration_info()["removed"] == 2
    assert sorted(row["source_id"] for row in database.recent(limit=10)) == [f"{f}:1", f"{f}:3"]


def test_fresh_database_marks_migration_done_without_backup(tmp_path) -> None:
    database = UsageDatabase(tmp_path / "usage.db")
    info = database.migration_info()
    assert info["removed"] == 0 and info["backup"] is None
    assert list(tmp_path.glob("*.bak-*")) == []


def test_series_buckets_follow_the_window(tmp_path) -> None:
    database = UsageDatabase(tmp_path / "usage.db")
    database.insert_events(
        [
            UsageEvent(source_id="a", provider="claude", prompt_tokens=10, timestamp=datetime(2026, 10, 5, 9, tzinfo=timezone.utc)),   # Monday
            UsageEvent(source_id="b", provider="claude", prompt_tokens=20, timestamp=datetime(2026, 10, 11, 23, tzinfo=timezone.utc)),  # Sunday, same week
            UsageEvent(source_id="c", provider="claude", prompt_tokens=40, timestamp=datetime(2026, 10, 12, 1, tzinfo=timezone.utc)),   # next Monday
            UsageEvent(source_id="d", provider="claude", prompt_tokens=80, timestamp=datetime(2026, 11, 1, 1, tzinfo=timezone.utc)),
        ]
    )

    by_week = database.series(window=None)
    assert by_week["unit"] == "month"
    assert [(row["period"], row["total_tokens"]) for row in by_week["rows"]] == [("2026-10-01", 70), ("2026-11-01", 80)]

    from tokentracker.collector.database import BUCKET_EXPRESSIONS, bucket_unit

    assert bucket_unit("90d") == "week" and bucket_unit("7d") == "day" and bucket_unit("today") == "hour"
    weeks = database._grouped(BUCKET_EXPRESSIONS["week"], "period", {})
    assert sorted((row["period"], row["total_tokens"]) for row in weeks) == [("2026-10-05", 30), ("2026-10-12", 40), ("2026-10-26", 80)]
    days = database._grouped(BUCKET_EXPRESSIONS["day"], "period", {})
    assert len(days) == 4


def test_host_column_is_stamped_and_filterable(tmp_path) -> None:
    database = UsageDatabase(tmp_path / "usage.db", hostname="spark")
    database.insert_events([UsageEvent(source_id="a", provider="claude", prompt_tokens=5)])  # no host set
    database.insert_events([UsageEvent(source_id="b", provider="claude", host="laptop", prompt_tokens=7)])
    # Rows written without a host are stamped with the local hostname when the database is opened.
    database = UsageDatabase(tmp_path / "usage.db", hostname="spark")
    hosts = {row["host"]: row["total_tokens"] for row in database.hosts()}
    assert hosts == {"spark": 5, "laptop": 7}
    assert database.stats(host="laptop")["total_tokens"] == 7
    assert database.stats()["total_tokens"] == 12


def test_ingest_namespaces_source_ids_and_upserts(tmp_path) -> None:
    database = UsageDatabase(tmp_path / "usage.db", hostname="spark")
    database.insert_events([UsageEvent(source_id="hermes:1", provider="hermes", prompt_tokens=100)])
    remote = [UsageEvent(source_id="hermes:1", provider="hermes", prompt_tokens=20, host="ignored")]

    assert database.ingest_events("laptop", remote) == 1
    assert database.stats()["requests"] == 2  # same source_id on another host does not collide
    assert database.stats(host="laptop")["total_tokens"] == 20
    assert database.ingest_events("laptop", remote) == 0  # unchanged resend is a no-op
    grown = [UsageEvent(source_id="hermes:1", provider="hermes", prompt_tokens=50)]
    assert database.ingest_events("laptop", grown) == 1
    assert database.stats(host="laptop")["total_tokens"] == 50
    assert database.ingest_events("", grown) == 0
    assert all(row["host"] == "laptop" for row in database.recent() if row["source_id"].startswith("laptop::"))


def test_pending_forward_tracks_local_unsynced_rows(tmp_path) -> None:
    database = UsageDatabase(tmp_path / "usage.db", hostname="laptop")
    database.insert_events([UsageEvent(source_id="a", provider="claude", prompt_tokens=1, host="laptop")])
    database.ingest_events("other", [UsageEvent(source_id="x", provider="claude", prompt_tokens=1)])

    pending = database.pending_forward()
    assert [event.source_id for _, event in pending] == ["a"]  # ingested rows are never forwarded on
    assert database.pending_forward_count() == 1
    database.mark_forwarded(row_id for row_id, _ in pending)
    assert database.pending_forward() == []

    # An upsert that changes the row queues it again.
    database.upsert_events([UsageEvent(source_id="a", provider="claude", prompt_tokens=9, host="laptop")])
    assert database.pending_forward_count() == 1
    database.set_meta("k", {"n": 1})
    assert database.get_meta("k") == {"n": 1} and database.get_meta("missing") is None
