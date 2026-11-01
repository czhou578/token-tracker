from __future__ import annotations

import logging
import signal
import time
from pathlib import Path

from tokentracker.collector.config import get_settings
from tokentracker.collector.database import UsageDatabase
from tokentracker.collector.parser import (
    parse_claude_jsonl,
    parse_cline_sqlite,
    parse_cline_task_history,
    parse_hermes_state_db,
)
from tokentracker.collector.pricing import Pricing

log = logging.getLogger("tokentracker.collector")

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
        self.database = UsageDatabase(self.settings.db_path)
        self.pricing_file = self.settings.pricing_file
        self.pricing = Pricing.load(self.pricing_file)
        # Change markers so each poll re-parses only the files that changed.
        self._claude_files: dict[Path, Signature] = {}
        self._sources: dict[Path, Signature] = {}
        self._pricing_signature = file_signature(self.pricing_file) if self.pricing_file else None

    def scan_once(self) -> int:
        self._refresh_pricing()
        price = self.pricing.price_events
        inserted = self._scan_claude()
        if self._changed(self._sources, self.cline_db):
            inserted += self.database.insert_events(price(parse_cline_sqlite(self.cline_db)))
        if self._changed(self._sources, self.cline_task_history):
            inserted += self.database.insert_events(price(parse_cline_task_history(self.cline_task_history)))
        if self._changed(self._sources, self.hermes_db):
            # Hermes rows are per-session aggregates that grow while the session
            # is open, so refresh existing rows instead of ignoring them.
            inserted += self.database.upsert_events(price(parse_hermes_state_db(self.hermes_db)))
        return inserted

    def reprice(self) -> int:
        """Recompute every stored cost with the current pricing table."""
        changed = self.database.reprice(self.pricing)
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

    def _scan_claude(self) -> int:
        if not self.root.exists():
            return 0
        inserted = 0
        present: set[Path] = set()
        for path in self.root.rglob("*.jsonl"):
            present.add(path)
            if self._changed(self._claude_files, path):
                inserted += self.database.insert_events(self.pricing.price_events(parse_claude_jsonl(path, self.root)))
        for stale in [path for path in self._claude_files if path not in present]:
            del self._claude_files[stale]
        return inserted

    @staticmethod
    def _changed(registry: dict[Path, Signature], path: Path | None) -> bool:
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
        return True

    def run_forever(self) -> None:
        running = True

        def stop(_signum: int, _frame: object) -> None:
            nonlocal running
            running = False

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

        log.info("collector started: claude=%s hermes=%s cline=%s db=%s",
                 self.root, self.hermes_db, self.cline_db or self.cline_task_history, self.database.path)
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
                    log.info("initial scan recorded %d new usage events from %d transcript files",
                             inserted, len(self._claude_files))
                elif inserted:
                    log.debug("recorded %d new usage events", inserted)
            first = False
            time.sleep(self.settings.poll_seconds)
        log.info("collector stopped")


def main() -> None:
    """Run the collector as a standalone background process (systemd/launchd entry)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Collector().run_forever()


if __name__ == "__main__":
    main()
