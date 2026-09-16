#!/usr/bin/env python3
"""What the double-clickable stop launchers run. SPEC §22.4.

The start launchers finished by printing *"To stop it: docker compose down"*,
which is a terminal command on the one path built so a person never opens a
terminal. The private repository had a stop shortcut from the beginning and
this did not, so the asymmetry was invisible: whoever wrote it already knew
the command.

Standard library only, and it must run before `uv sync` has ever happened —
the same constraint as `launch.py`, for the same reason.

**Two states that are not failures**, both of which the bare command gets
wrong: a daemon that is not running, and a stack that is already stopped.
Both mean "there is nothing to stop", which is the state this is trying to
reach, and `docker compose down` exits non-zero for the first of them.

**It confirms rather than assumes.** `down` returning 0 is a claim about a
command; counting what is left is a claim about the machine, and only the
second is worth printing a tick next to.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BOLD, GREEN, RED, DIM, OFF = (
    "\033[1m", "\033[32m", "\033[31m", "\033[2m", "\033[0m"
)
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    BOLD = GREEN = RED = DIM = OFF = ""


def hold(status: int) -> int:
    """Never vanish on failure. The launcher scripts also pause."""
    if status != 0:
        try:
            input("\nPress Enter to close.")
        except (EOFError, KeyboardInterrupt):
            pass
    return status


def main() -> int:
    print(f"\n{BOLD}passbook{OFF}\n")

    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print(f"{GREEN}Nothing to stop.{OFF}  Docker is not running.\n")
        return 0

    print("Stopping…")
    if subprocess.run(["docker", "compose", "down"], cwd=ROOT).returncode != 0:
        print(f"\n{RED}The stack did not stop cleanly.{OFF}\n")
        print("  See what is left:  docker compose ps\n")
        return hold(1)

    left = subprocess.run(
        ["docker", "compose", "ps", "--quiet"],
        cwd=ROOT, capture_output=True, text=True,
    )
    running = len([line for line in left.stdout.splitlines() if line.strip()])
    if running:
        print(f"\n{RED}{running} container(s) still running.{OFF}")
        print("  docker compose ps\n")
        return hold(1)

    print(f"\n{GREEN}Stopped.{OFF}\n")
    print(f"  {DIM}Your ledger is untouched — it lives in a volume, not in the{OFF}")
    print(f"  {DIM}containers. Start it again with the start launcher.{OFF}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ncancelled.")
        sys.exit(130)
