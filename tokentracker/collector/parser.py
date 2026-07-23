from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from tokentracker.collector.models import UsageEvent
from tokentracker.shared.constants import CLAUDE_PROVIDER
from tokentracker.shared.pricing import estimate_cost_usd


TOKEN_KEYS = {
    "prompt_tokens": "input_tokens",
    "completion_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "cache_write_tokens": "cache_creation_input_tokens",
    "reasoning_tokens": "reasoning_tokens",
}


def parse_claude_jsonl(path: Path, root: Path | None = None) -> Iterable[UsageEvent]:
    root = root or path.parent
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            event = parse_claude_record(payload, path, line_no, root)
            if event is not None:
                yield event


def parse_claude_record(payload: dict, path: Path, line_no: int, root: Path) -> UsageEvent | None:
    message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None

    token_counts = {field: _int(usage.get(key)) for field, key in TOKEN_KEYS.items()}
    total_tokens = sum(token_counts.values())
    if total_tokens == 0:
        return None

    timestamp = _parse_timestamp(payload.get("timestamp") or message.get("timestamp"))
    model = str(message.get("model") or payload.get("model") or "unknown")
    conversation_id = _first_string(payload, "sessionId", "session_id", "conversation_id", "conversationId")
    thread_id = _first_string(payload, "uuid", "requestId", "request_id") or conversation_id
    project = _project_from_path(path, root)
    source_id = f"{path.resolve()}:{line_no}"
    cost_usd = estimate_cost_usd(
        model=model,
        prompt_tokens=token_counts["prompt_tokens"],
        completion_tokens=token_counts["completion_tokens"],
        cache_read_tokens=token_counts["cache_read_tokens"],
        cache_write_tokens=token_counts["cache_write_tokens"],
    )

    metadata = {
        "cwd": payload.get("cwd"),
        "type": payload.get("type"),
        "role": message.get("role") if isinstance(message, dict) else None,
        "source_file": str(path),
        "line": line_no,
    }

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
        cost_usd=cost_usd,
        metadata_json=json.dumps(metadata, separators=(",", ":")),
    )


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _first_string(payload: dict, *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _project_from_path(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path.name
        return str(relative)
    return relative.parts[0] if relative.parts else path.stem
