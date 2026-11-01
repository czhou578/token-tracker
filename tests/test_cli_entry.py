import subprocess
import sys


def test_cli_reachable_as_module_from_any_cwd(tmp_path):
    """`python -m tokentracker.cli.cli` must work from any directory, not just the
    project root, because the package is importable from site-packages."""
    result = subprocess.run(
        [sys.executable, "-m", "tokentracker.cli.cli", "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "dashboard" in result.stdout
    assert "install-service" in result.stdout
    assert "uninstall-service" in result.stdout
    assert "status" in result.stdout


def test_package_entry_reachable_as_module_from_any_cwd(tmp_path):
    """`python -m tokentracker` (the package __main__) must also work from any directory."""
    result = subprocess.run(
        [sys.executable, "-m", "tokentracker", "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "dashboard" in result.stdout
    assert "install-service" in result.stdout
