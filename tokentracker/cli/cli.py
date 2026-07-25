from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

import typer

from tokentracker.collector.config import Settings, get_settings
from tokentracker.collector.service import Collector

app = typer.Typer(help="Token Tracker: local token analytics for AI coding sessions.")


@app.callback(invoke_without_command=True)
def default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        dashboard()


@app.command()
def dashboard() -> None:
    """Open the local analytics dashboard."""
    settings = get_settings()
    Collector().scan_once()
    url = f"http://{settings.host}:{settings.port}"
    _ensure_server(url, settings)
    webbrowser.open(url)
    typer.echo(f"Opened {url}")


def _ensure_server(url: str, settings: Settings | None = None) -> None:
    health_url = f"{url}/api/health"
    if _is_healthy(health_url):
        return

    settings = settings or get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log_file = settings.data_dir / "server.log"
    with log_file.open("ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "tokentracker.api.main"],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )

    for _ in range(30):
        if _is_healthy(health_url):
            return
        time.sleep(0.2)
    typer.echo(f"Dashboard server did not start. See {log_file}")
    raise typer.Exit(1)


def _is_healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False