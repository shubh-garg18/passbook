"""What happened to the ledger, and the promise that it is only a record.

The gap this closes is config, not money. Every statement is in `archive/` and
`verify-ledger` compares the ledger against it, so the rows have a paper trail
already. Renaming a payee, moving a category, deleting one — those change what
the ledger *says*, and `config/` is gitignored because it names real people, so
there was no history of them anywhere.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from passbook import audit
from passbook.store.memory import MemoryLedger


@pytest.fixture
def store():
    ledger = MemoryLedger()
    ledger.store_account("Bank savings", Decimal("10000.00"), date(2026, 5, 7), "INR")
    return ledger


def test_an_entry_is_kept_and_read_back_newest_first(store):
    audit.record(store, "import", "first")
    audit.record(store, "rename", "second")
    assert [e["summary"] for e in audit.history(store)] == ["second", "first"]


def test_entries_can_be_narrowed_to_one_kind(store):
    audit.record(store, "import", "a statement")
    audit.record(store, "rename", "a payee")
    assert [e["action"] for e in audit.history(store, action="rename")] == ["rename"]


def test_recording_never_fails_the_thing_it_records():
    """A backup that worked and an audit row that did not is a backup that
    worked. This is the only place that swallowing is allowed, and it is
    deliberate — every caller is a real action already finished."""

    class Broken(MemoryLedger):
        def record_event(self, *a, **k):
            raise RuntimeError("the log is on fire")

    audit.record(Broken(), "backup", "took a dump")  # must not raise


def test_reading_never_fails_the_page():
    class Broken(MemoryLedger):
        def events(self, *a, **k):
            raise RuntimeError("the log is on fire")

    assert audit.history(Broken()) == []


def test_no_ledger_means_no_entry_rather_than_a_new_connection():
    """A code path with no store open writes nothing. It must never open one
    just to log — that would put a database round trip on a path that had
    deliberately avoided it."""
    audit.record(None, "backup", "took a dump")
    assert audit.history(None) == []


def test_an_unknown_action_is_logged_but_never_raised(store, caplog):
    """A typo is a bug in this repository, not a reason to fail an import."""
    audit.record(store, "not-a-real-action", "something")
    assert "unknown audit action" in caplog.text
    assert audit.history(store)[0]["summary"] == "something"


def test_importing_a_statement_is_recorded(tmp_path, monkeypatch):
    """The end-to-end shape: a real import, a real entry."""
    import shutil

    from conftest import FIXTURE_ACCOUNT, XLS_FIXTURE
    from passbook import service
    from passbook.config import Account, Settings

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    staged = tmp_path / "statement.xls"
    shutil.copy(XLS_FIXTURE, staged)

    ledger = MemoryLedger()
    ledger.store_account("Bank savings", Decimal("10000.00"), date(2026, 5, 7), "INR")
    account = Account(
        slug="canara-1111", bank="canara",
        account_number=FIXTURE_ACCOUNT, asset_account="Bank savings",
    )
    parsed = service.parse_statement(staged)
    service.push_statement(
        parsed,
        Settings(passbook_account_number=FIXTURE_ACCOUNT,
                 passbook_asset_account="Bank savings"),
        ledger,
        account=account,
    )

    entry, = audit.history(ledger)
    assert entry["action"] == "import"
    assert "statement.xls" in entry["summary"]
    assert entry["affected"] == len(parsed.transactions)
    # The period, never the rows: a record of what happened, not a copy of it.
    assert "period" in entry["detail"]
    assert "narration" not in str(entry["detail"])


def test_a_second_import_of_the_same_file_records_nothing(tmp_path, monkeypatch):
    """Weekly downloads overlap by design, so re-importing is the normal case.
    An entry per no-op would bury the entries that mean something."""
    import shutil

    from conftest import FIXTURE_ACCOUNT, XLS_FIXTURE
    from passbook import service
    from passbook.config import Account, Settings

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    staged = tmp_path / "statement.xls"
    shutil.copy(XLS_FIXTURE, staged)

    ledger = MemoryLedger()
    ledger.store_account("Bank savings", Decimal("10000.00"), date(2026, 5, 7), "INR")
    account = Account(
        slug="canara-1111", bank="canara",
        account_number=FIXTURE_ACCOUNT, asset_account="Bank savings",
    )
    settings = Settings(passbook_account_number=FIXTURE_ACCOUNT,
                        passbook_asset_account="Bank savings")
    for _ in range(2):
        service.push_statement(service.parse_statement(staged), settings, ledger,
                               account=account)

    assert len(audit.history(ledger)) == 1
