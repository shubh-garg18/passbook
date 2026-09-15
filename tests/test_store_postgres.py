"""The ledger against a real Postgres. DECISIONS.md §36.

**A deliberate, narrow exception to "tests use fixtures, never the network".**
Everything else in this suite is hermetic and stays that way — `MemoryLedger`
is held to every invariant the schema declares, and `test_store.py` is where
they are pinned.

What a double cannot prove is that the schema *says* what the double enforces.
A `CHECK` constraint with a typo in it, a `NUMERIC` that comes back as a float,
a `PRIMARY KEY` on the wrong column: all three pass every in-memory test and
lose money in production. So this runs the same invariants against the database
that will actually hold them.

It auto-skips when there is no database, so `make test` on a laptop with the
containers stopped stays green and honest rather than red and ignored.

    make up && uv run pytest tests/test_store_postgres.py -v

Nothing here touches the live ledger: it writes rows with ids no statement
produces, against its own probe account, and removes them.

`PostgresLedger` is constructed directly rather than through `open_ledger`,
because `conftest.py` replaces `open_ledger` with one that refuses — the guard
that keeps every *other* test hermetic. This is the one file that means to
reach a database, so it goes around the guard in the open.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

import pytest

from passbook.store import LedgerError

#: Ids no bank emits, so a stray row is recognisable and a real one is never hit.
PROBE = "probe-test-"
ACCOUNT = "Probe account (tests)"


def _dsn() -> str | None:
    """Where the ledger is, or None if it is not there.

    A connect, not a guess: the port can be moved, and a fresh clone has no
    `.env` at all.
    """
    from passbook.config import load_settings

    try:
        settings = load_settings()
    except Exception:  # noqa: BLE001 — no settings is no database
        return None
    return settings.ledger_dsn


@pytest.fixture
def store():
    dsn = _dsn()
    if dsn is None:
        pytest.skip("no settings; run `make env`")
    try:
        from passbook.store.postgres import PostgresLedger

        opened = PostgresLedger(dsn)
    except LedgerError as exc:
        pytest.skip(f"no ledger to test against ({exc}); run `make up`")

    opened.store_account(ACCOUNT, Decimal("12612.64"), date(2026, 5, 7), "INR")
    try:
        yield opened
    finally:
        for stored in opened.account_transactions(ACCOUNT):
            opened.delete_transaction(stored["external_id"])
        # The audit entries these tests wrote, and only those: the table is
        # append-only by design, so a test that leaves rows in it is a test
        # that pollutes a real operator's history.
        with opened._conn.cursor() as cur:
            cur.execute("DELETE FROM audit WHERE summary IN ('one', 'two')")
        opened.close()


def row(suffix: str = "1", **over) -> dict:
    split = {
        "external_id": PROBE + suffix,
        "account": ACCOUNT,
        "kind": "withdrawal",
        "txn_date": date(2026, 5, 9),
        "amount": Decimal("65.00"),
        "description": "ZOKVEX QI (UPI)",
        "counterparty": "ZOKVEX QI",
        "category": "Eating out",
        "notes": "UPI/DR/...",
        "txn_time": time(1, 51, 33),
        "currency": "INR",
        "tags": ["food"],
    }
    split.update(over)
    return split


def test_the_identity_is_the_primary_key(store):
    """Not a check that could be bypassed — the shape of the table.

    The second write differs in every field a content hash would look at, which
    is exactly the case that got seven rows into a real ledger twice.
    """
    store.store_transaction(row())
    with pytest.raises(LedgerError, match="could not store"):
        store.store_transaction(row(description="renamed since", category="Mess"))
    assert len(store.account_transactions(ACCOUNT)) == 1


def test_money_comes_back_as_a_decimal(store):
    """Non-negotiable 1 at the boundary a double cannot speak for: `NUMERIC`
    maps to `Decimal` in psycopg, and a driver that handed back floats would
    put the whole rule at the mercy of a dependency."""
    store.store_transaction(row())
    amount = store.account_transactions(ACCOUNT)[0]["amount"]
    assert isinstance(amount, Decimal)
    assert amount == Decimal("65.00")


def test_the_date_and_the_clock_survive_the_round_trip(store):
    store.store_transaction(row())
    stored = store.account_transactions(ACCOUNT)[0]
    assert stored["txn_date"] == date(2026, 5, 9)
    assert stored["txn_time"] == time(1, 51, 33)


def test_the_database_refuses_a_direction_it_does_not_have(store):
    with pytest.raises(LedgerError):
        store.store_transaction(row("2", kind="opening balance"))


def test_the_database_refuses_a_negative_amount(store):
    with pytest.raises(LedgerError):
        store.store_transaction(row("3", amount=Decimal("-1.00")))


def test_tags_are_replaced_rather_than_appended(store):
    store.store_transaction(row(tags=["food", "not-earnings"]))
    store.update_transaction(PROBE + "1", {"tags": ["food"]})
    assert store.account_transactions(ACCOUNT)[0]["tags"] == ["food"]


def test_an_update_may_not_touch_what_the_statement_owns(store):
    store.store_transaction(row())
    with pytest.raises(LedgerError, match="may not touch"):
        store.update_transaction(PROBE + "1", {"amount": Decimal("1.00")})
    assert store.account_transactions(ACCOUNT)[0]["amount"] == Decimal("65.00")


def test_the_balance_is_derived_from_the_rows(store):
    store.store_transaction(row())
    store.store_transaction(row("2", kind="deposit", amount=Decimal("500.00")))
    live = next(a for a in store.asset_accounts() if a["name"] == ACCOUNT)
    assert live["current_balance"] == Decimal("12612.64") - Decimal("65.00") + Decimal("500.00")


def test_applying_the_schema_again_changes_nothing(store):
    """`_migrate` runs on every connection. It has to be safe on a live
    database, because that is the only kind there will ever be after the first
    start."""
    from passbook.store.postgres import PostgresLedger

    store.store_transaction(row())
    second = PostgresLedger(_dsn())
    try:
        assert len(second.account_transactions(ACCOUNT)) == 1
    finally:
        second.close()


def test_a_ledger_that_is_not_there_gives_up_rather_than_hanging():
    """**Measured.** libpq's default is no connection timeout at all, and a TCP
    connect to a port nothing is listening on does not always come back
    refused — under WSL's mirrored networking it simply hangs.

    Unbounded, a ledger that is down hangs every page instead of erroring on
    one, and that is the worse failure by a wide margin: an error names the
    problem, a hang looks like slowness. This test does not need a database —
    it needs the absence of one.
    """
    import time

    from passbook.store.postgres import CONNECT_TIMEOUT, PostgresLedger

    start = time.monotonic()
    with pytest.raises(LedgerError, match="could not connect"):
        # 5999 rather than a random port: fixed, so a failure here is about the
        # timeout rather than about which port was drawn.
        PostgresLedger("postgresql://nobody:nothing@127.0.0.1:5999/nowhere")
    assert time.monotonic() - start < CONNECT_TIMEOUT * 3


# --- the reads a page actually makes ------------------------------------------


def test_the_window_is_served_by_the_index(store):
    """Not a Python filter over everything. The index is on
    `(account, txn_date)` precisely so that "this month" reads a month."""
    from datetime import timedelta

    for i in range(6):
        store.store_transaction(row(str(i), txn_date=date(2026, 5, 9) + timedelta(days=i)))
    got = store.account_transactions(ACCOUNT, date(2026, 5, 11), date(2026, 5, 12))
    assert [r["txn_date"] for r in got] == [date(2026, 5, 11), date(2026, 5, 12)]


def test_search_matches_the_same_things_the_double_does(store):
    """The double is held to this, not the other way round — a fake that
    matches more than the database makes a search test green and a search
    wrong."""
    store.store_transaction(row("a", description="ZOKVEX QI (UPI)", category="Eating out",
                                tags=["food"], amount=Decimal("65.00")))
    # A different counterparty as well as a different description: the default
    # row carries `ZOKVEX QI` in both, and leaving it made the first case match
    # twice — which is the search doing its job over the column the test forgot.
    store.store_transaction(row("b", description="Employer (NEFT)", counterparty="Employer",
                                category="Salary", kind="deposit", tags=[],
                                amount=Decimal("9000.00")))

    for kwargs, expected in (
        ({"query": "zokvex"}, 1),          # case-insensitive
        ({"query": "UPI/DR"}, 2),          # the raw narration
        ({"query": "salary"}, 1),          # the category
        ({"query": "food"}, 1),            # a tag, resolved before it joins
        ({"kind": "deposit"}, 1),
        ({"category": "Eating out"}, 1),
        ({"tag": "food"}, 1),
        ({"minimum": Decimal("100")}, 1),
        ({"maximum": Decimal("100")}, 1),
    ):
        _, matched = store.search_transactions([ACCOUNT], **kwargs)
        assert matched == expected, kwargs


def test_a_page_is_a_page_and_matched_is_the_whole_answer(store):
    for i in range(7):
        store.store_transaction(row(str(i)))
    page, matched = store.search_transactions([ACCOUNT], limit=3)
    assert matched == 7 and len(page) == 3
    second, _ = store.search_transactions([ACCOUNT], limit=3, offset=3)
    assert {r["external_id"] for r in page} & {r["external_id"] for r in second} == set()


def test_count_does_not_fetch(store):
    for i in range(4):
        store.store_transaction(row(str(i)))
    assert store.count_transactions(ACCOUNT) == 4


def test_the_audit_log_is_append_only_and_reads_newest_first(store):
    store.record_event("import", "one", {"a": 1}, 3)
    store.record_event("rename", "two", {}, None)
    entries = store.events()
    assert [e["summary"] for e in entries[:2]] == ["two", "one"]
    assert entries[1]["detail"] == {"a": 1}
    assert entries[1]["affected"] == 3
    assert [e["action"] for e in store.events(action="rename")] == ["rename"]
