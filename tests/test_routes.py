from __future__ import annotations

from tokentracker.api.routes import authorized, load_ingest_token
from tokentracker.collector.config import get_settings


def test_authorized_requires_matching_bearer_token() -> None:
    assert authorized("Bearer abc", "abc")
    assert authorized("bearer abc ", "abc")
    assert not authorized("Bearer abd", "abc")
    assert not authorized("Basic abc", "abc")
    assert not authorized(None, "abc")
    assert not authorized("", "abc")


def test_load_ingest_token_prefers_env_then_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TOKEN_TRACKER_HOME", str(tmp_path))
    monkeypatch.delenv("TOKEN_TRACKER_INGEST_TOKEN", raising=False)
    settings = get_settings()
    assert load_ingest_token(settings) is None
    settings.ingest_token_file.write_text("from-file\n")
    assert load_ingest_token(settings) == "from-file"
    monkeypatch.setenv("TOKEN_TRACKER_INGEST_TOKEN", "from-env")
    assert load_ingest_token(settings) == "from-env"


import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_TRACKER_HOME", str(tmp_path))
    monkeypatch.delenv("TOKEN_TRACKER_INGEST_TOKEN", raising=False)
    from tokentracker.api.main import create_app

    return TestClient(create_app()), tmp_path


EVENT = {"source_id": "s:1", "provider": "claude", "model": "claude-opus-5-5", "prompt_tokens": 10, "timestamp": "2026-10-31T10:00:00Z"}


def test_ingest_is_404_until_a_token_exists(client) -> None:
    http, _ = client
    assert http.post("/api/events", json={"host": "laptop", "events": [EVENT]}).status_code == 404


def test_ingest_checks_the_token_before_reading_the_body(client) -> None:
    http, home = client
    (home / "ingest_token").write_text("secret\n")

    # No token, malformed body: 401, not 422, so the format is not revealed.
    response = http.post("/api/events", content=b"{not json", headers={"Content-Type": "application/json"})
    assert response.status_code == 401
    assert "events" not in response.text
    assert http.post("/api/events", json={"host": "laptop", "events": [EVENT]}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert http.post("/api/events", headers={"Authorization": "Bearer secret", "Content-Length": str(64 * 1024 * 1024)}, content=b"").status_code == 413

    bad = http.post("/api/events", content=b"{}", headers={"Authorization": "Bearer secret", "Content-Type": "application/json"})
    assert bad.status_code == 422

    ok = http.post("/api/events", json={"host": "laptop", "events": [EVENT]}, headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200 and ok.json() == {"accepted": 1, "received": 1}
    hosts = {row["host"]: row["events"] for row in http.get("/api/hosts").json()}
    assert hosts["laptop"] == 1


def test_series_endpoint_uses_the_tz_parameter(client) -> None:
    http, _ = client
    response = http.get("/api/series", params={"window": "today", "tz": "Asia/Kolkata"})
    assert response.status_code == 200
    body = response.json()
    assert body["tz"] == "Asia/Kolkata" and body["unit"] == "hour" and len(body["rows"]) == 24
