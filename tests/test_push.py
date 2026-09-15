"""Row construction and push semantics. DECISIONS.md §36.

The ledger here is `MemoryLedger`, which enforces every invariant the schema
does. Nothing touches the network — there is nothing left to reach.
"""

from datetime import date, time
from decimal import Decimal

import pytest

from passbook.models import UPI, Transaction
from passbook.push import build_split, push_transactions
from passbook.store import LedgerError
from passbook.store.memory import MemoryLedger

ASSET = "Canara Bank savings account"

#: The fixture statement's first transaction id.
BASE_TXN_ID = "20260509000001"

RULES = {
    "rules": [
        {"title": "Eating out", "category": "Eating out", "tag": "food", "payees": ["ZOKVEX QI"]},
    ],
    "not_earnings": {"tag": "not-earnings", "earnings_only": ["Employer"]},
    "large_oneoff": {"tag": "large-oneoff", "exclude_categories": ["Investments"]},
}


def ids(count: int) -> list[str]:
    """`count` consecutive ids, derived from the fixture's rather than spelled.

    Written out, a 14-digit literal reads as an account number or a UTR to the
    privacy audit — correctly, since it cannot tell the difference. Deriving
    them keeps the only long digit run in this file the fixture's own.
    """
    first = int(BASE_TXN_ID)
    return [str(first + n) for n in range(count)]


def txn(**kw) -> Transaction:
    base = dict(
        txn_id=BASE_TXN_ID,
        txn_date=date(2026, 5, 9),
        narration="UPI/DR/412345678901/ZOKVEX QI/YESB/**12345@YBL/UPI//X/09/05/2026 01:51:33",
        debit=Decimal("65.00"),
        credit=None,
        balance=Decimal("12547.64"),
        channel=UPI,
        payee="ZOKVEX QI",
    )
    base.update(kw)
    return Transaction(**base)


def ledger(*, account: str = ASSET) -> MemoryLedger:
    store = MemoryLedger()
    store.store_account(account, Decimal("12612.64"), date(2026, 5, 7), "INR")
    return store


# --- row shape ----------------------------------------------------------------


def test_direction_is_a_field_rather_than_a_sign():
    """Two pieces of code can disagree about a sign silently. They cannot
    disagree about `kind`."""
    assert build_split(txn(), ASSET)["kind"] == "withdrawal"
    assert build_split(txn(debit=None, credit=Decimal("65.00")), ASSET)["kind"] == "deposit"


def test_amount_is_positive_and_stays_a_decimal():
    """Non-negotiable 1, at the last boundary it could be lost."""
    for split in (
        build_split(txn(), ASSET),
        build_split(txn(debit=None, credit=Decimal("65.00")), ASSET),
    ):
        assert isinstance(split["amount"], Decimal)
        assert split["amount"] == Decimal("65.00")


def test_narration_is_preserved_verbatim_in_notes():
    assert build_split(txn(), ASSET)["notes"] == txn().narration


def test_the_counterparty_is_a_column_rather_than_an_account():
    """It used to live in whichever of source/destination the direction did not
    use, and a helper had to work out which."""
    assert build_split(txn(), ASSET)["counterparty"] == "ZOKVEX QI"
    assert build_split(txn(), ASSET)["account"] == ASSET


def test_external_id_is_the_banks_own_transaction_id():
    assert build_split(txn(), ASSET)["external_id"] == BASE_TXN_ID


def test_description_combines_payee_and_channel():
    assert build_split(txn(), ASSET)["description"] == "ZOKVEX QI (UPI)"


def test_missing_payee_becomes_unknown_channel():
    split = build_split(txn(payee=None), ASSET)
    assert split["description"] == "Unknown (UPI)"
    assert split["counterparty"] == "Unknown (UPI)"


def test_the_time_of_day_survives_the_write():
    """It did not, before: the old store had no column for it, so the hour had
    to be recovered from the archive every time a chart wanted it."""
    at = time(1, 51, 33)
    assert build_split(txn(txn_time=at), ASSET)["txn_time"] == at


def test_reversal_is_tagged_and_still_stored_as_a_deposit():
    split = build_split(
        txn(debit=None, credit=Decimal("65.00"), is_reversal=True), ASSET
    )
    assert split["kind"] == "deposit"
    assert "reversal" in split["tags"]


# --- the rules run before the write, not after --------------------------------


def test_no_rules_means_no_category_rather_than_a_guessed_one():
    split = build_split(txn(), ASSET)
    assert split["category"] == ""
    assert split["tags"] == []


def test_the_category_is_decided_before_the_row_is_stored():
    split = build_split(txn(), ASSET, rules=RULES)
    assert split["category"] == "Eating out"
    assert "food" in split["tags"]


def test_large_oneoff_is_tagged_now_that_the_category_is_known():
    big = txn(debit=Decimal("50000.00"))
    split = build_split(big, ASSET, rules=RULES, threshold=Decimal("10000"))
    assert "large-oneoff" in split["tags"]


def test_the_category_exclusion_that_used_to_be_inert():
    """It was documented as inert for a real reason: the engine that applied it
    ran at store time, and the category was not committed yet — so the rows the
    exclusion existed for were tagged anyway."""
    rules = {**RULES, "rules": [{"category": "Investments", "payees": ["ZOKVEX QI"]}]}
    split = build_split(
        txn(debit=Decimal("50000.00")), ASSET, rules=rules, threshold=Decimal("10000")
    )
    assert split["category"] == "Investments"
    assert "large-oneoff" not in split["tags"]


def test_a_threshold_without_rules_tags_nothing():
    """Asking what a row would look like is not the same as deciding what it is."""
    split = build_split(txn(debit=Decimal("50000.00")), ASSET, threshold=Decimal("10000"))
    assert split["tags"] == []


# --- the overlap is skipped by identity ---------------------------------------
#
# Every test below exists because of one incident. A statement overlapping an
# already-pushed period was re-uploaded after a config change. The old store
# decided duplicates on a hash of the SUBMITTED payload, the config change had
# rewritten the descriptions, so the hashes no longer matched and seven rows
# were written a second time. The balance went wrong by the sum of the extra
# copies and nothing raised.


def test_a_row_already_in_the_ledger_is_never_written_again():
    store = ledger()
    push_transactions(store, [txn()], ASSET)
    result = push_transactions(store, [txn()], ASSET)

    assert (result.pushed, result.already, result.duplicates, result.failed) == (0, 1, 0, 0)
    assert result.skipped == 1
    assert result.ok
    assert len(store.account_transactions(ASSET)) == 1


def test_a_changed_description_does_not_make_it_a_new_transaction():
    """The incident, reproduced.

    Same transaction, same id, different row — an alias renamed the payee
    between the two pushes. A content hash sees two different rows. passbook
    sees one id, and so does the primary key.
    """
    store = ledger()
    push_transactions(store, [txn()], ASSET)
    renamed = txn(payee="Someone Else Entirely")
    assert build_split(renamed, ASSET) != build_split(txn(), ASSET)  # a hash would differ

    result = push_transactions(store, [renamed], ASSET)
    assert result.already == 1
    assert len(store.account_transactions(ASSET)) == 1


def test_the_key_refuses_the_repeat_even_when_the_pre_read_misses_it():
    """The pre-read is an optimisation and a better message. The guarantee is
    the primary key, and it holds with the pre-read removed."""
    store = ledger()
    store.store_transaction(build_split(txn(), ASSET))
    with pytest.raises(LedgerError, match="already in the ledger"):
        store.store_transaction(build_split(txn(payee="Renamed"), ASSET))


def test_the_pre_migration_id_is_the_same_transaction_as_the_namespaced_one():
    """A bare id in the ledger still means "already there".

    The namespace migration can then be run when it suits rather than being
    forced by a push that would otherwise double every row.
    """
    from passbook.config import Account

    account = Account(
        slug="canara-1111",
        bank="canara",
        label="Savings",
        account_number="XXXXXXXX1111",
        asset_account=ASSET,
    )
    store = ledger()
    store.store_transaction(build_split(txn(), ASSET))  # written before the migration

    result = push_transactions(store, [txn()], account)
    assert result.already == 1
    assert result.pushed == 0


def test_only_the_rows_not_already_there_are_written():
    one, two, three = ids(3)
    store = ledger()
    push_transactions(store, [txn(txn_id=one)], ASSET)

    result = push_transactions(
        store,
        [txn(txn_id=one), txn(txn_id=two), txn(txn_id=three)],
        ASSET,
    )
    assert (result.pushed, result.already) == (2, 1)
    assert {r["external_id"] for r in store.account_transactions(ASSET)} == {one, two, three}


def test_one_statement_cannot_duplicate_itself():
    """`known` is updated as rows are written, so a file listing the same id
    twice writes it once."""
    store = ledger()
    result = push_transactions(store, [txn(), txn()], ASSET)
    assert (result.pushed, result.already) == (1, 1)
    assert len(store.account_transactions(ASSET)) == 1


def test_a_ledger_that_cannot_be_read_stops_the_push():
    """A read that failed and a read that found nothing are different answers.
    Writing on the second one when it was really the first is how an account
    gets doubled."""

    class Unreadable(MemoryLedger):
        def identities(self, account):
            raise LedgerError("connection refused")

    store = Unreadable()
    store.store_account(ASSET, Decimal("12612.64"), date(2026, 5, 7), "INR")
    with pytest.raises(LedgerError):
        push_transactions(store, [txn()], ASSET)
    assert store.account_transactions(ASSET) == []


def test_an_account_with_no_rows_yet_writes_everything():
    store = ledger()
    one, two = ids(2)
    result = push_transactions(store, [txn(txn_id=one), txn(txn_id=two)], ASSET)
    assert (result.pushed, result.already, result.failed) == (2, 0, 0)


def test_a_row_for_an_unregistered_account_fails_rather_than_vanishing():
    store = MemoryLedger()  # no accounts at all
    result = push_transactions(store, [txn()], ASSET)
    assert result.pushed == 0
    assert result.failed == 1
    assert not result.ok
    assert result.failures[0][0] == BASE_TXN_ID
