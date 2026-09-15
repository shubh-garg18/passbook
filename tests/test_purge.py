"""Removing an account's rows. DECISIONS.md §36.

Most of what this module used to test is gone with the store it tested against:
soft-delete tombstones that had to be force-deleted afterwards, a 401 that meant
either "already gone" or "your token died" and had to be told apart, and an
intent file written before the first delete so an interrupted run could be
finished later. A delete is a delete now, and it is one statement.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from passbook.purge import find_candidates, purge
from passbook.store import LedgerError
from passbook.store.memory import MemoryLedger

ASSET = "Canara Bank savings account"
BASE_TXN_ID = "20260509000001"


def row(external_id: str, **over) -> dict:
    split = {
        "external_id": external_id,
        "account": ASSET,
        "kind": "withdrawal",
        "txn_date": date(2026, 5, 9),
        "amount": Decimal("65.00"),
        "description": "ZOKVEX QI (UPI)",
        "counterparty": "ZOKVEX QI",
        "currency": "INR",
    }
    split.update(over)
    return split


def ledger(count: int = 3) -> MemoryLedger:
    store = MemoryLedger()
    store.store_account(ASSET, Decimal("12612.64"), date(2026, 5, 7), "INR")
    first = int(BASE_TXN_ID)
    for n in range(count):
        store.store_transaction(row(str(first + n)))
    return store


def test_every_stored_row_is_deletable_and_none_is_protected():
    """`external_id` is the primary key, so a row without one cannot exist.

    The protected list is still returned, and still shown, so that "nothing is
    protected" is something the operator reads rather than something the code
    assumes. The opening balance — the row this list existed to shield — is a
    column on the account now, not a row that could be deleted by accident.
    """
    candidates, protected = find_candidates(ledger(), ASSET)
    assert len(candidates) == 3
    assert protected == []


def test_an_account_with_no_rows_yields_nothing():
    store = MemoryLedger()
    store.store_account(ASSET, Decimal("0.00"), date(2026, 5, 7), "INR")
    assert find_candidates(store, ASSET) == ([], [])


def test_candidate_carries_enough_to_show_a_dry_run():
    """A dry run is the only thing standing between the operator and a delete,
    so it has to say what would go — not how many."""
    candidate = find_candidates(ledger(1), ASSET)[0][0]
    assert candidate.external_id == BASE_TXN_ID
    assert candidate.date == "2026-05-09"
    assert candidate.description == "ZOKVEX QI (UPI)"
    assert candidate.amount == Decimal("65.00")


def test_successful_deletes_are_counted_and_the_rows_are_gone():
    store = ledger()
    candidates, _ = find_candidates(store, ASSET)
    result = purge(store, candidates)
    assert (result.deleted, result.failed) == (3, 0)
    assert result.ok
    assert store.account_transactions(ASSET) == []


def test_a_failure_is_named_rather_than_swallowed():
    """The operator is about to be told how many rows went, and that number has
    to be one this function watched happen."""

    class Stubborn(MemoryLedger):
        def delete_transaction(self, external_id):
            raise LedgerError("connection reset")

    store = Stubborn()
    store.store_account(ASSET, Decimal("12612.64"), date(2026, 5, 7), "INR")
    store.store_transaction(row(BASE_TXN_ID))

    candidates, _ = find_candidates(store, ASSET)
    result = purge(store, candidates)
    assert (result.deleted, result.failed) == (0, 1)
    assert not result.ok
    assert result.failures == [(BASE_TXN_ID, "connection reset")]


def test_one_failure_does_not_stop_the_run():
    refuse = {str(int(BASE_TXN_ID) + 1)}

    class Partly(MemoryLedger):
        def delete_transaction(self, external_id):
            if external_id in refuse:
                raise LedgerError("nope")
            super().delete_transaction(external_id)

    store = Partly()
    store.store_account(ASSET, Decimal("12612.64"), date(2026, 5, 7), "INR")
    first = int(BASE_TXN_ID)
    for n in range(3):
        store.store_transaction(row(str(first + n)))

    candidates, _ = find_candidates(store, ASSET)
    result = purge(store, candidates)
    assert (result.deleted, result.failed) == (2, 1)
    assert [r["external_id"] for r in store.account_transactions(ASSET)] == sorted(refuse)


def test_progress_is_reported_as_it_goes():
    """A purge of a whole account is the longest thing the UI ever waits on."""
    seen: list[int] = []
    store = ledger()
    candidates, _ = find_candidates(store, ASSET)
    purge(store, candidates, on_progress=lambda r: seen.append(r.deleted))
    assert seen == [1, 2, 3]
