from __future__ import annotations

import glob
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from tokentracker.collector.config import CLAUDE_PROVIDER, CLINE_PROVIDER, HERMES_PROVIDER
from tokentracker.collector.models import ActivityEvent, UsageEvent


TOKEN_KEYS = {
    "prompt_tokens": "input_tokens",
    "completion_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "cache_write_tokens": "cache_creation_input_tokens",
    "reasoning_tokens": "reasoning_tokens",
}


def parse_claude_jsonl(path: Path, root: Path | None = None) -> list[UsageEvent]:
    """Return one usage event per API response in a Claude Code transcript.

    Claude Code writes one line per content block (thinking, text, each
    tool_use), and every line repeats the response's `message.usage`. Only the
    first line of each response (same `message.id`, else `requestId`) is kept,
    so `source_id` stays `path:line_no` of that first line.
    """
    return ClaudeTranscript(path, root or path.parent).read()


class ClaudeTranscript:
    """Reads one Claude Code transcript incrementally.

    Claude Code only appends to a transcript, so each `read` parses just the
    lines added since the previous one, and the collector never reads a long
    session's file from the top again. Only complete lines are consumed; a
    line still being written is parsed again on the next read under the same
    line number. When the file shrinks, is replaced, or no longer ends a line
    where the last read stopped, the next read starts over from the top (the
    database ignores rows it already has).

    Each read also collects the activity of the new lines in `activity`: the
    tool calls, the replies and the scheduled runs (`_claude_activity`).
    """

    def __init__(self, path: Path, root: Path):
        self.path = path
        self.activity: list[ActivityEvent] = []  # found by the last read
        # Computed once: resolve() costs a syscall per path component.
        self._source = str(path.resolve())
        self._project = _project_from_path(path, root)
        self.reset()

    def reset(self) -> None:
        """Forget all progress, so the next read starts from the top."""
        self.signature: tuple[int, int, int] | None = None  # (inode, size, mtime_ns) at the last read
        self._offset = 0  # bytes consumed, always just past a newline
        self._line_no = 0  # lines consumed, so source ids match a full parse
        self._seen: set[str] = set()  # response ids already counted

    def changed(self, stat: os.stat_result) -> bool:
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns) != self.signature

    def read(self, stat: os.stat_result | None = None) -> list[UsageEvent]:
        """Parse the lines appended since the last read and return their events."""
        stat = stat or self.path.stat()
        if self.signature is not None and (stat.st_ino != self.signature[0] or stat.st_size < self._offset):
            self.reset()
        events: list[UsageEvent] = []
        self.activity = []
        with self.path.open("rb") as handle:
            if self._offset:
                handle.seek(self._offset - 1)
                if handle.read(1) != b"\n":  # rewritten in place
                    self.reset()
                    handle.seek(0)
            line_no = self._line_no
            for raw in handle:
                line_no += 1
                event = self._parse(raw, line_no)
                if event is not None:
                    events.append(event)
                if not raw.endswith(b"\n"):
                    break  # still being written; a complete line here was counted once via _seen
                self._offset += len(raw)
                self._line_no = line_no
        self.signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        return events

    def _parse(self, raw: bytes, line_no: int) -> UsageEvent | None:
        line = raw.decode("utf-8", errors="ignore")
        if not line.strip():
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        # Before the response check: the tool calls are on the later lines of a response.
        self.activity.extend(_claude_activity(payload, self._project))
        response_id = _claude_response_id(payload)
        if response_id is not None:
            if response_id in self._seen:
                return None
            self._seen.add(response_id)
        return _claude_event(payload, f"{self._source}:{line_no}", self._project)


def _claude_response_id(payload: dict) -> str | None:
    message = payload.get("message")
    if isinstance(message, dict):
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id:
            return message_id
    request_id = payload.get("requestId")
    return request_id if isinstance(request_id, str) and request_id else None


def parse_claude_record(payload: dict, path: Path, line_no: int, root: Path) -> UsageEvent | None:
    return _claude_event(payload, f"{path.resolve()}:{line_no}", _project_from_path(path, root))


def _claude_event(payload: dict, source_id: str, project: str | None) -> UsageEvent | None:
    message = payload.get("message")
    if not isinstance(message, dict):
        message = None
    usage = message.get("usage") if message is not None else payload.get("usage")
    if not isinstance(usage, dict):
        return None

    token_counts = {field: _int(usage.get(key)) for field, key in TOKEN_KEYS.items()}
    total_tokens = sum(token_counts.values())
    if total_tokens == 0:
        return None

    message = message or {}
    timestamp = _parse_timestamp(payload.get("timestamp") or message.get("timestamp"))
    model = str(message.get("model") or payload.get("model") or "unknown")
    conversation_id = _first_string(payload, "sessionId", "session_id", "conversation_id", "conversationId")
    # A thread is the Claude Code session; `uuid` is unique per line and would count every message.
    thread_id = conversation_id or _first_string(payload, "requestId", "request_id", "uuid")

    return UsageEvent(
        source_id=source_id,
        timestamp=timestamp,
        thread_id=thread_id,
        conversation_id=conversation_id,
        project=project,
        provider=CLAUDE_PROVIDER,
        model=model,
        **token_counts,
        total_tokens=total_tokens,
        metadata_json=_dumps(_claude_pricing_metadata(usage, message)),
    )


TOOL_CALL_BLOCKS = ("tool_use", "server_tool_use")


def _claude_activity(payload: dict, project: str | None) -> list[ActivityEvent]:
    """Return the activity of one transcript line.

    - A tool call is a `tool_use` block of an assistant line. Its id is unique
      everywhere, so a session that Claude Code copies to a second file counts once.
    - A reply is an assistant message with `stop_reason` "end_turn" in the main
      chain. A subagent answers the model, not the user, so its final message
      does not count. Every line of the message repeats the message id.
    - A scheduled run is a `scheduled_task_fire` system line.
    """
    line_type = payload.get("type")
    if line_type == "system":
        line_id = payload.get("uuid")
        if payload.get("subtype") != "scheduled_task_fire" or not isinstance(line_id, str) or not line_id:
            return []
        found = [("scheduled_run", f"run:{line_id}", None)]
        model = None
    elif line_type == "assistant" and isinstance(payload.get("message"), dict):
        message = payload["message"]
        model = _first_string(message, "model")
        content = message.get("content")
        found = [
            ("tool_call", f"tool:{block['id']}", _first_string(block, "name") or "unknown")
            for block in (content if isinstance(content, list) else ())
            if isinstance(block, dict) and block.get("type") in TOOL_CALL_BLOCKS
            and isinstance(block.get("id"), str) and block["id"]
        ]
        message_id = _first_string(message, "id")
        if message_id and message.get("stop_reason") == "end_turn" and not payload.get("isSidechain"):
            found.append(("reply", f"reply:{message_id}", None))
    else:
        return []
    if not found:
        return []
    timestamp = _parse_timestamp(payload.get("timestamp"))
    thread_id = _first_string(payload, "sessionId", "session_id")
    return [
        ActivityEvent(
            source_id=source_id, kind=kind, name=name, timestamp=timestamp, thread_id=thread_id,
            project=project, provider=CLAUDE_PROVIDER, model=model,
        )
        for kind, source_id, name in found
    ]


def _claude_pricing_metadata(usage: dict, message: object = None) -> dict:
    """Keep the API message id and the usage fields that change the price of a request.

    - `cache_creation.ephemeral_1h_input_tokens`: 1-hour cache writes cost 2x
      the input rate, 5-minute writes 1.25x.
    - `speed`: fast mode has its own rates.
    - `inference_geo`: US-only inference costs 1.1x.
    - `server_tool_use.web_search_requests`: each search costs $0.01.
    """
    metadata: dict = {}
    message_id = _first_string(message, "id") if isinstance(message, dict) else None
    if message_id:
        metadata["message_id"] = message_id
    cache_creation = usage.get("cache_creation")
    if isinstance(cache_creation, dict):
        write_1h = _int(cache_creation.get("ephemeral_1h_input_tokens"))
        if write_1h:
            metadata["cache_write_1h_tokens"] = write_1h
    speed = usage.get("speed")
    if isinstance(speed, str) and speed and speed != "standard":
        metadata["speed"] = speed
    geo = usage.get("inference_geo")
    if isinstance(geo, str) and geo and geo != "global":
        metadata["inference_geo"] = geo
    server_tools = usage.get("server_tool_use")
    if isinstance(server_tools, dict):
        searches = _int(server_tools.get("web_search_requests"))
        if searches:
            metadata["web_search_requests"] = searches
    return metadata


def _dumps(metadata: dict) -> str:
    return json.dumps(metadata, separators=(",", ":")) if metadata else "{}"


# ── Gateway usage log ────────────────────────────────────────────────────
# A gateway between Claude Code and a local model server (for example
# colin-inference-engine `engine/server/gateway.py --usage-log`) appends one
# JSON line per response: {"ts", "id", "model", "dest", "session", "usage"}.
# Claude Code writes the usage of its main turns to the transcript, but not the
# usage of side requests such as the session title. The collector keeps only
# the lines whose message id no transcript holds (service.py).

GATEWAY_SOURCE_PREFIX = "gateway:"


class GatewayLog:
    """Reads the gateway usage log incrementally, at most `limit` bytes per read.

    The gateway only appends, so `offset` is always just past a newline. When
    the file shrinks or is replaced (another inode), reading starts over from
    the top (the database ignores rows it already has).
    """

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.inode: int | None = None

    def read(self, limit: int = 1 << 22) -> list[tuple[int, UsageEvent]]:
        """Return the events of the complete lines after `offset`, each with the offset where its line starts."""
        try:
            stat = self.path.stat()
        except OSError:
            return []
        if (self.inode is not None and stat.st_ino != self.inode) or stat.st_size < self.offset:
            self.offset = 0
        self.inode = stat.st_ino
        if stat.st_size == self.offset:
            return []
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            data = handle.read(limit)
        end = data.rfind(b"\n") + 1
        if not end and len(data) == limit:
            end = len(data)  # a line longer than `limit` is not a usage line: skip it, do not stop here
        events: list[tuple[int, UsageEvent]] = []
        start = self.offset
        for raw in data[:end].splitlines(keepends=True):
            event = _gateway_line(raw)
            if event is not None:
                events.append((start, event))
            start += len(raw)
        self.offset += end
        return events


def _gateway_line(raw: bytes) -> UsageEvent | None:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parse_gateway_record(payload) if isinstance(payload, dict) else None


def parse_gateway_record(payload: dict) -> UsageEvent | None:
    usage, message_id = payload.get("usage"), payload.get("id")
    if not isinstance(usage, dict) or not isinstance(message_id, str) or not message_id:
        return None
    token_counts = {field: _int(usage.get(key)) for field, key in TOKEN_KEYS.items()}
    total_tokens = sum(token_counts.values())
    if total_tokens == 0:
        return None
    session = payload.get("session") if isinstance(payload.get("session"), str) else None
    return UsageEvent(
        source_id=GATEWAY_SOURCE_PREFIX + message_id,
        timestamp=_parse_timestamp(payload.get("ts")),
        thread_id=session,
        conversation_id=session,
        provider=CLAUDE_PROVIDER,
        model=str(payload.get("model") or "unknown"),
        **token_counts,
        total_tokens=total_tokens,
        metadata_json=_dumps({"message_id": message_id, "via": "gateway"}),
    )


def session_project(root: Path, session: str | None) -> str | None:
    """Return the project of the transcript of a Claude Code session (`<root>/<project>/<session>.jsonl`)."""
    if not session:
        return None
    for path in root.glob(f"*/{glob.escape(session)}.jsonl"):
        return _project_from_path(path, root)
    return None


# ── Cline ────────────────────────────────────────────────────────────────
# Cline writes its conversation history into a SQLite database. The
# `cline_messages` table stores each message as JSON in a `message` column.
# The token usage lives inside that JSON (camelCase or snake_case keys).

CLINE_TOKEN_ALIASES = {
    "prompt_tokens": ("input_tokens", "inputTokenCount", "prompt_tokens", "prompt_tokens_count"),
    "completion_tokens": ("output_tokens", "outputTokenCount", "completion_tokens", "completion_tokens_count"),
    "cache_read_tokens": ("cache_read_input_tokens", "cacheReadInputTokens", "cache_read_tokens"),
    "cache_write_tokens": ("cache_creation_input_tokens", "cacheCreationInputTokens", "cache_write_tokens"),
    "reasoning_tokens": ("reasoning_tokens", "reasoningTokens"),
}


def parse_cline_sqlite(path: Path) -> Iterable[UsageEvent]:
    """Yield UsageEvent records from Cline's SQLite message database."""
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        table_names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "cline_messages" not in table_names:
            return
        columns = [row[1] for row in connection.execute("PRAGMA table_info(cline_messages)")]
        rows = list(connection.execute("SELECT * FROM cline_messages"))
    finally:
        connection.close()

    message_col = "message" if "message" in columns else columns[1]
    id_col = "id" if "id" in columns else columns[0]
    timestamp_col = "timestamp" if "timestamp" in columns else None

    for row in rows:
        raw = row[message_col]
        payload = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(payload, dict):
            continue
        source_id = f"{path.resolve()}:{row[id_col]}"
        timestamp = row[timestamp_col] if timestamp_col else None
        event = parse_cline_record(payload, source_id, timestamp)
        if event is not None:
            yield event


def parse_cline_record(payload: dict, source_id: str, timestamp: object = None) -> UsageEvent | None:
    usage = _extract_cline_usage(payload)
    if not isinstance(usage, dict):
        return None

    token_counts = {field: _first_int(usage, aliases) for field, aliases in CLINE_TOKEN_ALIASES.items()}
    total_tokens = sum(token_counts.values())
    if total_tokens == 0:
        return None

    message = payload.get("message")
    inner = message.get("message") if isinstance(message, dict) else None
    model = (
        _first_string(payload, "model")
        or _first_string(message, "model")
        or _first_string(inner, "model")
        or "unknown"
    )
    parsed_timestamp = _parse_timestamp(
        timestamp or _first_string(payload, "timestamp") or _first_string(message, "timestamp")
    )
    thread_id = _first_string(payload, "taskId", "sessionId", "conversation_id", "conversationId")

    return UsageEvent(
        source_id=source_id,
        timestamp=parsed_timestamp,
        thread_id=thread_id,
        provider=CLINE_PROVIDER,
        model=model,
        **token_counts,
        total_tokens=total_tokens,
    )


def parse_cline_task_history(path: Path) -> Iterable[UsageEvent]:
    """Parse Cline's `taskHistory.json` — the 4.x task-level token aggregate format.

    Each record is a single task (one API conversation) with aggregate usage:

        {
          "id": "1785782429653",
          "ulid": "01KZ4ETSZ1KC1Y6V7B02RR11DT",
          "ts": 1785782943608,          # epoch milliseconds
          "task": "write a poem",
          "tokensIn": 73347,
          "tokensOut": 1881,
          "cacheWrites": 0,
          "cacheReads": 43930,
          "totalCost": 0,
          "size": 55944,
          "cwdOnTaskInitialization": "/home/.../blog",
          "isFavorited": false,
          "modelId": "deepseek-v4-flash"
        }
    """
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            records = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(records, list):
        return

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        token_counts = {
            "prompt_tokens": _int(record.get("tokensIn")),
            "completion_tokens": _int(record.get("tokensOut")),
            "cache_read_tokens": _int(record.get("cacheReads")),
            "cache_write_tokens": _int(record.get("cacheWrites")),
        }
        total_tokens = sum(token_counts.values())
        if total_tokens == 0:
            continue

        ts = record.get("ts")
        if isinstance(ts, (int, float)):
            try:
                timestamp = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                timestamp = datetime.now(timezone.utc)
        else:
            timestamp = _parse_timestamp(ts)

        project = _project_from_cwd(record.get("cwdOnTaskInitialization"))
        thread_id = _first_string(record, "ulid", "id")
        model = _first_string(record, "modelId", "model") or "unknown"
        source_id = f"{path.resolve()}:{record.get('id') or index}"
        metadata: dict = {}
        reported_cost = _float(record.get("totalCost"))
        if reported_cost > 0:
            metadata["reported_cost_usd"] = reported_cost

        yield UsageEvent(
            source_id=source_id,
            timestamp=timestamp,
            thread_id=thread_id,
            project=project,
            provider=CLINE_PROVIDER,
            model=model,
            **token_counts,
            total_tokens=total_tokens,
            metadata_json=_dumps(metadata),
        )


HERMES_REQUIRED_COLUMNS = frozenset({"id", "model", "started_at", "input_tokens", "output_tokens"})
HERMES_COLUMNS = (
    "id", "source", "model", "title", "started_at", "ended_at", "cwd", "git_repo_root",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
    "estimated_cost_usd", "actual_cost_usd", "cost_source",
)


def parse_hermes_state_db(path: Path) -> Iterable[UsageEvent]:
    """Parse Hermes token usage from `~/.hermes/state.db` (SQLite).

    Each row in the `sessions` table is one conversation with its aggregate
    token usage — the analog to Cline's `taskHistory.json`. Open sessions
    (ended_at IS NULL) are included so usage shows up while Hermes runs;
    their counts grow over time, so the collector writes them with
    `UsageDatabase.upsert_events`. Hermes closes a session about a day after
    it starts, so waiting for `ended_at` would hide a day of usage.
    """
    if not path.is_file():
        return
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        available = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
        if not HERMES_REQUIRED_COLUMNS <= available:
            return
        columns = [column for column in HERMES_COLUMNS if column in available]
        rows = list(connection.execute(f"SELECT {', '.join(columns)} FROM sessions"))
    except sqlite3.Error:
        return
    finally:
        connection.close()

    for row in rows:
        event = _hermes_record(row)
        if event is not None:
            yield event


def _hermes_record(row: sqlite3.Row) -> UsageEvent | None:
    present = set(row.keys())

    def column(name: str) -> object:
        return row[name] if name in present else None

    token_counts = {
        "prompt_tokens": _int(column("input_tokens")),
        "completion_tokens": _int(column("output_tokens")),
        "cache_read_tokens": _int(column("cache_read_tokens")),
        "cache_write_tokens": _int(column("cache_write_tokens")),
        "reasoning_tokens": _int(column("reasoning_tokens")),
    }
    total_tokens = sum(token_counts.values())
    if total_tokens == 0:
        return None

    timestamp = _from_epoch_seconds(row["started_at"])

    thread_id = str(row["id"])
    model = str(row["model"] or "unknown")
    project = _project_from_cwd(column("git_repo_root") or column("cwd"))

    metadata = {
        "title": column("title"),
        "source": column("source"),
        "cwd": column("cwd"),
        "git_repo_root": column("git_repo_root"),
        "ended_at": column("ended_at"),
    }
    # Hermes prices sessions itself for hosted providers; a local vLLM server reports 0.
    reported_cost = _float(column("actual_cost_usd")) or _float(column("estimated_cost_usd"))
    if reported_cost > 0:
        metadata["reported_cost_usd"] = reported_cost
        metadata["hermes_cost_source"] = column("cost_source")

    return UsageEvent(
        source_id=f"hermes:{thread_id}",
        timestamp=timestamp,
        thread_id=thread_id,
        conversation_id=thread_id,
        project=project,
        provider=HERMES_PROVIDER,
        model=model,
        **token_counts,
        total_tokens=total_tokens,
        metadata_json=json.dumps(metadata),
    )


HERMES_MESSAGE_COLUMNS = frozenset(
    {"id", "session_id", "role", "tool_name", "tool_call_id", "tool_calls", "finish_reason", "timestamp", "content"}
)
HERMES_SESSION_COLUMNS = frozenset({"id", "source", "model", "started_at", "cwd", "git_repo_root"})
_HERMES_REPLY = "m.role = 'assistant' AND m.tool_calls IS NULL AND m.finish_reason IS 'stop'"
HERMES_ACTIVITY_SQL = f"""
SELECT m.id, m.session_id, m.role, m.tool_name, m.tool_call_id, m.timestamp,
       ({_HERMES_REPLY}) AS is_reply,
       CASE WHEN {_HERMES_REPLY} THEN m.content END AS content,
       s.source, s.model, s.started_at, s.cwd, s.git_repo_root
FROM messages m JOIN sessions s ON s.id = m.session_id
WHERE m.id > ? AND (m.role = 'tool' OR s.source = 'cron' OR ({_HERMES_REPLY}))
ORDER BY m.id
"""


def parse_hermes_activity(path: Path, after: int = 0) -> tuple[list[ActivityEvent], int]:
    """Return the Hermes activity of the `messages` rows with an id above *after*, and the highest id read.

    - A tool call is a row with role `tool`. When Hermes compacts a session it
      writes some rows again, with a new id and a new time, so the key is the
      session and the `tool_call_id`.
    - A reply is an assistant row without tool calls and with finish reason
      `stop`. Other finish reasons are steps of a turn, and a row without one
      is a compaction summary. The key holds a hash of the text, because a
      compaction writes replies again too. A subagent session answers the
      model, not the user, so it has no replies.
    - A scheduled run is a session with source `cron`. It is reported with the
      first row read from that session (the prompt of the schedule), at the
      start time of the session.

    When the newest row has an id below *after*, the file was replaced and the
    read starts over from the first row (the database ignores rows it already
    has). When there is no usable `messages` table, the result is empty and
    the id is 0.
    """
    if not path.is_file():
        return [], 0
    events: list[ActivityEvent] = []
    newest = after
    runs: set[str] = set()
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        messages = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
        sessions = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
        if not HERMES_MESSAGE_COLUMNS <= messages or not HERMES_SESSION_COLUMNS <= sessions:
            return [], 0
        if (connection.execute("SELECT MAX(id) FROM messages").fetchone()[0] or 0) < after:
            after = newest = 0
        for row in connection.execute(HERMES_ACTIVITY_SQL, (after,)):
            newest = row["id"]
            session = str(row["session_id"])
            common = {
                "thread_id": session,
                "project": _project_from_cwd(row["git_repo_root"] or row["cwd"]),
                "provider": HERMES_PROVIDER,
                "model": str(row["model"] or "unknown"),
            }
            if row["source"] == "cron" and session not in runs:
                runs.add(session)
                events.append(ActivityEvent(
                    source_id=f"hermes:run:{session}", kind="scheduled_run",
                    timestamp=_from_epoch_seconds(row["started_at"]), **common,
                ))
            timestamp = _from_epoch_seconds(row["timestamp"])
            if row["role"] == "tool":
                call_id = row["tool_call_id"] or row["id"]
                events.append(ActivityEvent(
                    source_id=f"hermes:tool:{session}:{call_id}", kind="tool_call",
                    name=str(row["tool_name"] or "unknown"), timestamp=timestamp, **common,
                ))
            elif row["is_reply"] and row["source"] != "subagent":
                digest = hashlib.sha256(str(row["content"] or "").encode("utf-8")).hexdigest()[:16]
                events.append(ActivityEvent(
                    source_id=f"hermes:reply:{session}:{digest}", kind="reply", timestamp=timestamp, **common,
                ))
    except sqlite3.Error:
        return [], after  # locked or half-written: read the same rows on the next change
    finally:
        connection.close()
    return events, newest


def _from_epoch_seconds(value: object) -> datetime:
    if isinstance(value, (int, float)) and value:
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return datetime.now(timezone.utc)
    return _parse_timestamp(value)


def _extract_cline_usage(payload: dict) -> object:
    """Locate the `usage` dict inside Cline's message JSON (handles nesting)."""
    message = payload.get("message")
    if isinstance(message, dict):
        # Cline sometimes wraps the message: { "message": { "message": { "usage": ... } } }
        usage = message.get("usage") or (message.get("message") or {}).get("usage")
        if isinstance(usage, dict):
            return usage
    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage
    return None


def _first_int(container: dict, keys: tuple[str, ...]) -> int:
    for key in keys:
        value = container.get(key)
        parsed = _int(value)
        if parsed:
            return parsed
    return 0


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _first_string(payload: dict | None, *keys: str) -> str | None:
    if payload is None:
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, str) and value:
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).astimezone(timezone.utc)
        except ValueError:
            pass
    if isinstance(value, (int, float)) and value:
        try:
            # Cline stores `ts` as epoch milliseconds
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now(timezone.utc)


def _project_from_path(path: Path, root: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return path.stem
    return relative.parts[0] if relative.parts else path.stem


def _project_from_cwd(cwd: object) -> str | None:
    """Derive a project label from a Cline `cwdOnTaskInitialization` path."""
    if not isinstance(cwd, str) or not cwd:
        return None
    parts = Path(cwd).expanduser().parts
    if not parts:
        return None
    # Prefer the last meaningful component (the project folder name).
    return parts[-1]
