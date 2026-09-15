"""Operations the UI reports on, and the one it can run. SPEC §15.

**Read-only by design, with one exception.** Running `make backup`,
`backup-remote` or `verify-backup` from the web container would need the Docker
socket — `backup` shells into the db container and `verify-backup` starts a
scratch Postgres. Mounting the socket into the one container that listens on a
port, parses untrusted uploads and is destined for Tailscale is equivalent to
granting it root on the host. The blast radius of a web compromise would go
from "the ledger" to "the machine, plus the ability to
delete every backup including the off-site copies".

So the UI *reports* backup health and the operator *runs* backups from the
host. This module reads `backups/` and, if rclone happens to be available,
lists the remote. It holds no passphrase and no rclone credential, and the
container is given neither.

The single exception is re-apply (§15.2), which needs no new privilege: it
talks to the ledger over HTTP with the token it already has.
"""

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

BACKUPS = Path("backups")

# Past this, the local backup is old enough to be worth saying so about. A
# backup is only as good as its last run, and nothing here is scheduled (D7).
BACKUP_STALE_DAYS = 7


@dataclass
class Artefact:
    name: str
    size: int
    modified: str
    age_days: int

    @property
    def human_size(self) -> str:
        value = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
            value /= 1024
        return f"{value:,.1f} GB"


def _artefact(path: Path) -> Artefact:
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime)
    return Artefact(
        name=path.name,
        size=stat.st_size,
        modified=modified.strftime("%Y-%m-%d %H:%M"),
        age_days=(datetime.now() - modified).days,
    )


def local_backups(backups: Path = BACKUPS, limit: int = 12) -> list[Artefact]:
    if not backups.is_dir():
        return []
    files = sorted(
        (p for p in backups.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [_artefact(p) for p in files[:limit]]


# How fresh a database dump has to be before the UI will run a purge-and-re-push
# (§15.2, §18.7). Not a warning — a precondition.
#
# The container cannot take a dump (that needs the Docker socket, which is the
# whole point of §15.1) but it CAN read `backups/`, so "a dump exists and is
# recent" is a fact it can check and refuse on. An hour is long enough to run
# `make backup` and then do the editing that led here, and short enough that the
# dump is of the ledger being deleted rather than of some earlier one.
REAPPLY_DUMP_MAX_AGE_MINUTES = 60


def newest_dump(backups: Path = BACKUPS) -> tuple[str, int] | None:
    """`(filename, age in minutes)` of the newest database dump, or None."""
    if not backups.is_dir():
        return None
    # Both names: an install that has been upgraded still has dumps written
    # under the old one, and a backup that stops being visible the moment it
    # is most needed is worse than no backup listing at all.
    dumps = [
        p
        for pattern in ("ledger-*.sql.gz", "the ledger-*.sql.gz")
        for p in backups.glob(pattern)
        if p.is_file()
    ]
    if not dumps:
        return None
    newest = max(dumps, key=lambda p: p.stat().st_mtime)
    age = datetime.now() - datetime.fromtimestamp(newest.stat().st_mtime)
    return newest.name, int(age.total_seconds() // 60)


def backup_age(backups: Path = BACKUPS) -> int | None:
    """Age in days of the newest database dump, or None if there is none."""
    dump = newest_dump(backups)
    return None if dump is None else dump[1] // (60 * 24)



_RCLONE_LINE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")


def remote_backups(remote: str | None, limit: int = 12) -> tuple[list[Artefact], str | None]:
    """List the off-site archives. Returns (artefacts, error).

    Best-effort: rclone is not installed in the web container and is not
    expected to be. A missing binary is reported as "not available here",
    which is accurate rather than alarming.
    """
    if not remote:
        return [], "No off-site copy is set up yet. `make backup-remote` on the host does it."
    if shutil.which("rclone") is None:
        return [], "Off-site copies run from the host — `make backup-remote`."

    try:
        result = subprocess.run(
            ["rclone", "lsl", f"{remote}/"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"rclone did not answer: {exc}"
    if result.returncode != 0:
        # stderr can name the remote but never a credential.
        return [], f"rclone failed: {result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'unknown error'}"

    out: list[Artefact] = []
    for line in result.stdout.splitlines():
        # `rclone lsl` -> "  <size> <YYYY-MM-DD HH:MM:SS.ffffff> <name>"
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        size, date, _time, name = parts[0], parts[1], parts[2], parts[3]
        try:
            size_int = int(size)
        except ValueError:
            continue
        try:
            age = (datetime.now() - datetime.strptime(date, "%Y-%m-%d")).days
        except ValueError:
            age = -1
        out.append(Artefact(name=name, size=size_int, modified=date, age_days=age))
    out.sort(key=lambda a: a.name, reverse=True)
    return out[:limit], None
