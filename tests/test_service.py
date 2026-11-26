from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import tokentracker.collector.service as service
from tokentracker.collector.service import Collector, file_signature


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Point every source at tmp_path so the collector never reads real data."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    monkeypatch.setenv("TOKEN_TRACKER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TOKEN_TRACKER_CLAUDE_DIR", str(claude_dir))
    monkeypatch.setenv("TOKEN_TRACKER_CLINE_DB", str(tmp_path / "no-cline.db"))
    monkeypatch.setenv("TOKEN_TRACKER_CLINE_TASK_HISTORY", str(tmp_path / "no-history.json"))
    monkeypatch.setenv("TOKEN_TRACKER_HERMES_DB", str(tmp_path / "state.db"))
    return tmp_path


def _claude_line(uuid: str, tokens: int) -> str:
    return json.dumps(
        {
            "timestamp": "2026-10-31T10:00:00Z",
            "sessionId": "s1",
            "uuid": uuid,
            "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": tokens, "output_tokens": 1}},
        }
    ) + "\n"


def _bump_mtime(path: Path) -> None:
    """Advance mtime so a rewrite inside one clock tick still registers as a change."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


def test_collector_reads_only_appended_lines(isolated, monkeypatch):
    transcript = isolated / "claude" / "proj" / "a.jsonl"
    transcript.parent.mkdir()
    transcript.write_text(_claude_line("m1", 10))
    starts: list[int] = []
    real_read = service.ClaudeTranscript.read

    def recording_read(self, stat=None):
        starts.append(self._offset)
        return real_read(self, stat)

    monkeypatch.setattr(service.ClaudeTranscript, "read", recording_read)
    collector = Collector()

    assert collector.scan_once() == 1
    assert collector.scan_once() == 0
    assert starts == [0]

    first_size = transcript.stat().st_size
    with transcript.open("a") as handle:
        handle.write(_claude_line("m2", 20))
    _bump_mtime(transcript)

    assert collector.scan_once() == 1
    assert starts == [0, first_size]
    assert collector.database.stats()["total_tokens"] == 32

    transcript.unlink()
    assert collector.scan_once() == 0
    assert transcript not in collector._transcripts


def test_restart_skips_transcripts_unchanged_since_a_clean_shutdown(isolated, monkeypatch):
    transcript = isolated / "claude" / "proj" / "a.jsonl"
    transcript.parent.mkdir()
    transcript.write_text(_claude_line("m1", 10))
    first = Collector()
    first.restore_state()  # as `run_forever` does; it also records that activity collection has run
    first.scan_once()
    first.save_state()
    first.database.close()

    reads: list[Path] = []
    real_read = service.ClaudeTranscript.read
    monkeypatch.setattr(service.ClaudeTranscript, "read", lambda self, stat=None: reads.append(self.path) or real_read(self, stat))
    second = Collector()
    assert second.restore_state() == 1
    assert second.database.get_meta(service.TRANSCRIPTS_META_KEY) is None  # used once
    assert second.scan_once() == 0
    assert reads == []

    with transcript.open("a") as handle:
        handle.write(_claude_line("m2", 20))
    _bump_mtime(transcript)
    assert second.scan_once() == 1  # read from the top; the first row is already stored
    assert second.database.stats()["total_tokens"] == 32
    assert second.database.stats()["requests"] == 2


def test_reprice_runs_only_when_the_pricing_changes(isolated, monkeypatch):
    collector = Collector()
    passes: list[int] = []
    real_reprice = collector.database.reprice
    monkeypatch.setattr(collector.database, "reprice", lambda pricing: passes.append(1) or real_reprice(pricing))

    collector.reprice()
    collector.reprice()
    assert len(passes) == 1

    (isolated / "home" / "pricing.json").write_text(json.dumps({"deepseek-v4-flash": {"input": 0.14, "output": 0.28}}))
    collector.scan_once()  # reloads the overrides
    assert len(passes) == 2


def test_collector_releases_memory_only_after_a_parse(isolated, monkeypatch):
    transcript = isolated / "claude" / "proj" / "a.jsonl"
    transcript.parent.mkdir()
    transcript.write_text(_claude_line("m1", 10))
    releases: list[int] = []
    monkeypatch.setattr(service, "release_memory", lambda: releases.append(1))
    collector = Collector()

    collector.scan_once()
    assert len(releases) == 1
    collector.scan_once()
    assert len(releases) == 1

    with transcript.open("a") as handle:
        handle.write(_claude_line("m2", 20))
    _bump_mtime(transcript)
    collector.scan_once()
    assert len(releases) == 2


def _hermes_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, model TEXT, title TEXT,
            started_at REAL, ended_at REAL, cwd TEXT, git_repo_root TEXT,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, reasoning_tokens INTEGER
        )"""
    )
    return connection


def test_collector_tracks_open_hermes_sessions_live(isolated):
    db_path = isolated / "state.db"
    connection = _hermes_db(db_path)
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("live", "cli", "deepseek-v4-flash-0731", None, 1793000000.0, None, "/p", "/p", 100, 20, 0, 0, 0),
    )
    connection.commit()
    collector = Collector()

    assert collector.scan_once() == 1
    assert collector.database.stats(provider="hermes")["total_tokens"] == 120

    connection.execute("UPDATE sessions SET input_tokens = 300, output_tokens = 50 WHERE id = 'live'")
    connection.commit()
    connection.close()
    _bump_mtime(db_path)

    assert collector.scan_once() == 1
    stats = collector.database.stats(provider="hermes")
    assert stats["total_tokens"] == 350
    assert stats["requests"] == 1
    assert collector.scan_once() == 0


def test_file_signature_folds_in_wal_file(tmp_path):
    db = tmp_path / "state.db"
    db.write_bytes(b"main")
    base = file_signature(db)
    assert base is not None
    (tmp_path / "state.db-wal").write_bytes(b"appended frames")
    assert file_signature(db) != base
    assert file_signature(tmp_path / "missing.db") is None


def test_collector_prices_events_and_reloads_overrides(isolated):
    transcript = isolated / "claude" / "proj" / "a.jsonl"
    transcript.parent.mkdir()
    transcript.write_text(_claude_line("m1", 1_000_000))
    db_path = isolated / "state.db"
    connection = _hermes_db(db_path)
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("live", "cli", "deepseek-v4-flash-0731", None, 1793000000.0, None, "/p", "/p", 1_000_000, 0, 0, 0, 0),
    )
    connection.commit()
    connection.close()
    collector = Collector()

    assert collector.scan_once() == 2
    assert collector.database.stats(provider="claude")["cost_usd"] == pytest.approx(4.00002)
    assert collector.database.stats(provider="hermes")["priced_requests"] == 0

    (isolated / "home" / "pricing.json").write_text(json.dumps({"deepseek-v4-flash": {"input": 0.14, "output": 0.28}}))
    collector.scan_once()

    hermes = collector.database.stats(provider="hermes")
    assert hermes["priced_requests"] == 1
    assert hermes["cost_usd"] == pytest.approx(0.14)


def _gateway_line(message_id: str, ts: str, tokens: int) -> str:
    return json.dumps({"ts": ts, "id": message_id, "model": "deepseek-v4-flash-0731", "dest": "deepseek", "session": "s1",
                       "usage": {"input_tokens": tokens, "output_tokens": 1}}) + "\n"


def _gateway_log(isolated: Path) -> Path:
    path = isolated / "home" / "gateway-usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_gateway_lines_count_only_the_responses_no_transcript_holds(isolated):
    transcript = isolated / "claude" / "proj" / "s1.jsonl"
    transcript.parent.mkdir()
    transcript.write_text(json.dumps({
        "timestamp": "2026-10-31T10:00:00Z", "sessionId": "s1", "uuid": "u1",
        "message": {"id": "chatcmpl-main", "model": "deepseek-v4-flash-0731", "usage": {"input_tokens": 10, "output_tokens": 1}},
    }) + "\n")
    _gateway_log(isolated).write_text(_gateway_line("chatcmpl-main", "2026-10-31T10:04:00Z", 10)
                                      + _gateway_line("chatcmpl-title", "2026-10-31T10:00:02Z", 100))
    collector = Collector()

    assert collector.scan_once() == 2  # the main turn from its transcript, the session title from the gateway log
    assert collector.database.stats()["total_tokens"] == 11 + 101
    with sqlite3.connect(collector.settings.db_path) as connection:
        rows = connection.execute("SELECT source_id, project, conversation_id FROM usage_events WHERE source_id LIKE 'gateway:%'").fetchall()
    assert rows == [("gateway:chatcmpl-title", "proj", "s1")]
    assert collector.scan_once() == 0


def test_a_gateway_line_waits_for_the_grace_period_and_its_position_survives_a_restart(isolated, monkeypatch):
    old = _gateway_line("chatcmpl-old", "2026-10-31T10:00:00Z", 5)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _gateway_log(isolated).write_text(old + _gateway_line("chatcmpl-new", now, 7))
    first = Collector()
    assert first.scan_once() == 1  # the new line waits
    assert first.database.get_meta(service.GATEWAY_META_KEY)["offset"] == len(old)
    first.database.close()

    second = Collector()
    second.restore_state()
    assert second.gateway.offset == len(old)  # the new line is read again, the old one is not
    monkeypatch.setattr(service, "GATEWAY_GRACE_SECONDS", 0)
    assert second.scan_once() == 1
    assert second.database.stats()["total_tokens"] == 6 + 8


def _activity_lines() -> str:
    def assistant(message_id: str, content: list, stop_reason: str) -> str:
        message = {"id": message_id, "model": "claude-opus-5-5", "stop_reason": stop_reason,
                   "usage": {"input_tokens": 10, "output_tokens": 1}, "content": content}
        return json.dumps({"type": "assistant", "timestamp": "2026-10-31T10:00:00Z", "sessionId": "s1", "message": message}) + "\n"

    def tool(call_id: str, name: str) -> dict:
        return {"type": "tool_use", "id": call_id, "name": name}

    fire = {"type": "system", "subtype": "scheduled_task_fire", "uuid": "fire-1", "timestamp": "2026-10-31T10:00:00Z", "sessionId": "s1"}
    return (
        json.dumps(fire) + "\n"
        + assistant("msg_1", [tool("toolu_1", "Bash"), tool("toolu_2", "Bash")], "tool_use")
        + assistant("msg_2", [tool("toolu_3", "Edit")], "tool_use")
        + assistant("msg_3", [{"type": "text"}], "end_turn")
    )


def _counts(collector: Collector) -> tuple[int, int, int, int]:
    counts = collector.database.highlights()
    return counts["replies"], counts["tool_calls"], counts["tools_used"], counts["scheduled_runs"]


def test_collector_stores_the_activity_of_a_transcript(isolated):
    transcript = isolated / "claude" / "proj" / "a.jsonl"
    transcript.parent.mkdir()
    transcript.write_text(_activity_lines())
    collector = Collector()

    assert collector.scan_once() == 3
    assert _counts(collector) == (1, 3, 2, 1)
    assert collector.database.highlights(host=collector.settings.hostname)["tool_calls"] == 3

    # Claude Code copies a session to a second file (a worktree): the ids repeat, the counts do not.
    copy = isolated / "claude" / "proj-worktree" / "a.jsonl"
    copy.parent.mkdir()
    copy.write_text(_activity_lines())
    collector.scan_once()
    assert _counts(collector) == (1, 3, 2, 1)


def test_first_start_with_activity_collection_reads_every_transcript_once(isolated, monkeypatch):
    transcript = isolated / "claude" / "proj" / "a.jsonl"
    transcript.parent.mkdir()
    transcript.write_text(_activity_lines())
    old = Collector()  # stands for the version before activity collection
    old.scan_once()
    old.save_state()
    with old.database.connect() as connection:
        connection.execute("DELETE FROM activity_events")
    old.database.close()

    upgraded = Collector()
    assert upgraded.restore_state() == 0  # the saved signatures are not used this once
    assert upgraded.scan_once() == 0  # the usage rows are already stored
    assert _counts(upgraded) == (1, 3, 2, 1)
    upgraded.save_state()
    upgraded.database.close()

    reads: list[Path] = []
    real_read = service.ClaudeTranscript.read
    monkeypatch.setattr(service.ClaudeTranscript, "read", lambda self, stat=None: reads.append(self.path) or real_read(self, stat))
    later = Collector()
    assert later.restore_state() == 1
    assert later.scan_once() == 0 and reads == []


def test_collector_stores_hermes_activity_and_keeps_its_position(isolated, monkeypatch):
    db_path = isolated / "state.db"
    connection = _hermes_db(db_path)
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("cron_1", "cron", "deepseek-v4-flash-0731", None, 1793000000.0, None, "/p", "/p", 100, 20, 0, 0, 0),
    )
    connection.execute(
        """CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
            tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL, finish_reason TEXT)"""
    )
    insert = "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name, timestamp, finish_reason) VALUES ('cron_1', ?, ?, ?, ?, ?, ?)"
    connection.execute(insert, ("tool", None, "call_1", "terminal", 1793000001.0, None))
    connection.execute(insert, ("assistant", "Done.", None, None, 1793000002.0, "stop"))
    connection.commit()
    collector = Collector()

    assert collector.scan_once() == 1
    assert _counts(collector) == (1, 1, 1, 1)
    assert collector.database.highlights(provider="hermes")["scheduled_runs"] == 1

    connection.execute(insert, ("tool", None, "call_2", "web_search", 1793000003.0, None))
    connection.commit()
    connection.close()
    _bump_mtime(db_path)
    collector.scan_once()
    assert _counts(collector) == (1, 2, 2, 1)
    collector.database.close()

    starts: list[int] = []
    real = service.parse_hermes_activity
    monkeypatch.setattr(service, "parse_hermes_activity", lambda path, after=0: starts.append(after) or real(path, after))
    restarted = Collector()
    restarted.scan_once()
    assert starts == [3]  # the position survives a restart
    assert _counts(restarted) == (1, 2, 2, 1)


def test_collector_tells_the_forwarder_about_activity_without_new_usage(isolated, monkeypatch):
    transcript = isolated / "claude" / "proj" / "a.jsonl"
    transcript.parent.mkdir()
    lines = _activity_lines().splitlines(keepends=True)
    transcript.write_text("".join(lines[:3]))  # the schedule line and two responses
    collector = Collector()
    told: list[int] = []
    monkeypatch.setattr(collector.forwarder, "notify", lambda: told.append(1))
    collector.scan_once()
    assert len(told) == 1 and collector.scan_once() == 0 and len(told) == 1

    # A later line of a response that is already counted: a new tool call, no new usage event.
    extra = json.loads(lines[2])
    extra["message"]["content"] = [{"type": "tool_use", "id": "toolu_9", "name": "Read"}]
    with transcript.open("a") as handle:
        handle.write(json.dumps(extra) + "\n")
    _bump_mtime(transcript)
    assert collector.scan_once() == 0
    assert len(told) == 2 and collector.database.highlights()["tool_calls"] == 4
