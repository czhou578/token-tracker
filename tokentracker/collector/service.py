from __future__ import annotations

import logging
import signal
import time
from dataclasses import replace
from pathlib import Path

from tokentracker.collector.config import get_settings
from tokentracker.collector.database import UsageDatabase
from tokentracker.collector.parser import (
    ClaudeTranscript,
    parse_cline_sqlite,
    parse_cline_task_history,
    parse_hermes_state_db,
)
from tokentracker.collector.forwarder import Forwarder
from tokentracker.collector.memory import release_memory
from tokentracker.collector.pricing import Pricing

log = logging.getLogger("tokentracker.collector")

# Meta keys. The first holds the transcript signatures saved at a clean shutdown;
# the second the `Pricing.fingerprint` that stored costs were last computed with.
TRANSCRIPTS_META_KEY = "collector_transcripts_v1"
PRICING_META_KEY = "pricing_fingerprint"

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
        if self._changed(self._sources, self.cline_db):
            inserted += self.database.insert_events(prepare(parse_cline_sqlite(self.cline_db)))
        if self._changed(self._sources, self.cline_task_history):
            inserted += self.database.insert_events(prepare(parse_cline_task_history(self.cline_task_history)))
        if self._changed(self._sources, self.hermes_db):
            # Hermes rows are per-session aggregates that grow while the session
            # is open, so refresh existing rows instead of ignoring them.
            inserted += self.database.upsert_events(prepare(parse_hermes_state_db(self.hermes_db)))
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
            except Exception:
                transcript.reset()  # read it from the top next time, so nothing is skipped
                raise
        for stale in [path for path in self._transcripts if path not in present]:
            del self._transcripts[stale]
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
        for name, signature in saved.items():
            path = Path(name)
            transcript = self._transcripts[path] = ClaudeTranscript(path, self.root)
            transcript.signature = tuple(signature)
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

        log.info("collector started on %s: claude=%s hermes=%s cline=%s db=%s remote=%s",
                 self.settings.hostname, self.root, self.hermes_db, self.cline_db or self.cline_task_history,
                 self.database.path, self.settings.remote_url or "off")
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
