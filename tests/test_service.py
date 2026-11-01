from __future__ import annotations

import json
import os
import sqlite3
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


def test_collector_reparses_only_changed_transcripts(isolated, monkeypatch):
    transcript = isolated / "claude" / "proj" / "a.jsonl"
    transcript.parent.mkdir()
    transcript.write_text(_claude_line("m1", 10))
    parses: list[Path] = []
    real_parse = service.parse_claude_jsonl

    def counting_parse(path, root=None):
        parses.append(path)
        return real_parse(path, root)

    monkeypatch.setattr(service, "parse_claude_jsonl", counting_parse)
    collector = Collector()

    assert collector.scan_once() == 1
    assert collector.scan_once() == 0
    assert parses == [transcript]

    with transcript.open("a") as handle:
        handle.write(_claude_line("m2", 20))
    _bump_mtime(transcript)

    assert collector.scan_once() == 1
    assert parses == [transcript, transcript]
    assert collector.database.stats()["total_tokens"] == 32

    transcript.unlink()
    assert collector.scan_once() == 0
    assert transcript not in collector._claude_files


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
