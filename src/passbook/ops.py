"""Operations the UI reports on, and the one it can run. SPEC §15.

**Read-only by design, with one exception.** Running `make backup`,
`backup-remote` or `verify-backup` from the web container would need the Docker
socket — `backup` shells into the db container and `verify-backup` starts a
scratch Postgres. Mounting the socket into the one container that listens on a
port, parses untrusted uploads and is destined for Tailscale is equivalent to
granting it root on the host. The blast radius of a web compromise would go
from "the ledger and the Firefly token" to "the machine, plus the ability to
delete every backup including the off-site copies".

So the UI *reports* backup health and the operator *runs* backups from the
host. This module reads `backups/` and, if rclone happens to be available,
lists the remote. It holds no passphrase and no rclone credential, and the
container is given neither.

The single exception is re-apply (§15.2), which needs no new privilege: it
talks to Firefly over HTTP with the token it already has.
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
    dumps = [p for p in backups.glob("firefly-*.sql.gz") if p.is_file()]
    if not dumps:
        return None
    newest = max(dumps, key=lambda p: p.stat().st_mtime)
    age = datetime.now() - datetime.fromtimestamp(newest.stat().st_mtime)
    return newest.name, int(age.total_seconds() // 60)


def backup_age(backups: Path = BACKUPS) -> int | None:
    """Age in days of the newest database dump, or None if there is none."""
    dump = newest_dump(backups)
    return None if dump is None else dump[1] // (60 * 24)


# --- purge intent: making an interrupted purge detectable ---------------------
# SPEC §19.7. A purge that can die mid-flight has to leave evidence, because the
# state it leaves behind is *coherent*: on 2026-08-11 a purge completed and the
# re-push stopped after 21 of 93 rows, and the result was a ledger with a
# self-consistent balance and no error anywhere (§19).
#
# So intent is recorded BEFORE the first delete and cleared only after the
# re-push is verified. An intent file that outlives its run therefore means
# exactly one thing — an unfinished cycle — and it says what remains.
#
# A plain JSON file in backups/ rather than a row in Firefly: the whole point is
# to survive the thing being interrupted, including Firefly being unreachable.
# `ops.py` never executes anything but rclone (asserted by
# `test_ops_only_ever_executes_rclone`), so this is file I/O only.

INTENT_GLOB = "purge-intent-*.json"

STAGES = ("purging", "purged", "repushing", "done")


def write_purge_intent(
    account: str,
    external_ids: list[str],
    statements: list[str],
    backups: Path = BACKUPS,
    slug: str = "",
) -> Path:
    """Record what is about to be deleted and what must be pushed back.

    `slug` names the account in registry terms (§21): with more than one account
    the Firefly asset-account name is not enough to resume against, and the
    recorded external_ids are namespaced by it.
    """
    import json

    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = backups / f"purge-intent-{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "created": datetime.now().isoformat(timespec="seconds"),
                "account": account,
                "slug": slug,
                "stage": "purging",
                "external_ids": sorted(external_ids),
                "expected_rows": len(set(external_ids)),
                "statements": statements,
                "deleted": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    log.warning("purge intent recorded at %s (%d row(s))", path, len(external_ids))
    return path


def read_purge_intent(path: Path) -> dict:
    import json

    return json.loads(Path(path).read_text(encoding="utf-8"))


def update_purge_intent(path: Path, **fields) -> dict:
    """Advance the record. Written in place so a crash leaves the last stage."""
    import json

    data = read_purge_intent(path)
    data.update(fields)
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def outstanding_purge_intents(backups: Path = BACKUPS) -> list[Path]:
    """Intent files whose run never finished. Oldest first."""
    if not backups.is_dir():
        return []
    out = []
    for path in sorted(backups.glob(INTENT_GLOB)):
        try:
            if read_purge_intent(path).get("stage") != "done":
                out.append(path)
        except (ValueError, OSError):
            # An unreadable intent file is itself an unfinished cycle: it was
            # being written when something stopped. Never silently ignored.
            out.append(path)
    return out


def clear_purge_intent(path: Path) -> None:
    """Mark done and remove. Only ever called after the ledger is verified."""
    path = Path(path)
    try:
        update_purge_intent(path, stage="done")
    except (ValueError, OSError):
        pass
    path.unlink(missing_ok=True)
    log.info("purge intent %s cleared", path.name)


_RCLONE_LINE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")


def remote_backups(remote: str | None, limit: int = 12) -> tuple[list[Artefact], str | None]:
    """List the off-site archives. Returns (artefacts, error).

    Best-effort: rclone is not installed in the web container and is not
    expected to be. A missing binary is reported as "not available here",
    which is accurate rather than alarming.
    """
    if not remote:
        return [], "PASSBOOK_RCLONE_REMOTE is not set"
    if shutil.which("rclone") is None:
        return [], "rclone is not available in this container (by design — see §15.3)"

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
