from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    pricing_file: Path | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    poll_seconds: float = 2.0
    # Multi-machine settings (see ~/.tokentracker/config.json and `tracker link`).
    hostname: str = ""
    config_file: Path | None = None
    ingest_token_file: Path | None = None
    remote_url: str | None = None
    remote_token: str | None = None
    project_labels: str = "clear"  # "clear" | "hash": how project names are sent to the remote
    project_salt: str = ""


CONFIG_KEYS = ("host_label", "remote_url", "remote_token", "project_labels", "project_salt")


def load_config(data_dir: Path) -> dict[str, Any]:
    """Read ~/.tokentracker/config.json; missing or invalid files count as empty."""
    path = data_dir / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data_dir: Path, **updates: Any) -> Path:
    """Merge *updates* into config.json (a None value removes the key) and return its path."""
    data_dir.mkdir(parents=True, exist_ok=True)
    data = load_config(data_dir)
    for key, value in updates.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path = data_dir / "config.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def get_settings() -> Settings:
    data_dir = Path(os.environ.get("TOKEN_TRACKER_HOME", "~/.tokentracker")).expanduser()
    claude_dir = Path(os.environ.get("TOKEN_TRACKER_CLAUDE_DIR", "~/.claude/projects")).expanduser()
    cline_db = _env_path("TOKEN_TRACKER_CLINE_DB") or _discover_cline_db()
    cline_task_history = _env_path("TOKEN_TRACKER_CLINE_TASK_HISTORY") or _discover_cline_task_history()
    hermes_db = _env_path("TOKEN_TRACKER_HERMES_DB") or Path(_HERMES_STATE_DB_DEFAULT).expanduser()
    pricing_file = _env_path("TOKEN_TRACKER_PRICING") or data_dir / "pricing.json"
    port = int(os.environ.get("TOKEN_TRACKER_PORT", DEFAULT_PORT))
    poll_seconds = float(os.environ.get("TOKEN_TRACKER_POLL_SECONDS", "2"))
    config = load_config(data_dir)
    remote_url = os.environ.get("TOKEN_TRACKER_REMOTE_URL") or config.get("remote_url") or None
    return Settings(
        hostname=os.environ.get("TOKEN_TRACKER_HOSTNAME") or config.get("host_label") or socket.gethostname(),
        config_file=data_dir / "config.json",
        ingest_token_file=_env_path("TOKEN_TRACKER_INGEST_TOKEN_FILE") or data_dir / "ingest_token",
        remote_url=remote_url.rstrip("/") if remote_url else None,
        remote_token=os.environ.get("TOKEN_TRACKER_REMOTE_TOKEN") or config.get("remote_token") or None,
        project_labels=os.environ.get("TOKEN_TRACKER_PROJECT_LABELS") or config.get("project_labels") or "clear",
        project_salt=str(config.get("project_salt") or ""),
        data_dir=data_dir,
        db_path=data_dir / "usage.db",
        claude_dir=claude_dir,
        cline_db=cline_db,
        cline_task_history=cline_task_history,
        hermes_db=hermes_db,
        pricing_file=pricing_file,
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
