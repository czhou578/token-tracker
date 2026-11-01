from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from tokentracker.collector.config import get_settings
from tokentracker.collector.database import UsageDatabase
from tokentracker.collector.forwarder import Forwarder, export_event, hashed_project
from tokentracker.collector.models import UsageEvent


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def linked(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_TRACKER_HOME", str(tmp_path))
    monkeypatch.setenv("TOKEN_TRACKER_REMOTE_URL", "https://spark.example.ts.net/")
    monkeypatch.setenv("TOKEN_TRACKER_REMOTE_TOKEN", "secret")
    monkeypatch.setenv("TOKEN_TRACKER_PROJECT_LABELS", "hash")
    monkeypatch.setenv("TOKEN_TRACKER_HOSTNAME", "laptop")
    settings = get_settings()
    database = UsageDatabase(settings.db_path, hostname=settings.hostname)
    return settings, database


def test_export_event_hashes_project_and_whitelists_metadata(linked) -> None:
    settings, _ = linked
    event = UsageEvent(
        source_id="/home/me/.claude/projects/x/s.jsonl:3", provider="hermes", project="secret-repo",
        prompt_tokens=10, cost_usd=0.5,
        metadata_json=json.dumps({"cwd": "/home/me/secret-repo", "title": "private", "cost_source": "table", "cache_write_1h_tokens": 5}),
    )
    data = export_event(event, settings)
    assert data["project"] == hashed_project("secret-repo", settings.project_salt) and data["project"].startswith("proj-")
    assert json.loads(data["metadata_json"]) == {"cache_write_1h_tokens": 5, "cost_source": "table"}
    assert data["host"] == "laptop" and data["cost_usd"] == 0.5 and data["source_id"] == event.source_id
    assert isinstance(data["timestamp"], str)


def test_forwarder_posts_pending_events_and_marks_them(linked, monkeypatch) -> None:
    settings, database = linked
    database.insert_events([UsageEvent(source_id=f"e{i}", provider="claude", host="laptop", prompt_tokens=i + 1) for i in range(3)])
    calls = []

    def fake_urlopen(request, timeout=0):
        calls.append((request.full_url, request.get_header("Authorization"), json.loads(request.data)))
        return _Response(b'{"accepted": 3}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    forwarder = Forwarder(settings, database)

    assert forwarder.enabled
    assert forwarder.flush() == 3
    url, auth, payload = calls[0]
    assert url == "https://spark.example.ts.net/api/events"
    assert auth == "Bearer secret"
    assert payload["host"] == "laptop" and len(payload["events"]) == 3
    assert database.pending_forward_count() == 0
    assert database.get_meta("forward_last_ok")["sent"] == 3
    assert forwarder.flush() == 0 and len(calls) == 1


def test_forwarder_backs_off_after_failure(linked, monkeypatch) -> None:
    settings, database = linked
    database.insert_events([UsageEvent(source_id="e", provider="claude", host="laptop", prompt_tokens=1)])
    attempts = []

    def failing_urlopen(request, timeout=0):
        attempts.append(1)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)
    forwarder = Forwarder(settings, database)

    assert forwarder.flush() == 0
    assert forwarder.flush() == 0  # inside the backoff window: no second attempt
    assert len(attempts) == 1
    assert database.pending_forward_count() == 1
    assert "connection refused" in database.get_meta("forward_last_error")["error"]


def test_forwarder_is_disabled_without_a_link(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TOKEN_TRACKER_HOME", str(tmp_path))
    monkeypatch.delenv("TOKEN_TRACKER_REMOTE_URL", raising=False)
    settings = get_settings()
    database = UsageDatabase(settings.db_path, hostname=settings.hostname)
    assert not Forwarder(settings, database).enabled
    assert Forwarder(settings, database).flush() == 0


def test_forwarder_queries_only_after_new_events(linked, monkeypatch) -> None:
    settings, database = linked
    database.insert_events([UsageEvent(source_id="e1", provider="claude", host="laptop", prompt_tokens=1)])
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=0: _Response(b"{}"))
    queries = []
    real_pending = database.pending_forward
    monkeypatch.setattr(database, "pending_forward", lambda limit: queries.append(limit) or real_pending(limit))
    forwarder = Forwarder(settings, database)

    assert forwarder.flush() == 1
    assert forwarder.flush() == 0 and forwarder.flush() == 0
    assert len(queries) == 1  # drained: idle polls do not touch the database

    database.insert_events([UsageEvent(source_id="e2", provider="claude", host="laptop", prompt_tokens=1)])
    forwarder.notify()
    assert forwarder.flush() == 1
    assert len(queries) == 2
