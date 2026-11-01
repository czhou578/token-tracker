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
