"""Take a database dump from inside the container. SPEC §37.

`make backup` is a host command because taking a dump needed `docker compose
exec db pg_dump`, and this container deliberately has no Docker socket (§15.1,
§15.3). That constraint is real and unchanged — but it was answering the wrong
question. **A dump does not need the Docker socket. It needs a TCP connection
to Postgres**, which this container has had all along, the same one the ledger
uses.

What that was costing: `/reapply/run` refuses without a dump from the last hour
and reads the age from `backups/`, so the single most destructive action in the
app was gated behind a terminal — and the operator who most needs a backup is
the one who has never opened one.

**What it carries, and it says which.** `make backup` also writes a git bundle
of the source. This did not, because the image holds a copy of the source
rather than a repository — so the first backup taken from the UI replaced the
host's tarball for that day with a poorer one and said nothing about it. It
writes one now: from the repository when compose mounts `.git` read-only, and
otherwise by carrying forward the bundle from the tarball it is replacing. When
there is neither, `source_bundle` is False and the response says the source is
on GitHub rather than implying a complete archive.

Still host-only: **checking that a dump restores**. That needs a scratch
database, which needs Docker. Taking a dump does not.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

BACKUPS = Path("backups")

# Matching `make backup` exactly, and the flags are not cosmetic:
# `--no-owner --no-privileges` stop pg_dump emitting `ALTER ... OWNER TO
# passbook` and `GRANT ... TO passbook`, which make the restore fail with
# `role "passbook" does not exist` on any machine where DB_USERNAME differs.
# The DR drill caught precisely that.
BUNDLE_TIMEOUT_SECONDS = 120

PG_DUMP_FLAGS = ("--clean", "--if-exists", "--no-owner", "--no-privileges")

DUMP_TIMEOUT_SECONDS = 300

# The smallest thing that could possibly be a ledger dump, in bytes of SQL
# **before compression**.
#
# Measured, not chosen: the real dump on this install is 83 KB gzipped and about
# 1.1 MB of SQL, and the ledger's schema alone is 80-odd tables — so 8 KB is far
# below any true dump and far above any failure. The floor exists because
# `pg_dump` can exit 0 having written almost nothing, which is the one backup
# failure with no symptom until the day you restore it.
#
# It counts SQL and not gzip on purpose. The first version checked the size of
# the compressed file, which measures how *repetitive* the dump is rather than
# how much of it there is: highly compressible real output looks like a failure,
# and a kilobyte of incompressible garbage looks like a success.
MIN_DUMP_BYTES = 8192


class BackupFailed(RuntimeError):
    """The backup did not complete. The message is safe to show."""


@dataclass
class BackupResult:
    dump: str
    dump_bytes: int
    config: str | None
    config_bytes: int
    # Whether `recovery/source.bundle` is in the tarball. Named rather than
    # assumed, because it is the one part of a backup that can legitimately be
    # absent — see `_source_bundle`.
    source_bundle: bool = False


def _settings() -> dict[str, str]:
    """Where to dump from — read from the same place the ledger is read from.

    This used to read `DB_HOST`/`DB_PASSWORD` out of the environment directly,
    and **compose sets neither**: the web container is handed one assembled
    `PASSBOOK_DATABASE_URL` and nothing else. So `available()` answered
    "DB_PASSWORD is not set for this container" on every install that has ever
    run, and the button this whole module exists for was never once offered.
    `pg_dump` has been sitting in the image the entire time.

    It was also wrong on the host, quietly: `DB_HOST` there is `db`, left over
    from the container that used to front this database, and `db` does not
    resolve from the host. `ledger_dsn` already knows both cases — one source
    of truth for where the database is, rather than a second one that agrees
    with the first on no install at all.
    """
    parsed: dict[str, str] = {}
    try:
        from psycopg.conninfo import conninfo_to_dict

        from .config import load_settings

        parsed = {
            key: str(value)
            for key, value in conninfo_to_dict(load_settings().ledger_dsn).items()
            if value is not None
        }
    except Exception:  # a malformed DSN, or settings that will not load
        log.debug("could not resolve the ledger DSN for a backup", exc_info=True)

    # The environment stays as a fallback so an install that sets DB_* and
    # nothing else keeps working.
    return {
        "host": parsed.get("host") or os.environ.get("DB_HOST", "db"),
        "port": parsed.get("port") or os.environ.get("DB_PORT", "5432"),
        "database": parsed.get("dbname") or os.environ.get("DB_DATABASE", "passbook"),
        "user": parsed.get("user") or os.environ.get("DB_USERNAME", "passbook"),
        "password": parsed.get("password") or os.environ.get("DB_PASSWORD", ""),
    }


def available() -> tuple[bool, str]:
    """Whether a backup can be taken here, and why not if it cannot.

    Checked before a button is offered rather than after it is pressed: an
    action that appears to be available and then explains itself is worse than
    one that explains itself up front (§18.7 learned this on the purge).
    """
    if shutil.which("pg_dump") is None:
        return False, "pg_dump is not in this image — rebuild it with `make up`."
    if not _settings()["password"]:
        return False, "the ledger connection has no password — check `.env` and run `make up`."
    if not BACKUPS.is_dir():
        return False, "backups/ is not mounted into this container — run `make up`."
    if not os.access(BACKUPS, os.W_OK):
        return False, "backups/ is mounted read-only."
    return True, ""


def run(backups: Path | None = None, today: date | None = None) -> BackupResult:
    """Write `ledger-<date>.sql.gz` and `config-<date>.tar.gz`.

    Written to a temporary file and renamed into place, so an interrupted dump
    can never replace a good backup with a truncated one — the failure that
    would only be discovered while restoring.
    """
    ok, why = available()
    if not ok:
        raise BackupFailed(why)

    backups = backups or BACKUPS
    stamp = (today or date.today()).isoformat()
    settings = _settings()

    dump = backups / f"ledger-{stamp}.sql.gz"
    partial = backups / f".ledger-{stamp}.sql.gz.partial"

    environment = {**os.environ, "PGPASSWORD": settings["password"]}
    command = [
        "pg_dump",
        "-h", settings["host"],
        "-p", str(settings["port"]),
        "-U", settings["user"],
        "-d", settings["database"],
        *PG_DUMP_FLAGS,
    ]

    sql_bytes = 0
    try:
        with partial.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb") as compressed:
                process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment
                )
                assert process.stdout is not None
                # Streamed and counted rather than `copyfileobj`'d: the count is
                # what the floor below is actually about, and holding a 1 MB dump
                # in memory to measure it afterwards would be the other way to
                # get it.
                for chunk in iter(lambda: process.stdout.read(64 * 1024), b""):
                    sql_bytes += len(chunk)
                    compressed.write(chunk)
                _, errors = process.communicate(timeout=DUMP_TIMEOUT_SECONDS)
        if process.returncode != 0:
            partial.unlink(missing_ok=True)
            # pg_dump does not echo the password; the message is safe.
            detail = (errors or b"").decode("utf-8", "replace").strip().splitlines()
            raise BackupFailed(f"pg_dump failed: {detail[-1] if detail else 'no output'}")
    except subprocess.TimeoutExpired as exc:
        process.kill()
        partial.unlink(missing_ok=True)
        raise BackupFailed(f"pg_dump timed out after {DUMP_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise BackupFailed(f"could not write the dump: {exc}") from exc

    if sql_bytes < MIN_DUMP_BYTES:
        partial.unlink(missing_ok=True)
        raise BackupFailed(
            f"pg_dump exited cleanly but produced only {sql_bytes} bytes of SQL "
            f"(a real dump is over {MIN_DUMP_BYTES // 1024} KB) — refusing to keep it"
        )

    partial.chmod(0o600)
    partial.replace(dump)
    log.warning("database dump written: %s (%d bytes)", dump.name, dump.stat().st_size)

    config, config_bytes, bundled = _config_tarball(backups, stamp)
    return BackupResult(
        dump=dump.name,
        dump_bytes=dump.stat().st_size,
        config=config,
        config_bytes=config_bytes,
        source_bundle=bundled,
    )


def _source_bundle(destination: Path, replacing: Path) -> bool:
    """Put `recovery/source.bundle` next to the config it belongs with.

    `make backup` on the host has always written one, and this module could
    not: the image holds a COPY of the source, not a repository. So the first
    backup taken from the UI replaced the host's tarball of the same day with a
    poorer one, silently. The ledger is irreplaceable and the config nearly so,
    and the source is on GitHub — but "backed up" quietly meaning two different
    things is how the day you need it becomes the day you find out.

    Two ways to have one, in order:

    1. **Write a fresh one.** A repository is reachable when compose mounts
       `.git` read-only, which it does where the Version card needs it (§123).
       `git bundle create` reads; it never writes into the repository.
    2. **Carry the existing one forward.** Otherwise take it out of the tarball
       this one is about to replace. That is not a stale copy: the source moves
       when you pull, not when you back up.

    Returns whether there is one. False is an answer, not a failure — an
    install with neither is a public clone whose source is a `git clone` away,
    and the caller says so rather than implying a complete archive.
    """
    done = subprocess.run(
        ["git", "bundle", "create", str(destination), "--all"],
        capture_output=True, text=True, timeout=BUNDLE_TIMEOUT_SECONDS, check=False,
    ) if shutil.which("git") and Path(".git").exists() else None
    if done is not None and done.returncode == 0 and destination.exists():
        return True
    if done is not None:
        log.debug("could not bundle the source: %s", done.stderr.strip()[:200])
        destination.unlink(missing_ok=True)

    if not replacing.exists():
        return False
    try:
        with tarfile.open(replacing, "r:gz") as archive:
            member = archive.extractfile("recovery/source.bundle")
            if member is None:
                return False
            destination.write_bytes(member.read())
    except (KeyError, OSError, tarfile.TarError):
        log.debug("no source bundle to carry forward from %s", replacing.name)
        destination.unlink(missing_ok=True)
        return False
    return True


def _config_tarball(backups: Path, stamp: str) -> tuple[str | None, int, bool]:
    """`config/*.yaml` plus APP_KEY. **Never `config/web-auth.json`.**

    The yaml is irreplaceable — aliases and rules are months of knowledge that
    exists nowhere else. `web-auth.json` is the opposite: a password hash, a
    TOTP secret and backup-code digests, none of which carry information and all
    of which take two minutes to recreate. Carrying them would put a LIVE second
    factor into the same archive as the password hash it is meant to be
    independent of, so one passphrase compromise would collapse two factors to
    none. SPEC §16.9.
    """
    sources = sorted(Path("config").glob("*.yaml"))
    app_key = os.environ.get("APP_KEY", "")
    if not sources and not app_key:
        return None, 0, False

    path = backups / f"config-{stamp}.tar.gz"
    partial = backups / f".config-{stamp}.tar.gz.partial"
    stage = Path(tempfile.mkdtemp(prefix="passbook-cfg-"))
    bundled = False
    try:
        recovery = stage / "recovery"
        recovery.mkdir()
        bundled = _source_bundle(recovery / "source.bundle", path)
        if app_key:
            key_file = recovery / "app-key.env"
            key_file.write_text(f"APP_KEY={app_key}\n", encoding="utf-8")
            key_file.chmod(0o600)

        with tarfile.open(partial, "w:gz") as archive:
            for source in sources:
                # Asserted rather than trusted to the glob: if this ever widens
                # to `config/*`, the credential file must not ride along.
                if source.name.endswith(".json"):
                    continue
                archive.add(source, arcname=f"config/{source.name}")
            archive.add(recovery, arcname="recovery")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    partial.chmod(0o600)
    partial.replace(path)
    return path.name, path.stat().st_size, bundled
