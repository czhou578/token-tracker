from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
CLAUDE_PROVIDER = "claude"
CLINE_PROVIDER = "cline"
HERMES_PROVIDER = "hermes"

# Hermes keeps its conversations and token usage in a SQLite state database.
_HERMES_STATE_DB_DEFAULT = "~/.hermes/state.db"

# Candidate locations where Cline stores its SQLite message database.
# - VS Code extension: `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/...`
# - VS Code Server (remote): `~/.vscode-server/data/User/globalStorage/saoudrizwan.claude-dev/...`
# - Cline CLI: `~/.config/cline/...` or `~/.cline/...`
_CLINE_CANDIDATE_DIRS = (
    "~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/sqlite",
    "~/.config/Code/User/globalStorage/saoudrizwan.claude-dev",
    "~/.vscode-server/data/User/globalStorage/saoudrizwan.claude-dev/sqlite",
    "~/.vscode-server/data/User/globalStorage/saoudrizwan.claude-dev",
    "~/.config/cline/sqlite",
    "~/.config/cline",
    "~/.cline/sqlite",
    "~/.cline",
)

_CLINE_DB_NAMES = ("cline.db", "cline.sqlite", "database.sqlite", "db.sqlite")

# Cline 4.x stores per-task token aggregates in `state/taskHistory.json`
# inside its globalStorage directory.
_CLINE_TASK_HISTORY_RELATIVE = "state/taskHistory.json"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    claude_dir: Path
    cline_db: Path | None = None
    cline_task_history: Path | None = None
    hermes_db: Path | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    poll_seconds: float = 2.0


def get_settings() -> Settings:
    data_dir = Path(os.environ.get("TOKEN_TRACKER_HOME", "~/.tokentracker")).expanduser()
    claude_dir = Path(os.environ.get("TOKEN_TRACKER_CLAUDE_DIR", "~/.claude/projects")).expanduser()
    cline_db = _env_path("TOKEN_TRACKER_CLINE_DB") or _discover_cline_db()
    cline_task_history = _env_path("TOKEN_TRACKER_CLINE_TASK_HISTORY") or _discover_cline_task_history()
    hermes_db = _env_path("TOKEN_TRACKER_HERMES_DB") or Path(_HERMES_STATE_DB_DEFAULT).expanduser()
    port = int(os.environ.get("TOKEN_TRACKER_PORT", DEFAULT_PORT))
    poll_seconds = float(os.environ.get("TOKEN_TRACKER_POLL_SECONDS", "2"))
    return Settings(
        data_dir=data_dir,
        db_path=data_dir / "usage.db",
        claude_dir=claude_dir,
        cline_db=cline_db,
        cline_task_history=cline_task_history,
        hermes_db=hermes_db,
        host=os.environ.get("TOKEN_TRACKER_HOST", DEFAULT_HOST),
        port=port,
        poll_seconds=poll_seconds,
    )


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    return Path(value).expanduser()


def _discover_cline_db() -> Path | None:
    """Locate Cline's SQLite message database in the usual install locations."""
    for candidate in _CLINE_CANDIDATE_DIRS:
        folder = Path(candidate).expanduser()
        if not folder.is_dir():
            continue
        for name in _CLINE_DB_NAMES:
            path = folder / name
            if path.is_file():
                return path
    return None


def _discover_cline_task_history() -> Path | None:
    """Locate Cline's `taskHistory.json` (4.x task-level token aggregates)."""
    for candidate in _CLINE_CANDIDATE_DIRS:
        folder = Path(candidate).expanduser()
        if not folder.is_dir():
            continue
        path = folder / _CLINE_TASK_HISTORY_RELATIVE
        if path.is_file():
            return path
    return None
