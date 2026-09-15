"""passbook's own ledger, and the invariants it enforces. DECISIONS.md §36.

Every test here is a thing that went wrong when the ledger was somebody else's
application reached over HTTP. The schema now refuses each one, and this is the
in-memory implementation held to the same constraints — because a double that
accepts what the database would refuse makes tests pass and production fail.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from passbook.store import LedgerError
from passbook.store.memory import MemoryLedger


def ledger() -> MemoryLedger:
    store = MemoryLedger()
    store.store_account("Bank savings", Decimal("10000.00"), date(2026, 5, 7), "INR")
    return store


def row(external_id: str = "canara-1111-20260509000001", **over) -> dict:
    split = {
        "external_id": external_id,
        "account": "Bank savings",
        "kind": "withdrawal",
        "txn_date": date(2026, 5, 9),
        "amount": Decimal("1418.91"),
        "description": "ZEPKV JYX (UPI)",
        "counterparty": "ZEPKV JYX (UPI)",
        "notes": "UPI/DR/...",
        "currency": "INR",
    }
    split.update(over)
    return split


# --- identity ----------------------------------------------------------------


def test_the_same_identity_cannot_be_stored_twice():
    """The whole reason the ledger moved.

    The previous store decided duplicates on a hash of the submitted payload,
    and passbook rewrites that payload for a living — an alias, a tag, a
    rename. Seven rows once posted a second time behind 133 identities with
    nothing raised anywhere. Here it is the primary key.
    """
    store = ledger()
    store.store_transaction(row())
    with pytest.raises(LedgerError, match="already in the ledger"):
        store.store_transaction(row(description="renamed since"))

    assert len(store.account_transactions("Bank savings")) == 1


def test_a_row_with_no_identity_is_refused():
    """Those are exactly the rows no check could ever reconcile."""
    store = ledger()
    with pytest.raises(LedgerError, match="no external_id"):
        store.store_transaction(row(external_id=""))


def test_a_transaction_needs_an_account_that_exists():
    store = MemoryLedger()
    with pytest.raises(LedgerError, match="no asset account"):
        store.store_transaction(row())


# --- money -------------------------------------------------------------------


def test_the_amount_stays_a_decimal():
    """Non-negotiable 1 does not stop at the storage boundary."""
    store = ledger()
    store.store_transaction(row(amount="1418.91"))
    stored = store.account_transactions("Bank savings")[0]["amount"]
    assert isinstance(stored, Decimal)
    assert stored == Decimal("1418.91")


def test_direction_lives_in_kind_not_in_the_sign():
    store = ledger()
    with pytest.raises(LedgerError, match="direction belongs in"):
        store.store_transaction(row(amount=Decimal("-1418.91")))


# --- updates -----------------------------------------------------------------


def test_an_update_may_not_touch_what_the_statement_owns():
    """The four fields config owns, and nothing else.

    Refused outright rather than merely ignored: an update that *could* move
    money is one that eventually does, and the previous store's update was
    sparse by accident rather than by rule.
    """
    store = ledger()
    store.store_transaction(row())
    for field, value in (
        ("amount", Decimal("1")),
        ("txn_date", date(2026, 1, 1)),
        ("kind", "deposit"),
        ("notes", "rewritten"),
    ):
        with pytest.raises(LedgerError, match="may not touch"):
            store.update_transaction(row()["external_id"], {field: value})

    unchanged = store.account_transactions("Bank savings")[0]
    assert unchanged["amount"] == Decimal("1418.91")
    assert unchanged["notes"] == "UPI/DR/..."


def test_an_update_writes_the_fields_config_does_own():
    store = ledger()
    store.store_transaction(row())
    store.update_transaction(
        row()["external_id"],
        {"description": "Mother (UPI)", "category": "Family", "tags": ["family"]},
    )
    stored = store.account_transactions("Bank savings")[0]
    assert stored["description"] == "Mother (UPI)"
    assert stored["category"] == "Family"
    assert stored["tags"] == ["family"]


def test_tags_replace_rather_than_append():
    """An omitted tag is a deleted tag, which is what lets a stale
    `not-earnings` be cleared at all."""
    store = ledger()
    store.store_transaction(row(tags=["not-earnings", "reversal"]))
    store.update_transaction(row()["external_id"], {"tags": ["reversal"]})
    assert store.account_transactions("Bank savings")[0]["tags"] == ["reversal"]


# --- reading -----------------------------------------------------------------


def test_identities_are_what_a_push_asks_before_posting():
    store = ledger()
    store.store_transaction(row("canara-1111-20260509000001"))
    store.store_transaction(row("canara-1111-20260509000002"))
    assert store.identities("Bank savings") == {
        "canara-1111-20260509000001",
        "canara-1111-20260509000002",
    }
    assert store.identities("Another account") == set()


def test_rows_come_back_in_statement_order():
    store = ledger()
    store.store_transaction(row("canara-1111-20260510000001", txn_date=date(2026, 5, 10)))
    store.store_transaction(row("canara-1111-20260509000001", txn_date=date(2026, 5, 9)))
    dates = [r["txn_date"] for r in store.account_transactions("Bank savings")]
    assert dates == sorted(dates)


def test_both_implementations_answer_the_same_questions():
    """A double that is missing a method is a double that passes every test and
    fails the first real request."""
    from passbook.store import LedgerStore
    from passbook.store.postgres import PostgresLedger

    wanted = {n for n in dir(LedgerStore) if not n.startswith("_")}
    for implementation in (MemoryLedger, PostgresLedger):
        missing = sorted(n for n in wanted if not callable(getattr(implementation, n, None)))
        assert not missing, f"{implementation.__name__} is missing {missing}"


def test_the_balance_is_the_rows_rather_than_a_stored_number():
    """A stored balance is a second copy of the truth, and the ledger already
    knows what a second copy of the truth costs."""
    store = ledger()
    store.store_transaction(row("canara-1111-20260509000001", amount=Decimal("1418.91")))
    store.store_transaction(
        row("canara-1111-20260509000002", kind="deposit", amount=Decimal("500.00"))
    )
    live = store.asset_accounts()[0]["current_balance"]
    assert live == Decimal("10000.00") - Decimal("1418.91") + Decimal("500.00")
    assert isinstance(live, Decimal)


def test_there_is_no_third_direction():
    """The schema's `CHECK (kind IN (...))`, enforced here too.

    The previous store modelled an opening balance as a transaction, and it
    turned up in listings as a row with no payee and no amount — counting one
    more than the archive held, which reads as a phantom transaction. It is a
    column on the account here, and this is what makes that unwritable.
    """
    store = ledger()
    with pytest.raises(LedgerError, match="not a direction"):
        store.store_transaction(row(kind="opening balance"))
