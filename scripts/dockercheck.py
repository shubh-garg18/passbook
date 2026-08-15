#!/usr/bin/env python3
"""What Docker needs on THIS machine, and how to fix it here. SPEC §22.4.

Shared by `preflight.py` (which reports) and `setup.py` (which offers to run the
fix). Standard library only, and it has to work on a machine where nothing is
installed yet.

**Why this is not three lines of advice.** "Start the Docker service" is four
different commands depending on where you are, and the wrong one fails in a way
that reads like a broken install:

| Situation | Correct command |
|---|---|
| Linux with systemd | `sudo systemctl start docker` |
| **WSL2 without systemd** | `sudo service docker start` — `systemctl` answers *"System has not been booted with systemd"* |
| macOS / Windows Docker Desktop | open the app; there is no service to start |
| Installed but no permission | `sudo usermod -aG docker $USER`, **then a fresh login** |

The WSL row is not hypothetical: measured on the machine this was written on —
`systemctl` exists, `systemctl is-system-running` reports `offline`, and PID 1 is
`init(Ubuntu)` rather than `systemd`. A preflight that printed the systemd
command there would send someone chasing an error that has nothing to do with
their problem.

So: detect, then name the one command that is right here.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DESKTOP_URL = {
    "Windows": "https://docs.docker.com/desktop/install/windows-install/",
    "Darwin": "https://docs.docker.com/desktop/install/mac-install/",
    "Linux": "https://docs.docker.com/desktop/install/linux-install/",
}


@dataclass
class Fix:
    """One step. `command` is None when only a human can do it."""

    label: str
    command: list[str] | None = None
    url: str | None = None
    note: str = ""
    needs_sudo: bool = False
    then_relogin: bool = False


@dataclass
class Diagnosis:
    ok: bool
    kind: str = ""            # "desktop" | "engine" | ""
    version: str = ""
    problem: str = ""
    fixes: list[Fix] = field(default_factory=list)


def _run(*args: str, timeout: int = 25) -> tuple[int, str]:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return done.returncode, (done.stdout + done.stderr).strip()


def is_wsl() -> bool:
    return "microsoft" in platform.release().lower()


def systemd_running() -> bool:
    """Whether systemd is actually PID 1 and up.

    `shutil.which("systemctl")` is not the question — WSL images ship the binary
    whether or not `[boot] systemd=true` is set. `is-system-running` answers
    `offline` and exits non-zero when it is not booted under systemd, and PID 1
    is the second opinion.
    """
    if not shutil.which("systemctl"):
        return False
    code, out = _run("systemctl", "is-system-running", timeout=8)
    if out.strip() in {"offline", "unknown"}:
        return False
    comm = Path("/proc/1/comm")
    if comm.exists() and comm.read_text().strip() != "systemd":
        return False
    return code == 0 or out.strip() in {"running", "degraded", "starting", "maintenance"}


def in_docker_group() -> bool:
    code, out = _run("id", "-nG")
    return code == 0 and "docker" in out.split()


def package_installer() -> tuple[str, list[str]] | None:
    """(family, command prefix) for this distro's package manager."""
    release = Path("/etc/os-release")
    ids = ""
    if release.exists():
        for line in release.read_text().splitlines():
            if line.startswith(("ID=", "ID_LIKE=")):
                ids += line.split("=", 1)[1].strip().strip('"') + " "
    ids = ids.lower()
    if any(name in ids for name in ("debian", "ubuntu")):
        return "debian", ["sudo", "apt-get", "install", "-y"]
    if any(name in ids for name in ("fedora", "rhel", "centos")):
        return "fedora", ["sudo", "dnf", "install", "-y"]
    if "arch" in ids:
        return "arch", ["sudo", "pacman", "-S", "--noconfirm"]
    return None


def start_command() -> list[str]:
    """The one right way to start the daemon here."""
    if systemd_running():
        return ["sudo", "systemctl", "start", "docker"]
    return ["sudo", "service", "docker", "start"]


def _install_fixes(system: str) -> list[Fix]:
    url = DESKTOP_URL.get(system, DESKTOP_URL["Linux"])
    if system == "Linux":
        fixes = [
            Fix(
                "install Docker Engine",
                command=["sh", "-c", "curl -fsSL https://get.docker.com | sh"],
                needs_sudo=True,
                note="Docker's own installer. No GUI, nothing running in the tray.",
            ),
            Fix(
                "add yourself to the docker group",
                command=["sudo", "usermod", "-aG", "docker", os.environ.get("USER", "")],
                needs_sudo=True,
                then_relogin=True,
                note="so `docker` needs no sudo. Takes effect on your next login.",
            ),
        ]
        if is_wsl():
            fixes.append(Fix(
                "or install Docker Desktop on Windows and enable WSL integration",
                # The WINDOWS installer: under WSL the host OS is Windows, and
                # platform.system() saying "Linux" is exactly the trap here.
                url=DESKTOP_URL["Windows"],
                note="Either works. Engine inside the distro needs no Windows-side app.",
            ))
        return fixes
    if system == "Darwin":
        fixes = []
        if shutil.which("brew"):
            fixes.append(Fix(
                "install Docker Desktop",
                command=["brew", "install", "--cask", "docker"],
                note="then open it from Applications once, to accept its terms.",
            ))
            fixes.append(Fix(
                "or Colima, if you would rather have no GUI",
                command=["brew", "install", "colima", "docker", "docker-compose"],
                note="UNTESTED by this project — see SETUP.md before relying on it.",
            ))
        fixes.append(Fix("install Docker Desktop", url=url,
                         note="pick the build matching your chip."))
        return fixes
    return [Fix("install Docker Desktop", url=url,
                note="let it enable WSL2 and restart if it asks.")]


def diagnose() -> Diagnosis:
    """What is wrong with Docker here, and the fix for HERE."""
    system = platform.system()

    if shutil.which("docker") is None:
        return Diagnosis(
            ok=False, problem="Docker is not installed", fixes=_install_fixes(system)
        )

    code, out = _run("docker", "info", "--format", "{{.ServerVersion}}|{{.OperatingSystem}}")
    if code == 0 and out:
        version, _, os_name = out.splitlines()[0].partition("|")
        kind = "desktop" if "Docker Desktop" in os_name else "engine"
        diagnosis = Diagnosis(ok=True, kind=kind, version=version)
        if _run("docker", "compose", "version", "--short")[0] != 0:
            diagnosis.ok = False
            diagnosis.problem = "the `docker compose` plugin is missing"
            installer = package_installer()
            if installer:
                family, prefix = installer
                package = {"debian": "docker-compose-plugin",
                           "fedora": "docker-compose-plugin",
                           "arch": "docker-compose"}[family]
                diagnosis.fixes = [Fix(f"install {package}",
                                       command=prefix + [package], needs_sudo=True)]
            else:
                diagnosis.fixes = [Fix("install the compose plugin",
                                       url=DESKTOP_URL.get(system, DESKTOP_URL["Linux"]))]
        return diagnosis

    # Installed but the daemon did not answer. Which of the three reasons?
    lowered = out.lower()
    if "permission denied" in lowered and not in_docker_group():
        return Diagnosis(
            ok=False,
            problem="Docker is installed but this user may not talk to it",
            fixes=[Fix(
                "add yourself to the docker group",
                command=["sudo", "usermod", "-aG", "docker", os.environ.get("USER", "")],
                needs_sudo=True, then_relogin=True,
                note="group membership only applies to new sessions.",
            )],
        )

    if system in ("Darwin", "Windows"):
        return Diagnosis(
            ok=False,
            problem="Docker Desktop is installed but not running",
            fixes=[Fix(
                "start Docker Desktop and wait for the whale icon to settle",
                note="there is no service to start; it is the app itself.",
            )],
        )

    fixes = [Fix("start the Docker daemon", command=start_command(), needs_sudo=True)]
    if is_wsl() and not systemd_running():
        # Measured on this machine: systemctl exists, systemd is offline, PID 1
        # is init(Ubuntu). `systemctl start docker` fails here with "System has
        # not been booted with systemd", which reads like a broken Docker.
        fixes.append(Fix(
            "make that survive a reboot",
            note=("add `[boot]\\ncommand = service docker start` to /etc/wsl.conf, "
                  "then run `wsl --shutdown` from PowerShell once. Without it the "
                  "daemon has to be started by hand after every restart."),
        ))
    return Diagnosis(
        ok=False, problem="Docker is installed but the daemon is not running", fixes=fixes
    )


def summary() -> str:
    """One line, for a report."""
    d = diagnose()
    if d.ok:
        flavour = {"desktop": "Docker Desktop", "engine": "Docker Engine"}.get(d.kind, "Docker")
        return f"{flavour} {d.version}"
    return d.problem


if __name__ == "__main__":
    d = diagnose()
    print(("ok    " if d.ok else "PROBLEM  ") + (summary()))
    for fix in d.fixes:
        print(f"  - {fix.label}")
        if fix.command:
            print(f"      {' '.join(fix.command)}")
        if fix.url:
            print(f"      {fix.url}")
        if fix.note:
            print(f"      {fix.note}")
