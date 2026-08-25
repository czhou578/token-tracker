import subprocess
import typer
import pytest

import tokentracker.cli.cli as cli


def _fake_systemctl(calls):
    def fake_which(name):
        return "/usr/bin/systemctl" if name == "systemctl" else None

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    return fake_which, fake_run


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


def test_install_service_writes_unit_and_starts(monkeypatch, tmp_path):
    calls = []
    which, run = _fake_systemctl(calls)
    monkeypatch.setattr(cli.shutil, "which", which)
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cli.install_service()
    unit = tmp_path / "config" / "systemd" / "user" / "tokentracker.service"
    assert unit.exists()
    assert "ExecStart=" in unit.read_text()
    assert calls[0] == ["/usr/bin/systemctl", "--user", "daemon-reload"]
    assert calls[1] == ["/usr/bin/systemctl", "--user", "enable", "--now", "tokentracker.service"]


def test_uninstall_service_stops_disables_and_removes_unit(monkeypatch, tmp_path):
    calls = []
    which, run = _fake_systemctl(calls)
    monkeypatch.setattr(cli.shutil, "which", which)
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "tokentracker.service").write_text("existing")
    cli.uninstall_service()
    assert not (unit_dir / "tokentracker.service").exists()
    assert calls[0] == ["/usr/bin/systemctl", "--user", "stop", "tokentracker.service"]
    assert calls[1] == ["/usr/bin/systemctl", "--user", "disable", "tokentracker.service"]


def test_install_service_errors_without_systemctl(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    with pytest.raises(typer.Exit):
        cli.install_service()
