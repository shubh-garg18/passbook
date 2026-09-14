"""Taking a backup from the UI. SPEC §37.

The dump itself is `pg_dump` talking to a real server, which these cannot have.
What they can pin is everything around it — and everything around it is where
the danger lives, because a backup only fails in a way you notice on the day you
need it.

So: that a half-written dump can never replace a good one, that a dump which
"succeeded" but is a few hundred bytes is refused, that the credential file is
not in the config archive, and that the result never claims a source bundle it
did not write.
"""

from __future__ import annotations

import gzip
import subprocess
import tarfile
from datetime import date
from pathlib import Path

import pytest

from passbook import backup


class FakePgDump:
    """A `pg_dump` that writes what it is told to, and exits how it is told to."""

    def __init__(self, payload: bytes = b"", code: int = 0, errors: bytes = b""):
        self.payload = payload
        self.code = code
        self.errors = errors
        self.command: list[str] | None = None
        self.env: dict | None = None

    def __call__(self, command, stdout=None, stderr=None, env=None):
        self.command = command
        self.env = env
        return _Process(self.payload, self.code, self.errors)


class _Process:
    def __init__(self, payload: bytes, code: int, errors: bytes):
        import io

        self.stdout = io.BytesIO(payload)
        self.returncode = code
        self._errors = errors

    def communicate(self, timeout=None):
        return b"", self._errors

    def kill(self):
        pass


@pytest.fixture
def ready(tmp_path, monkeypatch):
    """A writable `backups/`, a password, and a `pg_dump` on PATH."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "backups").mkdir()
    (tmp_path / "config").mkdir()
    monkeypatch.setenv("DB_PASSWORD", "s3cret")
    monkeypatch.setattr(backup.shutil, "which", lambda _: "/usr/bin/pg_dump")
    return tmp_path


def _plausible_dump() -> bytes:
    """Over the floor in SQL bytes, and shaped like a dump rather than noise.

    Deliberately repetitive: it gzips down to a few hundred bytes, which is the
    case the compressed-size floor used to reject.
    """
    return b"--\n-- PostgreSQL database dump\n--\n" + b"CREATE TABLE x (id int);\n" * 500


# --- availability, before a button is offered --------------------------------


def test_unavailable_without_pg_dump(ready, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda _: None)
    ok, why = backup.available()
    assert not ok
    assert "pg_dump" in why


def test_unavailable_without_a_password(ready, monkeypatch):
    monkeypatch.delenv("DB_PASSWORD")
    ok, why = backup.available()
    assert not ok
    assert "DB_PASSWORD" in why


def test_unavailable_when_backups_is_not_mounted(ready):
    (ready / "backups").rmdir()
    ok, why = backup.available()
    assert not ok
    assert "not mounted" in why


def test_available_when_everything_is_there(ready):
    assert backup.available() == (True, "")


def test_run_refuses_rather_than_half_working(ready, monkeypatch):
    monkeypatch.delenv("DB_PASSWORD")
    with pytest.raises(backup.BackupFailed, match="DB_PASSWORD"):
        backup.run()


# --- the dump ----------------------------------------------------------------


def test_writes_a_gzipped_dump_and_a_config_archive(ready, monkeypatch):
    (ready / "config" / "rules.yaml").write_text("categories: {}\n")
    monkeypatch.setenv("APP_KEY", "base64:abc")
    monkeypatch.setattr(backup.subprocess, "Popen", FakePgDump(_plausible_dump()))

    result = backup.run(today=date(2026, 9, 3))

    assert result.dump == "firefly-2026-09-03.sql.gz"
    dump = ready / "backups" / result.dump
    assert gzip.decompress(dump.read_bytes()).startswith(b"--\n-- PostgreSQL")
    assert result.dump_bytes == dump.stat().st_size

    assert result.config == "config-2026-09-03.tar.gz"
    with tarfile.open(ready / "backups" / result.config) as archive:
        assert set(archive.getnames()) >= {"config/rules.yaml", "recovery/app-key.env"}


def test_both_files_are_owner_only(ready, monkeypatch):
    (ready / "config" / "rules.yaml").write_text("categories: {}\n")
    monkeypatch.setattr(backup.subprocess, "Popen", FakePgDump(_plausible_dump()))
    result = backup.run(today=date(2026, 9, 3))

    for name in (result.dump, result.config):
        mode = (ready / "backups" / name).stat().st_mode & 0o777
        assert mode == 0o600, f"{name} is {oct(mode)}"


def test_the_password_is_passed_by_environment_not_on_the_command_line(ready, monkeypatch):
    """A password in argv is readable by every process on the box via /proc."""
    fake = FakePgDump(_plausible_dump())
    monkeypatch.setattr(backup.subprocess, "Popen", fake)
    backup.run(today=date(2026, 9, 3))

    assert fake.command is not None
    assert not any("s3cret" in part for part in fake.command)
    assert fake.env is not None and fake.env["PGPASSWORD"] == "s3cret"


# --- the failures that matter ------------------------------------------------


def test_a_failing_pg_dump_leaves_no_file_at_all(ready, monkeypatch):
    monkeypatch.setattr(
        backup.subprocess,
        "Popen",
        FakePgDump(b"", code=1, errors=b'pg_dump: error: connection to server failed\n'),
    )
    with pytest.raises(backup.BackupFailed, match="connection to server failed"):
        backup.run(today=date(2026, 9, 3))

    assert list((ready / "backups").iterdir()) == []


def test_a_dump_that_succeeded_but_is_tiny_is_refused(ready, monkeypatch):
    """The failure mode with no symptom: exit 0, and nothing in the file."""
    monkeypatch.setattr(backup.subprocess, "Popen", FakePgDump(b"-- empty\n"))
    with pytest.raises(backup.BackupFailed, match="refusing to keep it"):
        backup.run(today=date(2026, 9, 3))

    assert list((ready / "backups").iterdir()) == []


def test_the_floor_measures_sql_not_gzip(ready, monkeypatch):
    """A real dump that happens to compress well is not a failed dump.

    The first floor checked the size of the .gz, which measures repetitiveness.
    This payload is 12 KB of valid SQL and about 200 bytes compressed — under
    the old check it was rejected, and it is a perfectly good backup.
    """
    payload = b"INSERT INTO x VALUES (1);\n" * 500
    assert len(payload) > backup.MIN_DUMP_BYTES
    monkeypatch.setattr(backup.subprocess, "Popen", FakePgDump(payload))

    result = backup.run(today=date(2026, 9, 3))
    written = (ready / "backups" / result.dump).stat().st_size
    assert written < backup.MIN_DUMP_BYTES, "the point of the test is that it compresses hard"
    assert gzip.decompress((ready / "backups" / result.dump).read_bytes()) == payload


def test_a_failed_dump_cannot_replace_yesterdays_good_one(ready, monkeypatch):
    """The whole reason for the .partial dance.

    Written straight to the destination, a dump that died halfway would leave a
    truncated file with today's name — and the operator would find out on the
    day they restored it.
    """
    good = ready / "backups" / "firefly-2026-09-03.sql.gz"
    good.write_bytes(b"the good one")

    monkeypatch.setattr(backup.subprocess, "Popen", FakePgDump(b"", code=1, errors=b"boom\n"))
    with pytest.raises(backup.BackupFailed):
        backup.run(today=date(2026, 9, 3))

    assert good.read_bytes() == b"the good one"


def test_a_timeout_kills_the_dump_and_keeps_nothing(ready, monkeypatch):
    class Hanging(_Process):
        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("pg_dump", timeout or 0)

    monkeypatch.setattr(
        backup.subprocess, "Popen", lambda *a, **k: Hanging(_plausible_dump(), 0, b"")
    )
    with pytest.raises(backup.BackupFailed, match="timed out"):
        backup.run(today=date(2026, 9, 3))

    assert list((ready / "backups").iterdir()) == []


# --- what must never be in the archive ---------------------------------------


def test_the_credential_file_is_never_in_the_config_archive(ready, monkeypatch):
    """SPEC §16.9. A live TOTP secret must not ride in the same archive as the
    password hash it exists to be independent of."""
    (ready / "config" / "rules.yaml").write_text("categories: {}\n")
    (ready / "config" / "web-auth.json").write_text('{"totp_secret": "JBSWY3DPEHPK3PXP"}')
    monkeypatch.setattr(backup.subprocess, "Popen", FakePgDump(_plausible_dump()))

    result = backup.run(today=date(2026, 9, 3))
    assert result.config

    blob = (ready / "backups" / result.config).read_bytes()
    assert b"JBSWY3DPEHPK3PXP" not in blob
    with tarfile.open(ready / "backups" / result.config) as archive:
        assert not any(name.endswith(".json") for name in archive.getnames())


def test_it_never_claims_a_source_bundle_it_did_not_write(ready, monkeypatch):
    """`make backup` on the host also bundles the source. This cannot, and the
    field exists so no caller can round that off to "backed up"."""
    monkeypatch.setattr(backup.subprocess, "Popen", FakePgDump(_plausible_dump()))
    assert backup.run(today=date(2026, 9, 3)).source_bundle is False


def test_no_config_at_all_is_reported_as_none_rather_than_an_empty_archive(ready, monkeypatch):
    monkeypatch.delenv("APP_KEY", raising=False)
    monkeypatch.setattr(backup.subprocess, "Popen", FakePgDump(_plausible_dump()))
    result = backup.run(today=date(2026, 9, 3))

    assert result.config is None
    assert not list((ready / "backups").glob("config-*"))


def test_nothing_partial_is_left_behind_on_success(ready, monkeypatch):
    """A leftover `.partial` in backups/ is not harmless: `_dump_state` scans the
    directory to decide whether the purge gate opens."""
    (ready / "config" / "rules.yaml").write_text("categories: {}\n")
    monkeypatch.setattr(backup.subprocess, "Popen", FakePgDump(_plausible_dump()))
    backup.run(today=date(2026, 9, 3))

    assert not list((ready / "backups").glob(".*partial"))
