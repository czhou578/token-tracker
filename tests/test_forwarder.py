from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from tokentracker.collector.config import get_settings
from tokentracker.collector.database import UsageDatabase
from tokentracker.collector.forwarder import Forwarder, export_event, hashed_project
from tokentracker.collector.models import ActivityEvent, UsageEvent


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


def _server(monkeypatch, reply: bytes) -> list[dict]:
    payloads: list[dict] = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=0: payloads.append(json.loads(request.data)) or _Response(reply))
    return payloads


def test_forwarder_posts_activity_rows_in_their_own_batch(linked, monkeypatch) -> None:
    settings, database = linked
    database.insert_events([UsageEvent(source_id="e1", provider="claude", host="laptop", prompt_tokens=1)])
    database.insert_activity([
        ActivityEvent(source_id="tool:c1", kind="tool_call", name="Bash", thread_id="s1", project="secret-repo", model="claude-opus-5-5"),
        ActivityEvent(source_id="reply:m1", kind="reply", thread_id="s1", project="secret-repo"),
        ActivityEvent(source_id="tool:c9", kind="tool_call", name="Edit", host="other"),  # not from this machine
    ])
    payloads = _server(monkeypatch, b'{"accepted": 1, "received": 1, "activity": 2}')
    forwarder = Forwarder(settings, database)

    assert database.pending_forward_count() == 3
    assert forwarder.flush() == 3
    assert [(len(p["events"]), len(p.get("activity", []))) for p in payloads] == [(1, 0), (0, 2)]
    call = payloads[1]["activity"][0]
    assert (call["source_id"], call["kind"], call["name"], call["host"], call["model"]) == ("tool:c1", "tool_call", "Bash", "laptop", "claude-opus-5-5")
    assert call["project"] == hashed_project("secret-repo", settings.project_salt)
    assert database.pending_forward_count() == 0
    assert forwarder.flush() == 0 and len(payloads) == 2


def test_forwarder_keeps_activity_for_a_remote_that_does_not_confirm_it(linked, monkeypatch) -> None:
    settings, database = linked
    database.insert_events([UsageEvent(source_id="e1", provider="claude", host="laptop", prompt_tokens=1)])
    database.insert_activity([ActivityEvent(source_id="tool:c1", kind="tool_call", name="Bash")])
    payloads = _server(monkeypatch, b'{"accepted": 1, "received": 1}')  # a tracker from before activity collection
    forwarder = Forwarder(settings, database)

    assert forwarder.flush() == 1  # the usage event went
    assert database.pending_forward_count() == 1 and len(database.pending_activity_forward()) == 1
    database.insert_events([UsageEvent(source_id="e2", provider="claude", host="laptop", prompt_tokens=1)])
    forwarder.notify()
    assert forwarder.flush() == 1
    assert len(payloads) == 3 and "activity" not in payloads[2]  # not offered again inside the hour

    payloads.clear()
    forwarder._activity_after = 0.0  # the hour is over and the remote is updated
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=0: payloads.append(json.loads(request.data)) or _Response(b'{"activity": 1}'))
    forwarder.notify()
    assert forwarder.flush() == 1 and database.pending_forward_count() == 0
