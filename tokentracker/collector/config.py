from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
CLAUDE_PROVIDER = "claude"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    claude_dir: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    poll_seconds: float = 2.0


def get_settings() -> Settings:
    data_dir = Path(os.environ.get("TOKEN_TRACKER_HOME", "~/.tokentracker")).expanduser()
    claude_dir = Path(os.environ.get("TOKEN_TRACKER_CLAUDE_DIR", "~/.claude/projects")).expanduser()
    port = int(os.environ.get("TOKEN_TRACKER_PORT", DEFAULT_PORT))
    poll_seconds = float(os.environ.get("TOKEN_TRACKER_POLL_SECONDS", "2"))
    return Settings(
        data_dir=data_dir,
        db_path=data_dir / "usage.db",
        claude_dir=claude_dir,
        host=os.environ.get("TOKEN_TRACKER_HOST", DEFAULT_HOST),
        port=port,
        poll_seconds=poll_seconds,
    )