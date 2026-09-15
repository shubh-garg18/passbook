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


# --- reading a lot of rows without reading all of them ------------------------


def many(store, n: int = 40, account: str = "Bank savings"):
    from datetime import timedelta

    first = date(2026, 5, 9)
    for i in range(n):
        store.store_transaction(
            row(
                f"canara-1111-2026050900{i:04d}",
                txn_date=first + timedelta(days=i),
                amount=Decimal(f"{100 + i}.00"),
                category="Eating out" if i % 2 else "Shopping",
                description=f"PAYEE{i:03d} (UPI)",
                counterparty=f"PAYEE{i:03d}",
                tags=["food"] if i % 3 == 0 else [],
                kind="deposit" if i % 5 == 0 else "withdrawal",
            )
        )
    return store


def test_a_window_is_a_query_not_a_filter():
    """The window has to be served by the index. Filtering afterwards costs the
    same as asking for everything — measured, on a synthetic ten-year ledger,
    one month took as long as ten years."""
    store = many(ledger())
    got = store.account_transactions("Bank savings", date(2026, 5, 11), date(2026, 5, 13))
    assert [r["txn_date"] for r in got] == [
        date(2026, 5, 11), date(2026, 5, 12), date(2026, 5, 13)
    ]


def test_a_count_does_not_fetch_the_rows_to_count_them():
    store = many(ledger())
    assert store.count_transactions("Bank savings") == 40
    assert store.count_transactions("No such account") == 0


def test_search_pages_and_says_how_many_matched():
    """`matched` is the caption's denominator and a page is a hundred of them,
    so it cannot be the length of the page."""
    store = many(ledger())
    page, matched = store.search_transactions(["Bank savings"], limit=10)
    assert matched == 40
    assert len(page) == 10


def test_search_narrows_on_every_filter_the_page_offers():
    store = many(ledger())
    for kwargs, expected in (
        ({"kind": "deposit"}, 8),
        ({"category": "Shopping"}, 20),
        ({"tag": "food"}, 14),
        ({"minimum": Decimal("130.00")}, 10),
        ({"maximum": Decimal("109.00")}, 10),
        ({"query": "PAYEE007"}, 1),
    ):
        _, matched = store.search_transactions(["Bank savings"], **kwargs)
        assert matched == expected, kwargs


def test_search_finds_the_raw_narration_too():
    """Searching for a bank reference is exactly the case where the payee's
    name is no help, and it is why this page can replace a general search."""
    store = ledger()
    store.store_transaction(row(notes="UPI/DR/412345678901/SOMEONE/YESB"))
    _, matched = store.search_transactions(["Bank savings"], query="412345678901")
    assert matched == 1


def test_an_empty_category_is_searchable_by_the_name_the_analysis_gives_it():
    store = ledger()
    store.store_transaction(row("canara-1111-1", category=""))
    store.store_transaction(row("canara-1111-2", category="Shopping"))
    _, matched = store.search_transactions(["Bank savings"], category="(no category)")
    assert matched == 1


def test_search_orders_amounts_as_numbers_not_as_strings():
    """`"9.00"` sorts above `"10000.00"` lexically, which is the kind of wrong
    that looks fine."""
    store = ledger()
    store.store_transaction(row("canara-1111-1", amount=Decimal("9.00")))
    store.store_transaction(row("canara-1111-2", amount=Decimal("10000.00")))
    page, _ = store.search_transactions(["Bank savings"], order="amount")
    assert [r["amount"] for r in page] == [Decimal("10000.00"), Decimal("9.00")]


def test_every_row_comes_back_with_a_tags_list():
    """Empty where it has none. A caller must never have to tell "no tags" from
    "tags not loaded"."""
    store = many(ledger(), 5)
    page, _ = store.search_transactions(["Bank savings"])
    assert all(isinstance(r["tags"], list) for r in page)
    assert any(r["tags"] for r in page) and any(not r["tags"] for r in page)
