from __future__ import annotations

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

    def scan_once(self) -> int:
        inserted = 0
        if self.root.exists():
            for path in self.root.rglob("*.jsonl"):
                inserted += self.database.insert_events(parse_claude_jsonl(path, self.root))
        if self.cline_db is not None and self.cline_db.exists():
            inserted += self.database.insert_events(parse_cline_sqlite(self.cline_db))
        if self.cline_task_history is not None and self.cline_task_history.exists():
            inserted += self.database.insert_events(parse_cline_task_history(self.cline_task_history))
        if self.hermes_db is not None and self.hermes_db.exists():
            inserted += self.database.insert_events(parse_hermes_state_db(self.hermes_db))
        return inserted

    def run_forever(self) -> None:
        running = True

        def stop(_signum: int, _frame: object) -> None:
            nonlocal running
            running = False

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

        while running:
            self.scan_once()
            time.sleep(self.settings.poll_seconds)