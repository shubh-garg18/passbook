"""The first-run wizard's parsing, which is where it silently gets things wrong.

`scripts/setup.py` cannot be imported as part of the package — it is a
standard-library-only script that must run before `uv sync` has happened. It is
loaded by path here.

Only the pure parts are tested. What used to be here was most of the file:
shape-checking an API token, reading a validation error out of a registration
page, and the minimum password length a separate application enforced. There is
no separate application and no token, so those tests went with the code they
guarded — and the wizard is two steps shorter for it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT

spec = importlib.util.spec_from_file_location(
    "passbook_setup_wizard", REPO_ROOT / "scripts" / "setup.py"
)
wizard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wizard
spec.loader.exec_module(wizard)


def test_env_values_are_quoted_when_they_contain_spaces(tmp_path, monkeypatch):
    """An unquoted space in `.env` is a syntax error to `set -a; . ./.env`,
    which three make targets do — and an asset account name almost always has
    one. SPEC §7.2."""
    env = tmp_path / ".env"
    env.write_text("PASSBOOK_ASSET_ACCOUNT=\nOTHER=1\n")
    monkeypatch.setattr(wizard, "ENV", env)
    wizard.set_env("PASSBOOK_ASSET_ACCOUNT", "Bank savings")
    wizard.set_env("OTHER", "2")
    body = env.read_text()
    assert 'PASSBOOK_ASSET_ACCOUNT="Bank savings"' in body
    assert "OTHER=2" in body, "a value with no space needs no quotes"


def test_set_env_replaces_in_place_and_never_appends_a_duplicate(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("PASSBOOK_ACCOUNT_NUMBER=old\n")
    monkeypatch.setattr(wizard, "ENV", env)
    wizard.set_env("PASSBOOK_ACCOUNT_NUMBER", "new")
    assert env.read_text().count("PASSBOOK_ACCOUNT_NUMBER=") == 1
    assert "new" in env.read_text()


def test_reading_a_statement_yields_what_the_account_needs(tmp_path):
    """Account number, opening balance and the date it applies from all come out
    of the file — so nobody has to be told twice not to enter their CURRENT
    balance, which is what the previous store's setup invites."""
    details = wizard.read_statement(REPO_ROOT / "tests" / "fixtures" / "statement.xls")
    assert details is not None
    number, opening, first = details
    assert number.endswith("1111")
    assert str(opening) == "10000.00"
    assert first.isoformat() == "2026-05-09"


def test_an_unreadable_file_is_none_rather_than_a_crash(tmp_path):
    """The wizard falls back to asking. It must not traceback at someone who
    dropped the wrong file in inbox/."""
    junk = tmp_path / "not-a-statement.xls"
    junk.write_bytes(b"nothing here")
    assert wizard.read_statement(junk) is None


# ── what Docker needs on THIS machine ────────────────────────────────────────
# "Start the Docker service" is four different commands depending on where you
# are, and the wrong one fails in a way that reads like a broken install.

docker = importlib.util.module_from_spec(
    importlib.util.spec_from_file_location(
        "passbook_dockercheck", REPO_ROOT / "scripts" / "dockercheck.py"
    )
)
sys.modules["passbook_dockercheck"] = docker
docker.__loader__.exec_module(docker)


def test_systemd_is_judged_by_whether_it_is_running_not_installed(monkeypatch):
    """WSL images ship `systemctl` whether or not `[boot] systemd=true` is set.

    MEASURED on the machine this was written on: the binary is on PATH,
    `is-system-running` answers `offline`, and PID 1 is `init(Ubuntu)`. Treating
    "systemctl exists" as "systemd works" sends the user to a command that fails
    with "System has not been booted with systemd" — an error about something
    other than their problem.
    """
    monkeypatch.setattr(docker.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(docker, "_run", lambda *a, **k: (1, "offline"))
    assert docker.systemd_running() is False
    assert docker.start_command() == ["sudo", "service", "docker", "start"]


def test_a_real_systemd_gets_systemctl(monkeypatch, tmp_path):
    monkeypatch.setattr(docker.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(docker, "_run", lambda *a, **k: (0, "running"))
    comm = tmp_path / "comm"
    comm.write_text("systemd\n")
    monkeypatch.setattr(docker, "Path", lambda p: comm if p == "/proc/1/comm" else Path(p))
    assert docker.systemd_running() is True
    assert docker.start_command() == ["sudo", "systemctl", "start", "docker"]


def test_no_systemctl_at_all_falls_back_to_service(monkeypatch):
    monkeypatch.setattr(docker.shutil, "which", lambda name: None)
    assert docker.systemd_running() is False
    assert docker.start_command() == ["sudo", "service", "docker", "start"]


def test_a_permission_problem_is_not_reported_as_a_dead_daemon(monkeypatch):
    """Two different problems with two different fixes. Conflating them sends
    someone to restart a daemon that is already running."""
    monkeypatch.setattr(docker.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(docker, "_run", lambda *a, **k: (
        (1, "permission denied while trying to connect to the Docker daemon socket")
        if a[:2] == ("docker", "info") else (0, "nogroup")
    ))
    result = docker.diagnose()
    assert result.ok is False
    assert "may not talk to it" in result.problem
    assert result.fixes[0].command[:3] == ["sudo", "usermod", "-aG"]
    assert result.fixes[0].then_relogin, "usermod does not touch the current session"


def test_a_running_docker_reports_which_kind(monkeypatch):
    monkeypatch.setattr(docker.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(docker, "_run", lambda *a, **k: (0, "27.0.1|Docker Desktop"))
    result = docker.diagnose()
    assert result.ok and result.kind == "desktop" and result.version == "27.0.1"


def test_wsl_is_offered_the_windows_installer_not_the_linux_one(monkeypatch):
    """Under WSL `platform.system()` says Linux, and the Docker Desktop a user
    would install runs on Windows. That is exactly the trap."""
    monkeypatch.setattr(docker.shutil, "which", lambda name: None)
    monkeypatch.setattr(docker, "is_wsl", lambda: True)
    monkeypatch.setattr(docker.platform, "system", lambda: "Linux")
    urls = [f.url for f in docker.diagnose().fixes if f.url]
    assert urls and all("windows-install" in u for u in urls)


def test_nothing_destructive_is_marked_runnable_without_sudo_being_named():
    """Every command that changes the machine says so, so the wizard can ask."""
    monkey = docker.Fix("x", command=["sudo", "usermod"], needs_sudo=True)
    assert monkey.needs_sudo
    for fix in docker._install_fixes("Linux"):
        if fix.command and any(part == "sudo" for part in fix.command):
            assert fix.needs_sudo, f"{fix.label} runs sudo without declaring it"


# --- finding a statement without moving files by hand ------------------------


def test_only_statement_shaped_files_are_offered(tmp_path, monkeypatch):
    """Offering somebody their tax PDF would be worse than asking."""
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    for name in ("statement.xls", "photo.jpg", "notes.txt", "acct.pdf"):
        (downloads / name).write_text("x")
    monkeypatch.setattr(wizard.Path, "home", staticmethod(lambda: tmp_path))

    offered = {p.name for p in wizard.recent_downloads()}
    assert offered == {"statement.xls", "acct.pdf"}


def test_the_download_is_copied_never_moved(tmp_path, monkeypatch):
    """A setup step that relocates somebody's file is one that loses it when
    they re-run."""
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    source = downloads / "statement.xls"
    source.write_text("x")
    monkeypatch.setattr(wizard.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(wizard, "ROOT", tmp_path / "repo")
    (tmp_path / "repo").mkdir()
    monkeypatch.setattr(wizard, "ask", lambda *a, **k: "1")

    chosen = wizard.offer_a_statement()
    assert chosen is not None and chosen.name == "statement.xls"
    assert source.is_file(), "the original download must survive"


def test_declining_leaves_inbox_alone(tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "statement.xls").write_text("x")
    monkeypatch.setattr(wizard.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(wizard, "ROOT", tmp_path / "repo")
    (tmp_path / "repo").mkdir()
    monkeypatch.setattr(wizard, "ask", lambda *a, **k: "0")

    assert wizard.offer_a_statement() is None
    assert not (tmp_path / "repo" / "inbox").exists()
