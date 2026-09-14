"""The archive index. SPEC §114.

`archived_statements` stopped re-parsing in §110 and still loaded **every**
statement to answer a question about one window. Measured on synthetic growth
from the real fixture:

     statements    rows   load-all   index query
              4     372       33ms         3.2ms
            100    9300      741ms        20.4ms
            800   74400     6209ms        54.6ms

The tests that matter are not the speed ones. They are: does it return exactly
what reading the files returns, and does it stop claiming rows the archive no
longer has.
"""

from __future__ import annotations

import shutil
from datetime import date

import pytest

from conftest import SECOND_FIXTURE, XLS_FIXTURE
from passbook import index, service
from passbook.config import Account


@pytest.fixture
def archive(tmp_path):
    out = tmp_path / "archive"
    out.mkdir()
    shutil.copy(XLS_FIXTURE, out / "one.xls")
    return out


@pytest.fixture
def conn(tmp_path):
    connection = index.connect(tmp_path / "index.sqlite3")
    yield connection
    connection.close()


def _account(number: str, slug: str = "canara-1111") -> Account:
    return Account(slug=slug, bank="canara", account_number=number,
                   asset_account="A", label="A")


def _number(archive):
    return service.archived_statements(archive)[0].meta.account_number.strip()


def test_it_returns_what_reading_the_files_returns(archive, conn):
    """The claim the whole thing rests on. Asserted against the file path rather
    than against hardcoded rows, so it cannot drift as the loaders change."""
    index.sync(archive, conn, service.parse_statement)
    number = _number(archive)
    account = _account(number)

    statements = service.archived_statements(archive)
    want = service.dedupe_transactions(service.statements_for(account, statements))
    got = index.transactions(conn, [number])

    assert sorted(t.txn_id for t in got) == sorted(t.txn_id for t in want)
    by_id = {t.txn_id: t for t in got}
    for txn in want:
        assert by_id[txn.txn_id].balance == txn.balance
        assert by_id[txn.txn_id].narration == txn.narration
        assert by_id[txn.txn_id].debit == txn.debit
        assert by_id[txn.txn_id].credit == txn.credit


def test_overlapping_statements_dedupe_the_way_the_files_do(archive, conn):
    """Statements overlap by design — a weekly download re-covers earlier weeks
    — so the same `txn_id` is in several files and the rule decides which copy
    is read. `dedupe_transactions` takes the FIRST in sorted-path order, and the
    index has to reproduce that and not merely produce *a* row."""
    shutil.copy(XLS_FIXTURE, archive / "two.xls")
    index.sync(archive, conn, service.parse_statement)
    number = _number(archive)

    statements = service.archived_statements(archive)
    want = service.dedupe_transactions(service.statements_for(_account(number), statements))
    got = index.transactions(conn, [number])
    assert len(got) == len(want), "a duplicate survived, or a real row was dropped"


def test_two_accounts_are_never_deduped_together(archive, conn):
    """Non-negotiable 10. The bank sequences `txn_id` per account, so two
    accounts emit identical ids — 93 of 93 on these fixtures. Partitioning on
    the id alone would keep half the rows and report no error at all."""
    shutil.copy(SECOND_FIXTURE, archive / "second.xls")
    index.sync(archive, conn, service.parse_statement)

    numbers = sorted({s.meta.account_number.strip()
                      for s in service.archived_statements(archive)})
    assert len(numbers) == 2, "the fixtures no longer describe two accounts"
    both = index.transactions(conn, numbers)
    first = index.transactions(conn, numbers[:1])
    second = index.transactions(conn, numbers[1:])
    assert len(both) == len(first) + len(second) > len(first)


def test_a_window_returns_only_that_window(archive, conn):
    index.sync(archive, conn, service.parse_statement)
    number = _number(archive)
    everything = index.transactions(conn, [number])
    june = index.transactions(conn, [number], date(2026, 6, 1), date(2026, 6, 30))
    assert 0 < len(june) < len(everything)
    assert all(t.txn_date.month == 6 for t in june)


def test_a_statement_that_leaves_the_archive_takes_its_rows(archive, conn):
    """The index is a view of `archive/`, which is what `verify-ledger` compares
    the ledger against (§20). A row it kept after the file was gone would be a
    third place for the two to disagree."""
    index.sync(archive, conn, service.parse_statement)
    number = _number(archive)
    assert index.transactions(conn, [number])

    (archive / "one.xls").unlink()
    index.sync(archive, conn, service.parse_statement)
    assert index.transactions(conn, [number]) == []


def test_a_second_sync_adds_nothing_and_parses_nothing(archive, conn):
    """It hashes; it does not parse. That is the whole reason it is faster than
    reading the files, so it is asserted rather than assumed."""
    index.sync(archive, conn, service.parse_statement)

    def explode(_path, **_kwargs):
        raise AssertionError("a warm sync re-parsed a statement")

    assert index.sync(archive, conn, explode) == 0


def test_an_unreadable_statement_is_skipped_not_fatal(archive, conn):
    """The same rule `archived_statements` has always had: one bad file must not
    blank a page."""
    (archive / "junk.xls").write_bytes(b"not a statement")
    index.sync(archive, conn, service.parse_statement)
    assert index.transactions(conn, [_number(archive)])


def test_money_survives_the_round_trip(archive, conn):
    """Non-negotiable 1. The row is stored as its own JSON precisely so that no
    column type can turn a Decimal into a float on the way through."""
    from decimal import Decimal

    index.sync(archive, conn, service.parse_statement)
    for txn in index.transactions(conn, [_number(archive)]):
        assert txn.debit is None or isinstance(txn.debit, Decimal)
        assert isinstance(txn.balance, Decimal)


# --- the parse memos the index leaves behind. SPEC §117.1 --------------------


def test_memos_for_vanished_statements_are_pruned(archive, conn, tmp_path, monkeypatch):
    """One memo is written per file parsed, including uploads that were
    rejected and re-uploaded, and nothing removed them — a slow certainty.

    The archive's hashes are what a memo is still worth keeping for, and the
    index sync has already computed them.
    """
    from passbook import parsecache

    monkeypatch.setattr(parsecache, "SPARE", 2)
    parsecache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(6):
        (parsecache.CACHE_DIR / f"1-deadbeef{i:024x}.json").write_text("{}", encoding="utf-8")
    assert len(list(parsecache.CACHE_DIR.glob("*.json"))) == 6

    index.sync(archive, conn, service.parse_statement)
    left = sorted(p.stem for p in parsecache.CACHE_DIR.glob("*.json"))
    # The archived statement's own memo, plus SPARE of the strays.
    assert len(left) == 3, left


def test_a_staged_statement_keeps_its_memo(archive, conn, tmp_path):
    """A staged file is not in the archive — it has not been pushed — so a
    prune keyed on the archive alone would drop the memo for the one file the
    operator is actively waiting on, between preview and push."""
    from passbook import parsecache

    staged = tmp_path / "staged.xls"
    shutil.copy(XLS_FIXTURE, staged)
    (staged).write_bytes(bytearray(staged.read_bytes())[:-1] + b"\x01")
    try:
        service.parse_statement(staged)
    except Exception:
        pass
    before = {p.stem for p in parsecache.CACHE_DIR.glob("*.json")}

    index.sync(archive, conn, service.parse_statement)
    after = {p.stem for p in parsecache.CACHE_DIR.glob("*.json")}
    assert before <= after, "a staged statement's memo was pruned"


def test_pruning_never_raises_when_the_cache_is_gone(archive, conn):
    """Failing to prune costs disk; raising here would cost a page."""
    from passbook import parsecache

    shutil.rmtree(parsecache.CACHE_DIR, ignore_errors=True)
    assert parsecache.prune(set()) == 0

