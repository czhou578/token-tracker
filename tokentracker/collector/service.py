from __future__ import annotations

import signal
import time
from pathlib import Path

import typer

from tokentracker.collector.config import get_settings
from tokentracker.collector.database import UsageDatabase
from tokentracker.collector.parser import parse_claude_jsonl

app = typer.Typer(help="Run the Token Tracker background collector.")


class Collector:
    def __init__(self, root: Path | None = None):
        self.settings = get_settings()
        self.root = root or self.settings.claude_dir
        self.database = UsageDatabase(self.settings.db_path)

    def scan_once(self) -> int:
        if not self.root.exists():
            return 0
        inserted = 0
        for path in self.root.rglob("*.jsonl"):
            inserted += self.database.insert_events(parse_claude_jsonl(path, self.root))
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


@app.command()
def run(once: bool = typer.Option(False, "--once", help="Scan once and exit.")) -> None:
    collector = Collector()
    inserted = collector.scan_once()
    if once:
        typer.echo(f"Inserted {inserted} usage events.")
        return
    typer.echo(f"Token Tracker collector watching {collector.root}")
    collector.run_forever()


if __name__ == "__main__":
    app()
