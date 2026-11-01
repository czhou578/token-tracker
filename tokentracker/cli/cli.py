from __future__ import annotations

import os
import plistlib
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import typer

from tokentracker.collector.config import Settings, get_settings, save_config
from tokentracker.collector.database import UsageDatabase
from tokentracker.collector.forwarder import META_LAST_ERROR, META_LAST_OK
from tokentracker.collector.pricing import PRICING_SOURCE_DATE, Pricing
from tokentracker.collector.service import Collector

app = typer.Typer(help="Token Tracker: local token analytics for AI coding sessions.")


@dataclass(frozen=True)
class Service:
    key: str
    description: str
    module: str
    unit: str  # systemd unit name
    label: str  # launchd label


COLLECTOR = Service(
    "collector", "Token Tracker background collector",
    "tokentracker.collector.service", "tokentracker.service", "com.tokentracker.collector",
)
DASHBOARD = Service(
    "dashboard", "Token Tracker dashboard and ingest API",
    "tokentracker.api.main", "tokentracker-dashboard.service", "com.tokentracker.dashboard",
)
SERVICES = (COLLECTOR, DASHBOARD)

# Kept for callers and tests that refer to the collector unit by the old names.
SERVICE_NAME = COLLECTOR.unit
LAUNCHD_LABEL = COLLECTOR.label
COLLECTOR_MODULE = COLLECTOR.module


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
            [sys.executable, "-m", DASHBOARD.module],
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


# ── background services: shared ─────────────────────────────────────────


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
    """Warn when the services would run from a project virtual environment.

    The unit records the absolute interpreter path. If that folder is deleted
    or recreated (for example by `uv sync`), the service fails on every start.
    """
    parts = set(Path(sys.executable).parts)
    if parts & {".venv", "venv"}:
        typer.echo(
            f"Warning: the services will run {sys.executable}, which lives in a project virtual "
            "environment. If that folder is removed or recreated, the collector stops.\n"
            "For a persistent install run `uv tool install .` (or `pipx install .`) in the "
            "project folder, then run `tracker install-service` again from that `tracker`."
        )


def _unsupported() -> None:
    typer.echo(
        "Background service install supports Linux (systemd) and macOS (launchd).\n"
        "On this platform, schedule these commands to run at logon:\n"
        + "\n".join(f"  {sys.executable} -m {service.module}" for service in SERVICES)
    )
    raise typer.Exit(1)


# ── systemd (Linux) ──────────────────────────────────────────────────────


def _systemd_user_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return base / "systemd" / "user"


def _unit_file(service: Service = COLLECTOR) -> str:
    python = shlex.quote(sys.executable)
    return (
        "[Unit]\n"
        f"Description={service.description}\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={python} -m {service.module}\n"
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
    for service in SERVICES:
        unit_path = unit_dir / service.unit
        unit_path.write_text(_unit_file(service))
        typer.echo(f"Wrote {unit_path}")
    _systemctl(systemctl, ["daemon-reload"])
    for service in SERVICES:
        _systemctl(systemctl, ["reset-failed", service.unit], check=False)
        _systemctl(systemctl, ["enable", service.unit])
        _systemctl(systemctl, ["restart", service.unit])


def _uninstall_systemd() -> None:
    systemctl = _find_systemctl()
    for service in SERVICES:
        _systemctl(systemctl, ["stop", service.unit], check=False)
        _systemctl(systemctl, ["disable", service.unit], check=False)
        unit_path = _systemd_user_dir() / service.unit
        if unit_path.exists():
            unit_path.unlink()


def _systemd_state(service: Service) -> tuple[str, str | None]:
    unit_path = _systemd_user_dir() / service.unit
    if not unit_path.exists():
        return "not installed (run `tracker install-service`)", None
    interpreter = _exec_start_interpreter(unit_path.read_text())
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return "unknown (systemctl not found)", interpreter
    result = _systemctl(
        systemctl,
        ["show", "-p", "ActiveState", "-p", "SubState", "-p", "NRestarts", service.unit],
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


# ── launchd (macOS) ──────────────────────────────────────────────────────


def _launch_agents_dir() -> Path:
    return Path("~/Library/LaunchAgents").expanduser()


def _plist_path(service: Service = COLLECTOR) -> Path:
    return _launch_agents_dir() / f"{service.label}.plist"


def _plist_bytes(log_path: Path, service: Service = COLLECTOR) -> bytes:
    return plistlib.dumps(
        {
            "Label": service.label,
            "ProgramArguments": [sys.executable, "-m", service.module],
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
    for service in SERVICES:
        plist = _plist_path(service)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(_plist_bytes(settings.data_dir / f"{service.key}.log", service))
        typer.echo(f"Wrote {plist}")
        target = f"{_launchd_domain()}/{service.label}"
        # Unload a previous copy first; bootstrap refuses to load a label twice.
        _run([launchctl, "bootout", target], check=False)
        _run([launchctl, "bootstrap", _launchd_domain(), str(plist)])


def _uninstall_launchd() -> None:
    launchctl = _find_launchctl()
    for service in SERVICES:
        _run([launchctl, "bootout", f"{_launchd_domain()}/{service.label}"], check=False)
        plist = _plist_path(service)
        if plist.exists():
            plist.unlink()


def _launchd_state(service: Service) -> tuple[str, str | None]:
    plist = _plist_path(service)
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
    result = _run([launchctl, "print", f"{_launchd_domain()}/{service.label}"], check=False)
    if result.returncode != 0:
        return "installed but not loaded", interpreter
    for line in result.stdout.splitlines():
        if line.strip().startswith("state ="):
            return line.strip().partition("=")[2].strip(), interpreter
    return "loaded", interpreter


def _service_state(service: Service) -> tuple[str, str | None]:
    platform = _platform()
    if platform == "systemd":
        return _systemd_state(service)
    if platform == "launchd":
        return _launchd_state(service)
    return "background service not supported on this platform", None


# ── commands ─────────────────────────────────────────────────────────────


@app.command("install-service")
def install_service() -> None:
    """Install and start the collector and dashboard services (systemd on Linux, launchd on macOS)."""
    _warn_if_fragile_interpreter()
    platform = _platform()
    if platform == "systemd":
        _install_systemd()
    elif platform == "launchd":
        _install_launchd()
    else:
        _unsupported()
    typer.echo("Token Tracker collector and dashboard are running in the background.")


@app.command("uninstall-service")
def uninstall_service() -> None:
    """Stop and remove the background services."""
    platform = _platform()
    if platform == "systemd":
        _uninstall_systemd()
    elif platform == "launchd":
        _uninstall_launchd()
    else:
        _unsupported()
    typer.echo("Token Tracker services removed.")


@app.command()
def status() -> None:
    """Show service state, interpreter, remote link, and the newest event per source."""
    settings = get_settings()
    typer.echo(f"Machine:     {settings.hostname}")
    for service in SERVICES:
        state, interpreter = _service_state(service)
        typer.echo(f"{service.key.capitalize() + ':':<13}{state}")
        if interpreter is not None and not Path(interpreter).exists():
            typer.echo(f"  interpreter {interpreter} is MISSING - reinstall the package and run `tracker install-service`")
    _, interpreter = _service_state(COLLECTOR)
    if interpreter is not None and Path(interpreter).exists():
        typer.echo(f"Interpreter: {interpreter} (ok)")
    typer.echo(f"Database:    {settings.db_path}")

    database = UsageDatabase(settings.db_path, hostname=settings.hostname)
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

    token_file = settings.ingest_token_file
    ingest = "enabled" if token_file is not None and token_file.is_file() else "disabled (run `tracker ingest-token` to enable)"
    typer.echo(f"Ingest:      {ingest}")
    if settings.remote_url:
        pending = database.pending_forward_count()
        last_ok = database.get_meta(META_LAST_OK)
        last_error = database.get_meta(META_LAST_ERROR)
        detail = f"{pending} pending"
        if last_ok:
            detail += f", last push {_ago(last_ok['at'])} ({last_ok['sent']} events)"
        if last_error and (not last_ok or last_error["at"] > last_ok["at"]):
            detail += f", last error {_ago(last_error['at'])}: {last_error['error']}"
        typer.echo(f"Remote:      {settings.remote_url} ({detail})")
    else:
        typer.echo("Remote:      not linked (run `tracker link <url> --token <token>` on a second machine)")

    hosts = database.hosts()
    if len(hosts) > 1:
        typer.echo("Machines:    " + ", ".join(f"{row['host'] or '?'} ({row['events']} events)" for row in hosts))

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


def _ago(epoch: float) -> str:
    seconds = max(0, int(time.time() - epoch))
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@app.command("ingest-token")
def ingest_token(rotate: bool = typer.Option(False, "--rotate", help="Replace an existing token.")) -> None:
    """Enable ingest from other machines: create (or show) the shared bearer token."""
    settings = get_settings()
    path = settings.ingest_token_file
    if path is None:
        raise typer.Exit(1)
    if path.is_file() and not rotate:
        token = path.read_text(encoding="utf-8").strip()
        typer.echo(f"Ingest token already exists at {path} (use --rotate to replace it):")
    else:
        token = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        typer.echo(f"Wrote ingest token to {path}:")
    typer.echo(f"  {token}")
    typer.echo("On the other machine run:")
    typer.echo(f"  tracker link https://<this-machine>/ --token {token}")


@app.command()
def link(
    url: str = typer.Argument(..., help="Base URL of the main tracker, e.g. https://spark.tailxxxx.ts.net"),
    token: str = typer.Option(..., "--token", help="Ingest token from `tracker ingest-token` on the main tracker."),
    project_labels: str = typer.Option("hash", "--project-labels", help="'hash' (default) or 'clear' project names."),
    host_label: str | None = typer.Option(None, "--host-label", help="Name shown for this machine (default: hostname)."),
) -> None:
    """Forward this machine's usage to a main tracker. Settings are saved in config.json."""
    if project_labels not in ("hash", "clear"):
        typer.echo("--project-labels must be 'hash' or 'clear'")
        raise typer.Exit(1)
    settings = get_settings()
    path = save_config(
        settings.data_dir,
        remote_url=url.rstrip("/"),
        remote_token=token,
        project_labels=project_labels,
        project_salt=settings.project_salt or secrets.token_hex(8),
        host_label=host_label,
    )
    typer.echo(f"Saved remote link in {path}.")
    typer.echo("Restart the collector to start forwarding: tracker install-service")


@app.command()
def unlink() -> None:
    """Stop forwarding usage to a main tracker."""
    settings = get_settings()
    save_config(settings.data_dir, remote_url=None, remote_token=None)
    typer.echo("Remote link removed. Restart the collector: tracker install-service")


@app.command()
def reprice() -> None:
    """Recompute the cost estimate of every stored event with the current rate table."""
    settings = get_settings()
    pricing = Pricing.load(settings.pricing_file)
    changed = UsageDatabase(settings.db_path, hostname=settings.hostname).reprice(pricing)
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
    unpriced = UsageDatabase(settings.db_path, hostname=settings.hostname).unpriced_models()
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
