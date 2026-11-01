from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import typer

from tokentracker.collector.config import Settings, get_settings
from tokentracker.collector.database import UsageDatabase
from tokentracker.collector.pricing import PRICING_SOURCE_DATE, Pricing
from tokentracker.collector.service import Collector

app = typer.Typer(help="Token Tracker: local token analytics for AI coding sessions.")

SERVICE_NAME = "tokentracker.service"
LAUNCHD_LABEL = "com.tokentracker.collector"
COLLECTOR_MODULE = "tokentracker.collector.service"


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


# ── background service: shared ───────────────────────────────────────────


def _platform() -> str:
    """Name the service manager for this OS: 'systemd', 'launchd', or 'unsupported'."""
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    return "unsupported"


def _require_tool(name: str, hint: str) -> str:
    tool = shutil.which(name)
    if tool is None:
        typer.echo(f"{name} not found: {hint}")
        raise typer.Exit(1)
    return tool


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True)
    if check and result.returncode != 0:
        typer.echo(f"{' '.join(args)} failed:")
        typer.echo(result.stderr.strip())
        raise typer.Exit(1)
    return result


def _warn_if_fragile_interpreter() -> None:
    """Warn when the service would run from a project virtual environment.

    The unit records the absolute interpreter path. If that folder is deleted
    or recreated (for example by `uv sync`), the service fails on every start.
    """
    parts = set(Path(sys.executable).parts)
    if parts & {".venv", "venv"}:
        typer.echo(
            f"Warning: the service will run {sys.executable}, which lives in a project virtual "
            "environment. If that folder is removed or recreated, the collector stops.\n"
            "For a persistent install run `uv tool install .` (or `pipx install .`) in the "
            "project folder, then run `tracker install-service` again from that `tracker`."
        )


def _unsupported() -> None:
    typer.echo(
        "Background service install supports Linux (systemd) and macOS (launchd).\n"
        f"On this platform, schedule this command to run at logon:\n"
        f"  {sys.executable} -m {COLLECTOR_MODULE}"
    )
    raise typer.Exit(1)


# ── background service: systemd (Linux) ──────────────────────────────────


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
        f"ExecStart={python} -m {COLLECTOR_MODULE}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _find_systemctl() -> str:
    return _require_tool("systemctl", "systemd is not available on this system.")


def _systemctl(systemctl: str, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return _run([systemctl, "--user", *args], check=check)


def _install_systemd() -> None:
    systemctl = _find_systemctl()
    unit_dir = _systemd_user_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / SERVICE_NAME
    unit_path.write_text(_unit_file())
    typer.echo(f"Wrote {unit_path}")
    _systemctl(systemctl, ["daemon-reload"])
    _systemctl(systemctl, ["reset-failed", SERVICE_NAME], check=False)
    _systemctl(systemctl, ["enable", SERVICE_NAME])
    _systemctl(systemctl, ["restart", SERVICE_NAME])


def _uninstall_systemd() -> None:
    systemctl = _find_systemctl()
    _systemctl(systemctl, ["stop", SERVICE_NAME], check=False)
    _systemctl(systemctl, ["disable", SERVICE_NAME], check=False)
    unit_path = _systemd_user_dir() / SERVICE_NAME
    if unit_path.exists():
        unit_path.unlink()


def _systemd_state() -> tuple[str, str | None]:
    unit_path = _systemd_user_dir() / SERVICE_NAME
    if not unit_path.exists():
        return "not installed (run `tracker install-service`)", None
    interpreter = _exec_start_interpreter(unit_path.read_text())
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return "unknown (systemctl not found)", interpreter
    result = _systemctl(
        systemctl,
        ["show", "-p", "ActiveState", "-p", "SubState", "-p", "NRestarts", SERVICE_NAME],
        check=False,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    active = values.get("ActiveState", "unknown")
    sub = values.get("SubState", "unknown")
    restarts = values.get("NRestarts", "?")
    return f"{active} ({sub}), restarts: {restarts}", interpreter


def _exec_start_interpreter(unit_text: str) -> str | None:
    for line in unit_text.splitlines():
        if line.startswith("ExecStart="):
            parts = shlex.split(line.partition("=")[2])
            return parts[0] if parts else None
    return None


# ── background service: launchd (macOS) ──────────────────────────────────


def _launch_agents_dir() -> Path:
    return Path("~/Library/LaunchAgents").expanduser()


def _plist_path() -> Path:
    return _launch_agents_dir() / f"{LAUNCHD_LABEL}.plist"


def _plist_bytes(log_path: Path) -> bytes:
    return plistlib.dumps(
        {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [sys.executable, "-m", COLLECTOR_MODULE],
            "RunAtLoad": True,
            "KeepAlive": True,
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
            "StandardOutPath": str(log_path),
            "StandardErrorPath": str(log_path),
        }
    )


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def _find_launchctl() -> str:
    return _require_tool("launchctl", "launchd is not available on this system.")


def _install_launchd() -> None:
    launchctl = _find_launchctl()
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    plist = _plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(_plist_bytes(settings.data_dir / "collector.log"))
    typer.echo(f"Wrote {plist}")
    target = f"{_launchd_domain()}/{LAUNCHD_LABEL}"
    # Unload a previous copy first; bootstrap refuses to load a label twice.
    _run([launchctl, "bootout", target], check=False)
    _run([launchctl, "bootstrap", _launchd_domain(), str(plist)])


def _uninstall_launchd() -> None:
    launchctl = _find_launchctl()
    _run([launchctl, "bootout", f"{_launchd_domain()}/{LAUNCHD_LABEL}"], check=False)
    plist = _plist_path()
    if plist.exists():
        plist.unlink()


def _launchd_state() -> tuple[str, str | None]:
    plist = _plist_path()
    if not plist.exists():
        return "not installed (run `tracker install-service`)", None
    try:
        arguments = plistlib.loads(plist.read_bytes()).get("ProgramArguments") or []
    except plistlib.InvalidFileException:
        arguments = []
    interpreter = arguments[0] if arguments else None
    launchctl = shutil.which("launchctl")
    if launchctl is None:
        return "unknown (launchctl not found)", interpreter
    result = _run([launchctl, "print", f"{_launchd_domain()}/{LAUNCHD_LABEL}"], check=False)
    if result.returncode != 0:
        return "installed but not loaded", interpreter
    for line in result.stdout.splitlines():
        if line.strip().startswith("state ="):
            return line.strip().partition("=")[2].strip(), interpreter
    return "loaded", interpreter


# ── commands ─────────────────────────────────────────────────────────────


@app.command("install-service")
def install_service() -> None:
    """Install and start the background collector (systemd on Linux, launchd on macOS)."""
    _warn_if_fragile_interpreter()
    platform = _platform()
    if platform == "systemd":
        _install_systemd()
    elif platform == "launchd":
        _install_launchd()
    else:
        _unsupported()
    typer.echo("Token Tracker collector is running in the background.")


@app.command("uninstall-service")
def uninstall_service() -> None:
    """Stop and remove the background collector service."""
    platform = _platform()
    if platform == "systemd":
        _uninstall_systemd()
    elif platform == "launchd":
        _uninstall_launchd()
    else:
        _unsupported()
    typer.echo("Token Tracker collector service removed.")


@app.command()
def status() -> None:
    """Show the collector state, its interpreter, and the newest event per source."""
    settings = get_settings()
    platform = _platform()
    if platform == "systemd":
        state, interpreter = _systemd_state()
    elif platform == "launchd":
        state, interpreter = _launchd_state()
    else:
        state, interpreter = "background service not supported on this platform", None

    typer.echo(f"Collector:   {state}")
    if interpreter is not None:
        if Path(interpreter).exists():
            note = "ok"
        else:
            note = "MISSING - reinstall the package and run `tracker install-service`"
        typer.echo(f"Interpreter: {interpreter} ({note})")
    typer.echo(f"Database:    {settings.db_path}")

    database = UsageDatabase(settings.db_path)
    migration = database.migration_info()
    if migration and migration.get("backup"):
        typer.echo(
            f"Migration:   collapsed {migration['removed']} duplicate Claude rows on {migration['at'][:10]}; "
            f"backup {migration['backup']}"
        )
    totals = database.stats()
    priced, requests = totals["priced_requests"], totals["requests"]
    coverage = f"{priced}/{requests} events priced" if requests else "no events"
    typer.echo(f"Est. cost:   ${totals['cost_usd']:,.2f} at list price ({coverage})")

    latest = database.latest_by_provider()
    sources = (
        ("claude", settings.claude_dir),
        ("cline", settings.cline_db or settings.cline_task_history),
        ("hermes", settings.hermes_db),
    )
    typer.echo("Sources:")
    for provider, path in sources:
        found = "found" if path is not None and path.exists() else "not found"
        row = latest.get(provider)
        last = f"{row['events']} events, last {row['last_timestamp']}" if row else "no events"
        typer.echo(f"  {provider:<7} {path or '-'}  [{found}]  {last}")


@app.command()
def reprice() -> None:
    """Recompute the cost estimate of every stored event with the current rate table."""
    settings = get_settings()
    pricing = Pricing.load(settings.pricing_file)
    changed = UsageDatabase(settings.db_path).reprice(pricing)
    typer.echo(f"Repriced {changed} events ({_pricing_origin(settings, pricing)}).")


@app.command()
def pricing() -> None:
    """Show the rate table in use (USD per million tokens) and the models without a price."""
    settings = get_settings()
    table = Pricing.load(settings.pricing_file)
    typer.echo(f"Rates: {_pricing_origin(settings, table)}")
    typer.echo(f"{'model':<22} {'input':>8} {'output':>8} {'cache rd':>9} {'write 5m':>9} {'write 1h':>9}")
    for key, rates in sorted(table.table.items()):
        typer.echo(
            f"{key:<22} {rates.get('input', 0):>8.3f} {rates.get('output', 0):>8.3f} "
            f"{rates.get('cache_read', 0):>9.4f} {rates.get('cache_write_5m', 0):>9.4f} "
            f"{rates.get('cache_write_1h', 0):>9.4f}"
        )
    unpriced = UsageDatabase(settings.db_path).unpriced_models()
    if unpriced:
        typer.echo("")
        typer.echo(f"Unpriced models (add a rate card to {settings.pricing_file} to estimate them):")
        for row in unpriced:
            typer.echo(f"  {row['model']:<36} {row['events']:>7} events {row['total_tokens']:>14,} tokens")


def _pricing_origin(settings: Settings, pricing: Pricing) -> str:
    bundled = f"bundled Anthropic list prices dated {PRICING_SOURCE_DATE}"
    if settings.pricing_file is not None and settings.pricing_file.is_file():
        return f"{bundled} + overrides from {settings.pricing_file}"
    return bundled


def main() -> None:
    """Console entry point so the CLI also runs as ``python -m tokentracker.cli.cli``.

    The package is installed in site-packages, so this works from any working
    directory, not just the project root.
    """
    app()


if __name__ == "__main__":
    main()
