#!/usr/bin/env python3
"""What the double-clickable launchers run. SPEC §22.4.

One entry point with one decision: has this install been set up?

- **No** -> hand over to `scripts/setup.py`, the first-run wizard.
- **Yes** -> start the stack and open the browser.

Standard library only, and it must run before `uv sync` has ever happened.
It cannot import `passbook`.

**It always waits for a keypress on failure.** A launcher that closes its own
window on error tells the user nothing at all, which is worse than not existing:
they double-click, a window flashes, and there is no way to find out why.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"

BOLD, GREEN, RED, YELLOW, DIM, OFF = (
    "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
)
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    BOLD = GREEN = RED = YELLOW = DIM = OFF = ""

WEB = "http://localhost:8081"


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV.is_file():
        return values
    for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("\"'")
    return values


def configured(env: dict[str, str]) -> bool:
    """The two that mean this install has been through the wizard.

    They used to be `:?required` in compose, which made a blank one abort the
    whole stack with an interpolation error naming a variable the user had
    never seen — and, worse, made the wizard itself impossible, because the
    wizard reads them out of the first statement *after* starting the stack.
    They are `:-` now and the app reports them as not configured, which is the
    honest state; this stays as the test for whether to run the wizard.
    """
    return all(env.get(key) for key in
               ("PASSBOOK_ACCOUNT_NUMBER", "PASSBOOK_ASSET_ACCOUNT"))


def up(*services: str) -> int:
    return subprocess.run(
        ["docker", "compose", "up", "-d", "--wait", *services], cwd=ROOT
    ).returncode


def responding(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status < 500
    except (urllib.error.HTTPError,):
        return True  # answering at all is what is being asked
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def wait_for(url: str, seconds: int = 120) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if responding(url):
            return True
        print(".", end="", flush=True)
        time.sleep(2)
    return False


def stopper() -> str:
    """The stop launcher for whichever platform this is.

    Named rather than guessed at by the reader: three files exist and only one
    of them is the one in front of them.
    """
    return {
        "nt": "launchers\\stop-passbook.cmd",
        "darwin": "launchers/stop-passbook.command",
    }.get("nt" if os.name == "nt" else sys.platform, "launchers/stop-passbook.sh")


def hold(status: int) -> int:
    """Never vanish. The launcher scripts also pause, belt and braces."""
    if status != 0:
        try:
            input("\nPress Enter to close.")
        except (EOFError, KeyboardInterrupt):
            pass
    return status


def main() -> int:
    print(f"\n{BOLD}passbook{OFF}\n")

    if subprocess.run(
        ["docker", "info"], capture_output=True
    ).returncode != 0:
        print(f"{RED}Docker is not running.{OFF}\n")
        print("  Start Docker Desktop and wait for the whale icon to settle,")
        print("  then run this again.\n")
        print(f"  {DIM}For the full list of what is needed:{OFF}")
        print(f"  {DIM}python3 scripts/preflight.py{OFF}")
        return hold(1)

    env = read_env()
    if not ENV.is_file() or not configured(env):
        print(f"{YELLOW}This install is not set up yet.{OFF}")
        print("Starting the first-run wizard.\n")
        return hold(
            subprocess.run([sys.executable, str(ROOT / "scripts" / "setup.py")], cwd=ROOT).returncode
        )

    print("Starting…")
    if up() != 0:
        print(f"\n{RED}The stack did not start.{OFF}\n")
        print("  See what went wrong:  docker compose logs --tail=50\n")
        return hold(1)

    print("Waiting for the web UI ", end="", flush=True)
    if not wait_for(f"{WEB}/api/session"):
        print(f"\n\n{RED}The web UI did not answer.{OFF}")
        print("  docker compose logs web\n")
        return hold(1)

    print(f"\n\n{GREEN}Ready.{OFF}  {BOLD}{WEB}{OFF}\n")
    try:
        webbrowser.open(WEB)
    except Exception:  # noqa: BLE001 — the URL is printed either way
        pass
    print(f"  {DIM}Leave this window open or close it — the stack keeps running.{OFF}")
    # Not `docker compose down`. This is the one path built so that a person
    # never opens a terminal, and it ended by giving them a terminal command.
    print(f"  {DIM}To stop it, double-click {stopper()}{OFF}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
