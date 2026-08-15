#!/usr/bin/env python3
"""What is missing, and where to get it. SPEC §22.4.

Run before anything else, by `make setup`, by the double-clickable launchers,
and on its own:

    python3 scripts/preflight.py

**Standard library only, and it must run on a machine where nothing is
installed yet** — that is the entire point. It cannot import `passbook`, cannot
assume `uv`, and cannot assume a shell beyond the one that started it.

Every failure names the thing, the reason it is needed, and a URL. "docker: not
found" is a true statement that helps nobody.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DOCKER_DESKTOP = {
    "Windows": "https://docs.docker.com/desktop/install/windows-install/",
    "Darwin": "https://docs.docker.com/desktop/install/mac-install/",
    "Linux": "https://docs.docker.com/desktop/install/linux-install/",
}
PYTHON_DOWNLOAD = "https://www.python.org/downloads/"
UV_INSTALL = "https://docs.astral.sh/uv/getting-started/installation/"

GREEN, RED, YELLOW, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    GREEN = RED = YELLOW = DIM = OFF = ""


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def ok(self, message: str) -> None:
        print(f"  {GREEN}ok{OFF}    {message}")

    def warn(self, message: str, detail: str = "") -> None:
        print(f"  {YELLOW}warn{OFF}  {message}")
        if detail:
            print(f"        {DIM}{detail}{OFF}")
        self.warnings.append(message)

    def fail(self, message: str, fix: str) -> None:
        print(f"  {RED}MISSING{OFF}  {message}")
        for line in fix.splitlines():
            print(f"           {line}")
        self.failures.append(message)


def run(*args: str, timeout: int = 25) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def check_python(report: Report) -> None:
    version = sys.version_info
    if version < (3, 11):
        report.fail(
            f"Python {version.major}.{version.minor} — this needs 3.11 or newer",
            f"Install a current Python: {PYTHON_DOWNLOAD}",
        )
    else:
        report.ok(f"Python {version.major}.{version.minor}.{version.micro}")


def check_docker(report: Report) -> None:
    system = platform.system()
    link = DOCKER_DESKTOP.get(system, DOCKER_DESKTOP["Linux"])

    if shutil.which("docker") is None:
        report.fail(
            "Docker is not installed",
            f"Install Docker Desktop — one installer, and it sets up everything\n"
            f"else this project needs, including WSL2 on Windows:\n  {link}",
        )
        return

    code, out = run("docker", "info", "--format", "{{.ServerVersion}}")
    if code != 0:
        hint = {
            "Windows": "Start Docker Desktop from the Start menu and wait for the\nwhale icon to stop animating, then run this again.",
            "Darwin": "Open Docker Desktop from Applications and wait for the whale\nicon in the menu bar to settle, then run this again.",
        }.get(system, "Start the Docker service: sudo systemctl start docker\nIf that says permission denied: sudo usermod -aG docker $USER, then log out and back in.")
        report.fail("Docker is installed but not running", hint)
        return
    report.ok(f"Docker engine {out.splitlines()[0] if out else 'running'}")

    code, out = run("docker", "compose", "version", "--short")
    if code != 0:
        report.fail(
            "the `docker compose` plugin is missing",
            f"Docker Desktop bundles it. On a Linux server install\n"
            f"docker-compose-plugin from your package manager.\n  {link}",
        )
    else:
        report.ok(f"docker compose {out}")


def check_disk(report: Report) -> None:
    try:
        free_gb = shutil.disk_usage(ROOT).free / 1e9
    except OSError:
        return
    # Firefly + Postgres + the built web image, plus room for the database.
    if free_gb < 3:
        report.fail(
            f"only {free_gb:.1f} GB free on this disk",
            "The three images and the database need about 2.5 GB.",
        )
    else:
        report.ok(f"{free_gb:.0f} GB free disk")


def check_location(report: Report) -> None:
    """The one WSL2 rule that silently breaks Postgres. SPEC D8."""
    path = str(ROOT)
    if path.startswith("/mnt/") and "microsoft" in platform.release().lower():
        report.fail(
            f"the project is on a Windows drive ({path})",
            "Postgres cannot set file permissions across the Windows/Linux\n"
            "boundary, and it is pathologically slow. Move the folder under\n"
            "your Linux home directory:\n"
            "  cp -r . ~/passbook && cd ~/passbook",
        )
    else:
        report.ok("project is on a filesystem Postgres can use")


def port_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def env_port(name: str, default: int) -> int:
    """Read a port out of .env without importing anything."""
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(f"{name}="):
                try:
                    return int(line.split("=", 1)[1].strip().strip("\"'"))
                except ValueError:
                    pass
    return default


def check_ports(report: Report) -> None:
    """Held ports are a warning, not a failure — the setup can move them.

    On WSL with `networkingMode=mirrored` the distro shares Windows's port
    space, so a Windows service on 8080 makes the bind fail while `ss` inside
    the distro shows nothing at all. That is why the message says to look on
    the Windows side rather than assuming the check is wrong.
    """
    firefly = env_port("FIREFLY_HOST_PORT", 8080)
    ports = {firefly: "Firefly III", 8081: "the passbook web UI"}
    busy = {port: what for port, what in ports.items() if not port_free(port)}
    if not busy:
        report.ok(f"ports {', '.join(str(p) for p in sorted(ports))} are free")
        return
    for port, what in busy.items():
        # Only Firefly's host port is settable. Saying "set FIREFLY_HOST_PORT"
        # about 8081 would send someone to change a variable that has nothing to
        # do with it, which is worse than saying nothing.
        if port == firefly:
            fix = (
                "If a passbook is already running, that is fine. Otherwise set\n"
                "        FIREFLY_HOST_PORT in .env to a free port, and match APP_URL\n"
                "        and FIREFLY_URL to it — all three have to agree."
            )
        else:
            fix = (
                "If a passbook is already running, that is fine. Otherwise stop\n"
                "        whatever holds it: this port is fixed in docker-compose.yml."
            )
        report.warn(
            f"port {port} ({what}) is already in use",
            fix + f"\n        On Windows, `netstat -ano | findstr :{port}` names the process;\n"
            "        with WSL in mirrored networking the holder can be a Windows service\n"
            "        that `ss` inside the distro cannot see at all.",
        )


def check_git(report: Report) -> None:
    if shutil.which("git") is None:
        report.warn(
            "git is not installed",
            "Not needed to run passbook, but `git pull` is how you get updates,\n"
            "        and `make backup` puts the source in the backup only when it can\n"
            "        see a repository.",
        )
    else:
        report.ok("git")


def check_uv(report: Report) -> None:
    if shutil.which("uv") is None:
        report.warn(
            "uv is not installed",
            "Only needed to run the command line tool outside Docker. The web UI\n"
            f"        and `make setup` do not need it.  {UV_INSTALL}",
        )
    else:
        code, out = run("uv", "--version")
        report.ok(out or "uv")


def main() -> int:
    print(f"\npassbook preflight — {platform.system()} {platform.machine()}\n")
    report = Report()

    print("required")
    check_python(report)
    check_docker(report)
    check_disk(report)
    check_location(report)

    print("\nhelpful")
    check_ports(report)
    check_git(report)
    check_uv(report)

    print()
    if report.failures:
        print(f"{RED}{len(report.failures)} thing(s) must be installed or started first.{OFF}")
        print("Fix the MISSING lines above and run this again.\n")
        return 1
    if report.warnings:
        print(f"{YELLOW}Ready, with {len(report.warnings)} warning(s) above.{OFF}\n")
    else:
        print(f"{GREEN}Ready.{OFF}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
