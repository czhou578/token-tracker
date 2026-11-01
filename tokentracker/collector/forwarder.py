"""Push local usage events to another tracker over HTTPS.

Used on a second machine (a laptop) so its usage shows up on the main tracker.
Events are sent in batches with a bearer token; rows are marked `synced` only
after the server accepts them, so an offline laptop catches up on reconnect.
Only a whitelist of metadata keys leaves the machine, and project names can be
replaced by a salted hash.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict
from typing import Any

from tokentracker.collector.config import Settings
from tokentracker.collector.database import UsageDatabase
from tokentracker.collector.models import UsageEvent

log = logging.getLogger("tokentracker.forwarder")

# Metadata that affects pricing or dedupe; paths, titles and cwd never leave the machine.
METADATA_WHITELIST = (
    "message_id", "cache_write_1h_tokens", "speed", "inference_geo", "web_search_requests",
    "reported_cost_usd", "cost_source", "ended_at",
)
BATCH_SIZE = 500
MAX_BATCHES_PER_FLUSH = 10
BACKOFF_START = 30.0
BACKOFF_MAX = 600.0
META_LAST_OK = "forward_last_ok"
META_LAST_ERROR = "forward_last_error"


def hashed_project(project: str | None, salt: str) -> str | None:
    if not project:
        return project
    digest = hashlib.sha256(f"{salt}:{project}".encode("utf-8")).hexdigest()
    return f"proj-{digest[:10]}"


def export_event(event: UsageEvent, settings: Settings) -> dict[str, Any]:
    """The wire form of an event: pricing metadata only, project hashed when configured."""
    try:
        metadata = json.loads(event.metadata_json or "{}")
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    kept = {key: metadata[key] for key in METADATA_WHITELIST if key in metadata}
    project = event.project
    if settings.project_labels == "hash":
        project = hashed_project(project, settings.project_salt)
    data = asdict(event)
    data["timestamp"] = event.timestamp.isoformat()
    data["project"] = project
    data["host"] = settings.hostname
    data["metadata_json"] = json.dumps(kept, separators=(",", ":")) if kept else "{}"
    return data


class Forwarder:
    def __init__(self, settings: Settings, database: UsageDatabase):
        self.settings = settings
        self.database = database
        self._next_attempt = 0.0
        self._backoff = BACKOFF_START
        self._warned = False
        # Unknown at start, so the first flush looks. After that, only new local
        # events or an unfinished backlog make a flush query the database.
        self._pending = True

    @property
    def enabled(self) -> bool:
        return bool(self.settings.remote_url and self.settings.remote_token)

    def notify(self) -> None:
        """Record that local events were written, so the next flush sends them."""
        self._pending = True

    def flush(self) -> int:
        """Send pending events; return how many the remote accepted this call."""
        if not self.enabled or not self._pending or time.monotonic() < self._next_attempt:
            return 0
        sent = 0
        drained = False
        for _ in range(MAX_BATCHES_PER_FLUSH):
            pending = self.database.pending_forward(BATCH_SIZE)
            if not pending:
                drained = True
                break
            ids = [row_id for row_id, _ in pending]
            payload = {
                "host": self.settings.hostname,
                "events": [export_event(event, self.settings) for _, event in pending],
            }
            try:
                self._post(payload)
            except (OSError, ValueError) as error:  # urllib's URLError and HTTPError are OSErrors
                self._backoff = min(self._backoff * 2, BACKOFF_MAX) if self._warned else BACKOFF_START
                self._next_attempt = time.monotonic() + self._backoff
                self.database.set_meta(META_LAST_ERROR, {"at": time.time(), "error": str(error)[:300]})
                if not self._warned:
                    log.warning("forwarding to %s failed (%s); retrying every %.0f s", self.settings.remote_url, error, self._backoff)
                    self._warned = True
                return sent
            self.database.mark_forwarded(ids)
            sent += len(ids)
            if len(pending) < BATCH_SIZE:
                drained = True
                break
        self._pending = not drained
        if sent:
            self.database.set_meta(META_LAST_OK, {"at": time.time(), "sent": sent})
            if self._warned:
                log.info("forwarding to %s recovered", self.settings.remote_url)
        self._warned = False
        self._backoff = BACKOFF_START
        return sent

    def _post(self, payload: dict[str, Any]) -> None:
        # Imported here: urllib.request loads ssl (about 2.6 MB), and most
        # collectors (the main machine) never forward.
        import urllib.request

        request = urllib.request.Request(
            f"{self.settings.remote_url}/api/events",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.remote_token}",
                "User-Agent": "tokentracker-forwarder",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise ValueError(f"HTTP {response.status}")
