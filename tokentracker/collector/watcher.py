from __future__ import annotations

from tokentracker.collector.service import Collector


def watch_forever() -> None:
    Collector().run_forever()
