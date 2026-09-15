"""Shared fixtures. Everything here reads tests/fixtures/, never inbox/.

The fixtures are produced by scripts/redact.py from a real statement, with the
metadata block, narrations and amounts rewritten but the balance chain
recomputed so the §6.6 invariant stays meaningful.
"""

import csv
from pathlib import Path


import pytest

# Anchored on this file, never on an absolute path. Four tests used to name one
# particular checkout, which made them pass on exactly one machine.
REPO_ROOT = Path(__file__).resolve().parent.parent

@pytest.fixture(autouse=True)
def _empty_caches():
    """A cache is shared state, and shared state between tests is a test that
    passes because of the one before it.

    Both of these live on DISK, so they outlive a process as well as a test —
    which is the point of them, and exactly why a test must not inherit one.
    Pointed at a tmpdir rather than emptied, so a run never touches the
    operator's own. Autouse, so nothing has to remember.
    """
    import tempfile

    from passbook import index as _ix
    from passbook import parsecache as _pc
    from passbook import yamlfile as _yf

    scratch = Path(tempfile.mkdtemp())
    memo_was, _pc.CACHE_DIR = _pc.CACHE_DIR, scratch / "statements"
    # The index is CWD-relative, and a sync DELETES rows whose statement is not
    # on disk — so without this each test would quietly demolish the previous
    # one's index.
    index_was, _ix.INDEX_PATH = _ix.INDEX_PATH, scratch / "index.sqlite3"
    _yf.forget_everything()
    yield
    _pc.CACHE_DIR = memo_was
    _ix.INDEX_PATH = index_was
    _yf.forget_everything()

FIXTURES = Path(__file__).parent / "fixtures"
XLS_FIXTURE = FIXTURES / "statement.xls"
# A second account whose transaction ids collide with the first's completely, and
# whose last four digits collide too. SPEC §21.1 — the bank sequences ids per
# account, so this is the real shape of a second Canara account, not a contrivance.
SECOND_FIXTURE = FIXTURES / "statement-second.xls"
SECOND_ACCOUNT = "888800001111"
CSV_FIXTURE = FIXTURES / "statement.csv"
HTML_FIXTURE = FIXTURES / "statement.html"

# Matches scripts/redact.py's synthetic metadata block.
FIXTURE_ACCOUNT = "999900001111"
FIXTURE_SHEET_NAME = "Account Number 999900001111"
FIXTURE_TXN_COUNT = 93


@pytest.fixture
def rows() -> list[list[str]]:
    with CSV_FIXTURE.open(newline="", encoding="utf-8") as fh:
        return [list(r) for r in csv.reader(fh)]


@pytest.fixture
def parsed():
    from passbook.loaders import load

    return load(CSV_FIXTURE)


@pytest.fixture
def enriched(parsed):
    """Transactions with narration fields applied, as the CLI does."""
    from passbook import narration

    meta, transactions = parsed
    narration.enrich(transactions)
    return meta, transactions


class StoreDouble:
    """Base for the stand-ins that replace the ledger in tests.

    It used to exist for `fresh()` — the real client memoised, and a check that
    read the memo had not read the ledger. There is no memo now; a query is a
    query. What is left is the `with` block every call site uses, so a double
    does not have to remember to grow one.

    A double is held to the real thing's shape deliberately: `MemoryLedger` in
    `passbook.store.memory` enforces every invariant the schema does, and is
    the right base for anything that needs to behave like a ledger rather than
    merely answer one question.
    """

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_real_ledger(monkeypatch):
    """Tests never open a real database. Enforced, not remembered.

    Every route reaches the ledger through `_base.open_ledger`, so a test that
    forgets to supply a fake used to fall through to a genuine TCP connect —
    which does not fail fast. It waits out the driver's connection timeout, and
    a suite of those turns minutes into an afternoon while looking like nothing
    more than slowness.

    So the default is a store that refuses immediately, with a message naming
    the fixture the test should have used. A test that wants a working ledger
    overrides this by patching the same name, which is what every one of them
    already does.
    """
    from passbook.store import LedgerError

    def refuse(*_args, **_kwargs):
        raise LedgerError(
            "no ledger in tests — patch `passbook.web.api._base.open_ledger`, "
            "or use `passbook.store.memory.MemoryLedger`"
        )

    monkeypatch.setattr("passbook.web.api._base.open_ledger", refuse)
    monkeypatch.setattr("passbook.store.open_ledger", refuse)
