import plistlib
import subprocess

import pytest
import typer
from typer.testing import CliRunner

import tokentracker.cli.cli as cli


def _fake_run(calls, stdout=""):
    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout, "")

    return fake_run


def _which(name_to_path):
    return lambda name: name_to_path.get(name)


@pytest.fixture
def systemd(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli, "_platform", lambda: "systemd")
    monkeypatch.setattr(cli.shutil, "which", _which({"systemctl": "/usr/bin/systemctl"}))
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(calls))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("TOKEN_TRACKER_HOME", str(tmp_path / "home"))
    return calls


@pytest.fixture
def launchd(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli, "_platform", lambda: "launchd")
    monkeypatch.setattr(cli, "_launch_agents_dir", lambda: tmp_path / "LaunchAgents")
    monkeypatch.setattr(cli, "_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr(cli.shutil, "which", _which({"launchctl": "/bin/launchctl"}))
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(calls))
    monkeypatch.setenv("TOKEN_TRACKER_HOME", str(tmp_path / "home"))
    return calls


def test_unit_file_uses_installed_python_and_newlines(monkeypatch):
    monkeypatch.setattr(cli.sys, "executable", "/tmp/venv/bin/python")
    unit = cli._unit_file()
    assert "ExecStart=/tmp/venv/bin/python -m tokentracker.collector.service" in unit
    assert "[Unit]" in unit and "[Service]" in unit and "[Install]" in unit
    assert "WantedBy=default.target" in unit
    assert chr(10) in unit


def test_systemd_user_dir_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert cli._systemd_user_dir() == tmp_path / "config" / "systemd" / "user"


def test_systemd_user_dir_defaults_to_dot_config(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    path = cli._systemd_user_dir()
    assert path.parts[-2:] == ("systemd", "user")


def test_install_service_writes_units_and_restarts(systemd, tmp_path):
    cli.install_service()
    unit_dir = tmp_path / "config" / "systemd" / "user"
    collector = (unit_dir / "tokentracker.service").read_text()
    dashboard = (unit_dir / "tokentracker-dashboard.service").read_text()
    assert "-m tokentracker.collector.service" in collector
    assert "-m tokentracker.api.main" in dashboard
    actions = [call[2:] for call in systemd]
    assert actions == [
        ["daemon-reload"],
        ["reset-failed", "tokentracker.service"],
        ["enable", "tokentracker.service"],
        ["restart", "tokentracker.service"],
        ["reset-failed", "tokentracker-dashboard.service"],
        ["enable", "tokentracker-dashboard.service"],
        ["restart", "tokentracker-dashboard.service"],
    ]
    assert all(call[:2] == ["/usr/bin/systemctl", "--user"] for call in systemd)


def test_uninstall_service_stops_disables_and_removes_unit(systemd, tmp_path):
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "tokentracker.service").write_text("existing")
    cli.uninstall_service()
    assert not (unit_dir / "tokentracker.service").exists()
    assert systemd[0] == ["/usr/bin/systemctl", "--user", "stop", "tokentracker.service"]
    assert systemd[1] == ["/usr/bin/systemctl", "--user", "disable", "tokentracker.service"]
    assert systemd[2] == ["/usr/bin/systemctl", "--user", "stop", "tokentracker-dashboard.service"]


def test_install_service_errors_without_systemctl(monkeypatch):
    monkeypatch.setattr(cli, "_platform", lambda: "systemd")
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    with pytest.raises(typer.Exit):
        cli.install_service()


def test_install_service_warns_about_project_venv(systemd, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "executable", "/home/me/project/.venv/bin/python")
    cli.install_service()
    assert "project virtual environment" in capsys.readouterr().out


def test_install_service_is_quiet_for_tool_install(systemd, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "executable", "/home/me/.local/share/uv/tools/tokentracker/bin/python")
    cli.install_service()
    assert "Warning" not in capsys.readouterr().out


def test_install_service_refuses_unsupported_platform(monkeypatch):
    monkeypatch.setattr(cli, "_platform", lambda: "unsupported")
    with pytest.raises(typer.Exit):
        cli.install_service()


def test_install_service_launchd_writes_plist_and_bootstraps(launchd, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sys, "executable", "/opt/tools/bin/python")
    cli.install_service()
    plist_path = tmp_path / "LaunchAgents" / "com.tokentracker.collector.plist"
    assert plist_path.exists()
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["Label"] == "com.tokentracker.collector"
    assert plist["ProgramArguments"] == ["/opt/tools/bin/python", "-m", "tokentracker.collector.service"]
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
    assert plist["StandardOutPath"] == str(tmp_path / "home" / "collector.log")
    dashboard_plist = tmp_path / "LaunchAgents" / "com.tokentracker.dashboard.plist"
    assert plistlib.loads(dashboard_plist.read_bytes())["ProgramArguments"][-1] == "tokentracker.api.main"
    assert launchd == [
        ["/bin/launchctl", "bootout", "gui/501/com.tokentracker.collector"],
        ["/bin/launchctl", "bootstrap", "gui/501", str(plist_path)],
        ["/bin/launchctl", "bootout", "gui/501/com.tokentracker.dashboard"],
        ["/bin/launchctl", "bootstrap", "gui/501", str(dashboard_plist)],
    ]


def test_uninstall_service_launchd_boots_out_and_removes_plist(launchd, tmp_path):
    plist_path = tmp_path / "LaunchAgents" / "com.tokentracker.collector.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_bytes(b"existing")
    cli.uninstall_service()
    assert not plist_path.exists()
    assert launchd == [
        ["/bin/launchctl", "bootout", "gui/501/com.tokentracker.collector"],
        ["/bin/launchctl", "bootout", "gui/501/com.tokentracker.dashboard"],
    ]


def test_status_reports_missing_interpreter_and_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_platform", lambda: "systemd")
    monkeypatch.setattr(cli.shutil, "which", _which({"systemctl": "/usr/bin/systemctl"}))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        _fake_run([], stdout="ActiveState=activating\nSubState=auto-restart\nNRestarts=178130\n"),
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("TOKEN_TRACKER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TOKEN_TRACKER_CLAUDE_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("TOKEN_TRACKER_HERMES_DB", str(tmp_path / "state.db"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "tokentracker.service").write_text(
        "[Service]\nExecStart=/gone/.venv/bin/python3 -m tokentracker.collector.service\n"
    )
    (tmp_path / "claude").mkdir()

    result = CliRunner().invoke(cli.app, ["status"])

    assert result.exit_code == 0, result.output
    assert "activating (auto-restart), restarts: 178130" in result.output
    assert "/gone/.venv/bin/python3 is MISSING" in result.output
    assert "Ingest:      disabled" in result.output
    assert "Remote:      not linked" in result.output
    assert "claude" in result.output and "[found]" in result.output
    assert "hermes" in result.output and "[not found]" in result.output


def test_status_reports_not_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_platform", lambda: "systemd")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("TOKEN_TRACKER_HOME", str(tmp_path / "home"))

    result = CliRunner().invoke(cli.app, ["status"])

    assert result.exit_code == 0, result.output
    assert "not installed" in result.output


def test_ingest_token_creates_private_file_once(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_TRACKER_HOME", str(tmp_path / "home"))
    first = CliRunner().invoke(cli.app, ["ingest-token"])
    assert first.exit_code == 0, first.output
    token_file = tmp_path / "home" / "ingest_token"
    token = token_file.read_text().strip()
    assert len(token) >= 32 and token in first.output
    assert oct(token_file.stat().st_mode)[-3:] == "600"
    second = CliRunner().invoke(cli.app, ["ingest-token"])
    assert "already exists" in second.output and token in second.output
    rotated = CliRunner().invoke(cli.app, ["ingest-token", "--rotate"])
    assert token_file.read_text().strip() != token and rotated.exit_code == 0


def test_link_and_unlink_write_config(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_TRACKER_HOME", str(tmp_path / "home"))
    result = CliRunner().invoke(cli.app, ["link", "https://spark.example.ts.net/", "--token", "abc", "--host-label", "work-laptop"])
    assert result.exit_code == 0, result.output
    from tokentracker.collector.config import get_settings

    settings = get_settings()
    assert settings.remote_url == "https://spark.example.ts.net"
    assert settings.remote_token == "abc"
    assert settings.project_labels == "hash" and settings.project_salt
    assert settings.hostname == "work-laptop"
    assert oct(settings.config_file.stat().st_mode)[-3:] == "600"

    assert CliRunner().invoke(cli.app, ["unlink"]).exit_code == 0
    settings = get_settings()
    assert settings.remote_url is None and settings.remote_token is None
    assert settings.hostname == "work-laptop"  # label and salt survive an unlink
