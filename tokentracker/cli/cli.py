from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
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
SERVICE_NAME = "tokentracker.service"


def _systemd_user_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return base / "systemd" / "user"


def _unit_file() -> str:
    python = shlex.quote(sys.executable)
    return (
        "[Unit]\n"
        "Description=Token Tracker background collector\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={python} -m tokentracker.collector.service\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _find_systemctl() -> str:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        typer.echo("systemctl not found: systemd is not available on this system.")
        raise typer.Exit(1)
    return systemctl


def _systemctl(systemctl: str, args: list[str], check: bool = True) -> None:
    result = subprocess.run([systemctl, "--user", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        command = " ".join(args)
        typer.echo(f"systemctl --user {command} failed:")
        typer.echo(result.stderr.strip())
        raise typer.Exit(1)


@app.command("install-service")
def install_service() -> None:
    """Install and start the background collector as a systemd user service."""
    systemctl = _find_systemctl()
    unit_dir = _systemd_user_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / SERVICE_NAME
    unit_path.write_text(_unit_file())
    typer.echo(f"Wrote {unit_path}")
    _systemctl(systemctl, ["daemon-reload"])
    _systemctl(systemctl, ["enable", "--now", SERVICE_NAME])
    typer.echo("Token Tracker collector is running in the background.")


@app.command("uninstall-service")
def uninstall_service() -> None:
    """Stop and remove the background collector service."""
    systemctl = _find_systemctl()
    _systemctl(systemctl, ["stop", SERVICE_NAME], check=False)
    _systemctl(systemctl, ["disable", SERVICE_NAME], check=False)
    unit_path = _systemd_user_dir() / SERVICE_NAME
    if unit_path.exists():
        unit_path.unlink()
    typer.echo("Token Tracker collector service removed.")


def main() -> None:
    """Console entry point so the CLI also runs as ``python -m tokentracker.cli.cli``.

    The package is installed (editable) in site-packages, so this works from any
    working directory, not just the project root.
    """
    app()


if __name__ == "__main__":
    main()
