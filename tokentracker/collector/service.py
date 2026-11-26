from __future__ import annotations

import logging
import signal
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tokentracker.collector.config import get_settings
from tokentracker.collector.database import CLAUDE_DEDUPE_WINDOW_SECONDS, UsageDatabase
from tokentracker.collector.parser import (
    GATEWAY_SOURCE_PREFIX,
    ClaudeTranscript,
    GatewayLog,
    parse_cline_sqlite,
    parse_cline_task_history,
    parse_hermes_activity,
    parse_hermes_state_db,
    session_project,
)
from tokentracker.collector.forwarder import Forwarder
from tokentracker.collector.memory import release_memory
from tokentracker.collector.models import UsageEvent
from tokentracker.collector.pricing import Pricing

log = logging.getLogger("tokentracker.collector")

# Meta keys. The first holds the transcript signatures saved at a clean shutdown;
# the second the `Pricing.fingerprint` that stored costs were last computed with.
TRANSCRIPTS_META_KEY = "collector_transcripts_v1"
PRICING_META_KEY = "pricing_fingerprint"
# The read position in the gateway usage log: the start of the first line not yet stored or dropped.
GATEWAY_META_KEY = "collector_gateway_log_v1"
# Set after the collector first ran with activity collection. Until then the saved transcript signatures are not
# used, so the first scan reads every transcript from the top and stores the activity of the lines read before.
ACTIVITY_BACKFILL_META_KEY = "activity_backfill_v1"
# The id of the newest Hermes `messages` row whose activity is stored, with the path of that database.
HERMES_ACTIVITY_META_KEY = "collector_hermes_activity_v1"
# The gateway log holds every response of a local model. The transcripts hold the main turns, written within seconds
# of the response. A gateway line waits this long before it is compared with the stored transcript rows, so a main
# turn counts once, from its transcript, and only the side requests count from the log.
GATEWAY_GRACE_SECONDS = 60

# (mtime_ns, size) of a source file, folded with its SQLite WAL file when one exists.
Signature = tuple[int, int]


def file_signature(path: Path) -> Signature | None:
    """Return a change marker for *path*, or None when the file does not exist.

    SQLite in WAL mode appends to ``<db>-wal`` and leaves the main file alone
    until a checkpoint, so the WAL file's stat is folded into the marker.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    mtime, size = stat.st_mtime_ns, stat.st_size
    try:
        wal = path.with_name(path.name + "-wal").stat()
    except OSError:
        return (mtime, size)
    return (max(mtime, wal.st_mtime_ns), size + wal.st_size)


class Collector:
    def __init__(
        self,
        root: Path | None = None,
        cline_db: Path | None = None,
        cline_task_history: Path | None = None,
        hermes_db: Path | None = None,
    ):
        self.settings = get_settings()
        self.root = root or self.settings.claude_dir
        self.cline_db = cline_db or self.settings.cline_db
        self.cline_task_history = cline_task_history or self.settings.cline_task_history
        self.hermes_db = hermes_db or self.settings.hermes_db
        self.database = UsageDatabase(self.settings.db_path, hostname=self.settings.hostname, persistent=True)
        self.gateway = GatewayLog(self.settings.gateway_log) if self.settings.gateway_log else None
        self._gateway_pending: list[tuple[int, UsageEvent]] = []  # (line offset, event) younger than the grace period
        self._hermes_after: int | None = None  # loaded from the database on the first Hermes read
        self.forwarder = Forwarder(self.settings, self.database)
        self.pricing_file = self.settings.pricing_file
        self.pricing = Pricing.load(self.pricing_file)
        # One incremental reader per transcript, so a poll parses only appended lines.
        self._transcripts: dict[Path, ClaudeTranscript] = {}
        # Change markers so each poll re-parses only the sources that changed.
        self._sources: dict[Path, Signature] = {}
        self._pricing_signature = file_signature(self.pricing_file) if self.pricing_file else None
        # Set when this poll parsed a source or repriced, so the freed memory is released.
        self._parsed = False

    def scan_once(self) -> int:
        self._refresh_pricing()
        prepare = self._prepare
        inserted = self._scan_claude()
        inserted += self._scan_gateway()  # after the transcripts, so this poll's main turns are already stored
        if self._changed(self._sources, self.cline_db):
            inserted += self.database.insert_events(prepare(parse_cline_sqlite(self.cline_db)))
        if self._changed(self._sources, self.cline_task_history):
            inserted += self.database.insert_events(prepare(parse_cline_task_history(self.cline_task_history)))
        if self._changed(self._sources, self.hermes_db):
            # Hermes rows are per-session aggregates that grow while the session
            # is open, so refresh existing rows instead of ignoring them.
            inserted += self.database.upsert_events(prepare(parse_hermes_state_db(self.hermes_db)))
            self._scan_hermes_activity()
        if inserted:
            self.forwarder.notify()
        self.forwarder.flush()
        if self._parsed:
            self._parsed = False
            release_memory()
        return inserted

    def _prepare(self, events):
        """Price each event and stamp it with this machine's name."""
        hostname = self.settings.hostname
        for event in self.pricing.price_events(events):
            yield event if event.host else replace(event, host=hostname)

    def reprice(self) -> int:
        """Recompute every stored cost with the current pricing table, unless that was already done.

        New events are priced as they are stored, so only a change of rates,
        overrides or pricing code needs the full pass over every row.
        """
        fingerprint = self.pricing.fingerprint()
        if self.database.get_meta(PRICING_META_KEY) == fingerprint:
            return 0
        changed = self.database.reprice(self.pricing)
        self.database.set_meta(PRICING_META_KEY, fingerprint)
        if changed:
            log.info("repriced %d stored events", changed)
        return changed

    def _refresh_pricing(self) -> None:
        """Reload the override file when it changes, then reprice stored events."""
        if self.pricing_file is None:
            return
        signature = file_signature(self.pricing_file)
        if signature == self._pricing_signature:
            return
        self._pricing_signature = signature
        self.pricing = Pricing.load(self.pricing_file)
        log.info("pricing overrides %s: %s", "loaded from" if signature else "removed,", self.pricing_file)
        self.reprice()
        self._parsed = True

    def _scan_claude(self) -> int:
        if not self.root.exists():
            return 0
        inserted = 0
        present: set[Path] = set()
        for path in self.root.rglob("*.jsonl"):
            present.add(path)
            try:
                stat = path.stat()
            except OSError:
                continue
            transcript = self._transcripts.get(path)
            if transcript is None:
                transcript = self._transcripts[path] = ClaudeTranscript(path, self.root)
            if not transcript.changed(stat):
                continue
            self._parsed = True
            try:
                events = transcript.read(stat)
                if events:
                    inserted += self.database.insert_events(self._prepare(events))
                if transcript.activity:
                    self.database.insert_activity(transcript.activity)
                    transcript.activity = []
            except Exception:
                transcript.reset()  # read it from the top next time, so nothing is skipped
                raise
        for stale in [path for path in self._transcripts if path not in present]:
            del self._transcripts[stale]
        return inserted

    def _scan_hermes_activity(self) -> None:
        """Store the Hermes activity of the message rows written since the last read."""
        if self._hermes_after is None:
            position = self.database.get_meta(HERMES_ACTIVITY_META_KEY) or {}
            self._hermes_after = int(position.get("after") or 0) if position.get("path") == str(self.hermes_db) else 0
        events, newest = parse_hermes_activity(self.hermes_db, self._hermes_after)
        self.database.insert_activity(events)
        if newest != self._hermes_after:
            self._hermes_after = newest
            self.database.set_meta(HERMES_ACTIVITY_META_KEY, {"path": str(self.hermes_db), "after": newest})

    def _scan_gateway(self) -> int:
        """Store the gateway lines older than the grace period whose message id no stored row has."""
        if self.gateway is None:
            return 0
        inserted = 0
        while True:  # one chunk at a time, so a long log is never held in memory at once
            before = self.gateway.offset
            chunk = self.gateway.read()
            if chunk:
                self._parsed = True
                self._gateway_pending.extend(chunk)
            inserted += self._store_gateway_lines()
            if self.gateway.offset == before:
                return inserted

    def _store_gateway_lines(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=GATEWAY_GRACE_SECONDS)
        ripe = [event for _, event in self._gateway_pending if event.timestamp <= cutoff]
        if not ripe:
            return 0
        self._gateway_pending = [(offset, event) for offset, event in self._gateway_pending if event.timestamp > cutoff]
        # A response's transcript row carries the time of its first line, up to a long generation earlier.
        window = timedelta(seconds=CLAUDE_DEDUPE_WINDOW_SECONDS)
        known = self.database.message_ids(min(e.timestamp for e in ripe) - window, max(e.timestamp for e in ripe) + window)
        side = [e for e in ripe if e.source_id.removeprefix(GATEWAY_SOURCE_PREFIX) not in known]
        projects = {session: session_project(self.root, session) for session in {e.conversation_id for e in side}}
        inserted = self.database.insert_events(self._prepare(replace(e, project=projects[e.conversation_id]) for e in side))
        offset = self._gateway_pending[0][0] if self._gateway_pending else self.gateway.offset
        self.database.set_meta(GATEWAY_META_KEY, {"path": str(self.gateway.path), "inode": self.gateway.inode, "offset": offset})
        return inserted

    def save_state(self) -> None:
        """Record which transcript versions are fully stored, for `restore_state` at the next start."""
        self.database.set_meta(
            TRANSCRIPTS_META_KEY,
            {str(path): list(t.signature) for path, t in self._transcripts.items() if t.signature},
        )

    def restore_state(self) -> int:
        """Load the signatures from the last clean shutdown, so unchanged transcripts are not read.

        The record is used once and then deleted. A transcript that changed
        since then no longer matches, so it is read from the top, and the
        database ignores the rows it already has.
        """
        saved = self.database.get_meta(TRANSCRIPTS_META_KEY) or {}
        self.database.delete_meta(TRANSCRIPTS_META_KEY)
        if not self.database.get_meta(ACTIVITY_BACKFILL_META_KEY):
            # A crash before the scan ends leaves no saved signatures, so the next start reads everything too.
            self.database.set_meta(ACTIVITY_BACKFILL_META_KEY, datetime.now(timezone.utc).isoformat())
            saved = {}
        for name, signature in saved.items():
            path = Path(name)
            transcript = self._transcripts[path] = ClaudeTranscript(path, self.root)
            transcript.signature = tuple(signature)
        # The gateway position is kept after each store, not only at a clean shutdown.
        position = self.database.get_meta(GATEWAY_META_KEY) or {}
        if self.gateway is not None and position.get("path") == str(self.gateway.path):
            self.gateway.offset, self.gateway.inode = int(position.get("offset") or 0), position.get("inode")
        return len(saved)

    def _changed(self, registry: dict[Path, Signature], path: Path | None) -> bool:
        """Record the current signature of *path* and report whether it changed."""
        if path is None:
            return False
        signature = file_signature(path)
        if signature is None:
            registry.pop(path, None)
            return False
        if registry.get(path) == signature:
            return False
        registry[path] = signature
        self._parsed = True
        return True

    def run_forever(self) -> None:
        running = True

        def stop(_signum: int, _frame: object) -> None:
            nonlocal running
            running = False

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

        log.info("collector started on %s: claude=%s gateway=%s hermes=%s cline=%s db=%s remote=%s",
                 self.settings.hostname, self.root, self.settings.gateway_log, self.hermes_db,
                 self.cline_db or self.cline_task_history, self.database.path, self.settings.remote_url or "off")
        try:
            restored = self.restore_state()
        except Exception:  # noqa: BLE001 - without it the first scan reads every transcript
            log.exception("could not restore transcript state")
            restored = 0
        # Apply the current rate table to history, so a code update with new
        # rates or a first run after the cost column was added is reflected.
        try:
            self.reprice()
        except Exception:  # noqa: BLE001
            log.exception("reprice failed")
        first = True
        while running:
            try:
                inserted = self.scan_once()
            except Exception:  # noqa: BLE001 - a bad poll must not stop the service
                log.exception("scan failed")
            else:
                if first:
                    log.info("initial scan recorded %d new usage events from %d transcript files "
                             "(%d unchanged since the last shutdown)", inserted, len(self._transcripts), restored)
                elif inserted:
                    log.debug("recorded %d new usage events", inserted)
            first = False
            time.sleep(self.settings.poll_seconds)
        try:
            self.save_state()
        except Exception:  # noqa: BLE001 - the next start then reads every transcript
            log.exception("could not save transcript state")
        self.database.close()  # checkpoints the WAL into the database file
        log.info("collector stopped")


def main() -> None:
    """Run the collector as a standalone background process (systemd/launchd entry)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Collector().run_forever()


if __name__ == "__main__":
    main()
