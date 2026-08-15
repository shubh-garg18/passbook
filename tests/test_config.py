"""Alias loading and the payees.md drift check.

The yaml is the source of truth: it is what the code reads, so a typo in a
hand-maintained markdown file must never change ledger behaviour. The drift
check reports disagreement and does nothing else.
"""

from passbook.config import alias_drift, load_payee_aliases, parse_payees_markdown

TABLE = """# payee tokens (3 distinct)

| # | Token | Len | Alias | Chan | Txns |
|---|---|---|---|---|---|
| 1 | ZEPKV JYX | 9 | Groceries | UPI | 5 |
| 2 | JYXQI | 5 | Transport | UPI | 5 |
| 3 | GRUGR | 5 |  | UPI | 1 |
"""


def write(tmp_path, md=TABLE, yaml_text=None):
    md_path = tmp_path / "payees.md"
    md_path.write_text(md)
    yaml_path = tmp_path / "aliases.yaml"
    if yaml_text is not None:
        yaml_path.write_text(yaml_text)
    return yaml_path, md_path


def test_parses_token_and_alias_columns(tmp_path):
    _, md = write(tmp_path)
    assert parse_payees_markdown(md) == {
        "ZEPKV JYX": "Groceries",
        "JYXQI": "Transport",
        "GRUGR": "",
    }


def test_columns_are_found_by_header_not_position(tmp_path):
    """payees.md is hand-edited; columns get added and removed."""
    md = (
        "| Alias | Token | Notes |\n|---|---|---|\n"
        "| Groceries | ZEPKV JYX | something |\n"
    )
    _, path = write(tmp_path, md=md)
    assert parse_payees_markdown(path) == {"ZEPKV JYX": "Groceries"}


def test_missing_file_is_not_an_error(tmp_path):
    assert parse_payees_markdown(tmp_path / "nope.md") == {}
    assert alias_drift(tmp_path / "nope.yaml", tmp_path / "nope.md") == []


def test_no_drift_when_they_agree(tmp_path):
    yaml_path, md = write(
        tmp_path,
        yaml_text="aliases:\n  ZEPKV JYX: Groceries\n  JYXQI: Transport\n",
    )
    assert alias_drift(yaml_path, md) == []


def test_reports_a_differing_alias(tmp_path):
    yaml_path, md = write(
        tmp_path,
        yaml_text="aliases:\n  ZEPKV JYX: Grocery\n  JYXQI: Transport\n",
    )
    drift = alias_drift(yaml_path, md)
    assert len(drift) == 1
    assert "ZEPKV JYX" in drift[0] and "Groceries" in drift[0] and "Grocery" in drift[0]


def test_reports_an_alias_only_in_markdown(tmp_path):
    yaml_path, md = write(tmp_path, yaml_text="aliases:\n  JYXQI: Transport\n")
    drift = alias_drift(yaml_path, md)
    assert any("ZEPKV JYX" in d and "no alias" in d for d in drift)


def test_yaml_ahead_of_markdown_is_not_drift(tmp_path):
    """payees.md's Alias column is generated, so the yaml being ahead is
    ordinary staleness — the next `make payees` fixes it and nothing is at
    risk. Reporting it fired on every normal UI edit, which trains the warning
    to be ignored."""
    yaml_path, md = write(
        tmp_path,
        yaml_text=(
            "aliases:\n  ZEPKV JYX: Groceries\n  JYXQI: Transport\n"
            "  ZZZ: Ghost\n  GRUGR: Someone\n"
        ),
    )
    assert alias_drift(yaml_path, md) == []


def test_a_hand_edit_the_yaml_lacks_is_still_reported(tmp_path):
    """The direction that still matters: payees.md claims an alias the yaml
    does not have, so regenerating would silently discard it."""
    md_text = TABLE.replace("| 3 | GRUGR | 5 |  | UPI | 1 |", "| 3 | GRUGR | 5 | Invented | UPI | 1 |")
    yaml_path, md = write(
        tmp_path, md=md_text,
        yaml_text="aliases:\n  ZEPKV JYX: Groceries\n  JYXQI: Transport\n",
    )
    drift = alias_drift(yaml_path, md)
    assert any("GRUGR" in d and "discard" in d for d in drift)


def test_drift_check_never_mutates_either_file(tmp_path):
    """Detect, don't auto-sync."""
    yaml_text = "aliases:\n  ZEPKV JYX: Wrong Name\n"
    yaml_path, md = write(tmp_path, yaml_text=yaml_text)
    before_md, before_yaml = md.read_text(), yaml_path.read_text()
    alias_drift(yaml_path, md)
    assert md.read_text() == before_md
    assert yaml_path.read_text() == before_yaml
    # and the yaml still wins for anything that actually reads aliases
    assert load_payee_aliases(yaml_path)["ZEPKV JYX"] == "Wrong Name"


# --- sync staleness. SPEC D7: no cron, so this is the only nudge -------------

import os
import time

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
    name, age = last_sync(tmp_path / "archive")
    assert name == "new.xls"
    assert age == 0


def test_age_is_computed_from_the_newest_file(tmp_path):
    a = tmp_path / "archive"
    a.mkdir()
    f = a / "stale.xls"
    f.write_text("x")
    long_ago = time.time() - 21 * 86400
    os.utime(f, (long_ago, long_ago))
    name, age = last_sync(a)
    assert (name, age) == ("stale.xls", 21)
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
        monkeypatch.setattr("passbook.service.last_sync", lambda: ("Acnt_stmt__x.xls", days))
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

    monkeypatch.setattr("passbook.service.last_sync", lambda: None)
    buf = Console(record=True, width=100)
    assert cli.sync_staleness(buf) is None
    assert "no statement has been pushed" in buf.export_text()
