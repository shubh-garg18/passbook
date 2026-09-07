"""Sync staleness, out of `archive/`.

The payees.md half of this file went with the file itself (§117): aliases live
in `config/payee_aliases.yaml` and nowhere else, so there is no second copy to
drift from.
"""

from datetime import date, datetime, timedelta

import pytest

from passbook.config import SYNC_STALE_DAYS, SYNC_URGENT_DAYS, last_sync


# --- sync staleness. SPEC D7: no cron, so this is the only nudge -------------

import os
import time
from datetime import date, timedelta

from passbook.config import SYNC_STALE_DAYS, SYNC_URGENT_DAYS, last_sync


def test_no_archive_directory_means_never_synced(tmp_path):
    assert last_sync(tmp_path / "nope") is None


def test_empty_archive_means_never_synced(tmp_path):
    (tmp_path / "archive").mkdir()
    assert last_sync(tmp_path / "archive") is None


def test_reports_the_newest_file_and_its_age(tmp_path):
    a = tmp_path / "archive" / "2026-08"
    a.mkdir(parents=True)
    old, new = a / "old.xls", a / "new.xls"
    old.write_text("x"); new.write_text("x")
    long_ago = time.time() - 40 * 86400
    os.utime(old, (long_ago, long_ago))
    name, age, when = last_sync(tmp_path / "archive")
    assert name == "new.xls"
    assert age == 0
    assert when == date.today()


def test_age_is_computed_from_the_newest_file(tmp_path):
    a = tmp_path / "archive"
    a.mkdir()
    f = a / "stale.xls"
    f.write_text("x")
    long_ago = time.time() - 21 * 86400
    os.utime(f, (long_ago, long_ago))
    name, age, when = last_sync(a)
    assert (name, age) == ("stale.xls", 21)
    assert when == date.today() - timedelta(days=21), "the stamp and the age must agree"
    assert age > SYNC_STALE_DAYS  # would warn


def test_dotfiles_are_ignored(tmp_path):
    a = tmp_path / "archive"
    a.mkdir()
    (a / ".DS_Store").write_text("x")
    assert last_sync(a) is None


def _archive_aged(tmp_path, days):
    a = tmp_path / "archive"
    a.mkdir(exist_ok=True)
    f = a / "Acnt_stmt__x.xls"
    f.write_text("x")
    when = time.time() - days * 86400
    os.utime(f, (when, when))
    return a


def test_thresholds_are_ordered():
    """A single threshold cannot express 'late' and 'losing data' separately."""
    assert SYNC_STALE_DAYS < SYNC_URGENT_DAYS


def test_escalation_tiers(tmp_path, monkeypatch):
    """Fresh -> warn -> STALE, at the documented boundaries."""
    from rich.console import Console

    from passbook import cli

    def render(days):
        # Patch where the data now comes from: cli.sync_staleness renders
        # service.sync_status(), which is the single source of the tiers.
        monkeypatch.setattr(
            "passbook.service.last_sync",
            lambda *a, **k: ("Acnt_stmt__x.xls", days, date.today() - timedelta(days=days)),
        )
        buf = Console(record=True, width=100)
        age = cli.sync_staleness(buf)
        return age, buf.export_text()

    age, text = render(SYNC_STALE_DAYS)          # on the boundary: still fine
    assert age == SYNC_STALE_DAYS
    assert "ok" in text and "STALE" not in text

    age, text = render(SYNC_STALE_DAYS + 1)      # late
    assert "warn" in text
    assert "data" in text and "loss" in text     # says why it matters
    assert "STALE" not in text

    age, text = render(SYNC_URGENT_DAYS)         # boundary: still the soft tier
    assert "warn" in text and "STALE" not in text

    age, text = render(SYNC_URGENT_DAYS + 1)     # escalated
    assert "STALE" in text
    assert "Download today" in text
    assert "restore" in text                     # names what cannot save you


def test_never_synced_is_reported(tmp_path, monkeypatch):
    from rich.console import Console

    from passbook import cli

    monkeypatch.setattr("passbook.service.last_sync", lambda *a, **k: None)
    buf = Console(record=True, width=100)
    assert cli.sync_staleness(buf) is None
    assert "no statement has been pushed" in buf.export_text()


def test_a_malformed_attribution_file_is_ignored_not_fatal(tmp_path):
    """§73. `load_attribution` runs per request, so an AttributeError in it took
    the whole analysis page down with a 500 rather than being a config error.
    A hand-edited file can be a list, a string, or empty."""
    from passbook.config import load_attribution

    for junk in ("- a\n- b\n", "just a string\n", "", "null\n"):
        path = tmp_path / "attribution.yaml"
        path.write_text(junk)
        assert load_attribution(path).categories == frozenset()


def test_attribution_days_are_clamped_into_a_month_that_exists(tmp_path):
    """`to_day: 0` raised `ValueError: day is out of range` from
    `date.replace`; 29-31 would silently skip February, which is why
    `reminders.Schedule` caps day-of-month at 28 for the same reason."""
    from passbook.config import load_attribution

    path = tmp_path / "attribution.yaml"
    path.write_text("categories: [Credit Card]\nbefore_day: 0\nto_day: 99\n")
    loaded = load_attribution(path)
    assert loaded.before_day == 1
    assert loaded.to_day == 28

    path.write_text("categories: [Credit Card]\nbefore_day: nonsense\n")
    assert load_attribution(path).before_day == 10
