from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import typer

from tokentracker.collector.config import get_settings
from tokentracker.collector.database import UsageDatabase
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
    _ensure_server(url)
    webbrowser.open(url)
    typer.echo(f"Opened {url}")


@app.command()
def collect(once: bool = typer.Option(True, "--once/--forever", help="Scan once or keep collecting.")) -> None:
    """Run the collector."""
    collector = Collector()
    if once:
        typer.echo(f"Inserted {collector.scan_once()} usage events.")
        return
    collector.run_forever()


@app.command()
def today() -> None:
    """Print today's usage summary."""
    _print_summary("today")


@app.command()
def week() -> None:
    """Print the last seven days of usage."""
    _print_summary("7d")


@app.command()
def projects() -> None:
    """Print usage grouped by project."""
    Collector().scan_once()
    database = UsageDatabase(get_settings().db_path)
    _print_table(database.projects(window="30d"), "project")


@app.command()
def models() -> None:
    """Print usage grouped by model."""
    Collector().scan_once()
    database = UsageDatabase(get_settings().db_path)
    _print_table(database.models(window="30d"), "model")


@app.command("install-service")
def install_service() -> None:
    """Write a systemd user service for background collection."""
    settings = get_settings()
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = service_dir / "tokentracker.service"
    service_file.write_text(
        "\n".join(
            [
                "[Unit]",
                "Description=Token Tracker background collector",
                "",
                "[Service]",
                f"ExecStart={sys.executable} -m tokentracker.collector.service",
                "Restart=on-failure",
                f"Environment=TOKEN_TRACKER_HOME={settings.data_dir}",
                f"Environment=TOKEN_TRACKER_CLAUDE_DIR={settings.claude_dir}",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        ),
        encoding="utf-8",
    )
    typer.echo(f"Wrote {service_file}")
    typer.echo("Enable it with: systemctl --user enable --now tokentracker")


def _print_summary(window: str) -> None:
    Collector().scan_once()
    database = UsageDatabase(get_settings().db_path)
    stats = database.stats(window=window)
    typer.echo(json.dumps(stats, indent=2))


def _print_table(rows: list[dict], label: str) -> None:
    if not rows:
        typer.echo("No usage events found.")
        return
    typer.echo(f"{label:32} tokens       cost     requests")
    for row in rows:
        typer.echo(
            f"{str(row[label])[:32]:32} {int(row['total_tokens']):10} "
            f"${float(row['cost_usd']):8.4f} {int(row['requests']):10}"
        )


def _ensure_server(url: str) -> None:
    health_url = f"{url}/api/health"
    if _is_healthy(health_url):
        return

    settings = get_settings()
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
