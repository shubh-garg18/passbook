"""The balance-continuity invariant and its supporting assertions. SPEC §6.6, §6.7.

CLAUDE.md non-negotiable #3: never soften or skip the continuity check to make
a test pass. These tests exist to prove it bites.
"""

from decimal import Decimal

import pytest

from conftest import FIXTURE_ACCOUNT, FIXTURE_TXN_COUNT
from passbook.loaders._table import from_rows
from passbook.validate import (
    AccountMismatch,
    BalanceBreak,
    IntegrityError,
    assert_account,
    check,
    check_continuity,
)


def test_reference_fixture_reconciles_cleanly(parsed):
    meta, transactions = parsed
    final = check_continuity(meta, transactions)
    assert final == meta.closing_balance
    assert len(transactions) == FIXTURE_TXN_COUNT


def test_full_check_passes_with_no_warnings(enriched):
    meta, transactions = enriched
    assert check(meta, transactions) == []


# --- the check must actually bite --------------------------------------------


def test_deleting_a_row_is_caught(rows):
    """SPEC §10 requires exactly this fixture: one row removed."""
    header = next(i for i, r in enumerate(rows) if "Trasnaction ID" in r)
    victim = header + 5  # a transaction row, safely past the Opening sentinel
    trimmed = [r for i, r in enumerate(rows) if i != victim]
    meta, transactions = from_rows(trimmed)
    assert len(transactions) == FIXTURE_TXN_COUNT - 1
    with pytest.raises(BalanceBreak) as exc:
        check_continuity(meta, transactions)
    # The message must name the offending row and both balances. SPEC §6.6.
    assert "balance break at sheet row" in str(exc.value)
    assert "continuity requires" in str(exc.value)


def test_duplicating_a_row_is_caught(rows):
    header = next(i for i, r in enumerate(rows) if "Trasnaction ID" in r)
    victim = header + 5
    doubled = rows[:victim] + [rows[victim]] + rows[victim:]
    meta, transactions = from_rows(doubled)
    # Caught as a duplicate ID before continuity even runs — either is a failure.
    with pytest.raises((IntegrityError, BalanceBreak)):
        check(meta, transactions)


def test_a_tampered_amount_is_caught(rows):
    tampered = [list(r) for r in rows]
    header = next(i for i, r in enumerate(tampered) if "Trasnaction ID" in r)
    for row in tampered[header + 2 :]:
        if row[2].strip() and row[2].strip() != " ":
            row[2] = "999999.00"  # inflate one withdrawal
            break
    meta, transactions = from_rows(tampered)
    with pytest.raises(BalanceBreak):
        check_continuity(meta, transactions)


def test_final_balance_must_equal_the_closing_sentinel(parsed):
    meta, transactions = parsed
    meta.closing_balance = meta.closing_balance + Decimal("1.00")
    with pytest.raises(BalanceBreak, match="Closing Balance sentinel"):
        check(meta, transactions)


def test_tolerance_is_one_paisa_not_more(parsed):
    meta, transactions = parsed
    transactions[0].balance += Decimal("0.009")  # under tolerance, accepted
    check_continuity(meta, transactions)
    transactions[0].balance += Decimal("0.01")  # now over
    with pytest.raises(BalanceBreak):
        check_continuity(meta, transactions)


def test_row_with_both_debit_and_credit_is_rejected(parsed):
    meta, transactions = parsed
    transactions[0].credit = Decimal("1.00")
    with pytest.raises(IntegrityError, match="both"):
        check(meta, transactions)


def test_row_with_neither_debit_nor_credit_is_rejected(parsed):
    meta, transactions = parsed
    transactions[0].debit = None
    transactions[0].credit = None
    with pytest.raises(IntegrityError, match="neither"):
        check(meta, transactions)


def test_duplicate_txn_id_is_rejected(parsed):
    meta, transactions = parsed
    transactions[1].txn_id = transactions[0].txn_id
    with pytest.raises(IntegrityError, match="duplicate transaction ID"):
        check(meta, transactions)


def test_empty_statement_is_rejected(parsed):
    meta, _ = parsed
    with pytest.raises(IntegrityError, match="no transactions"):
        check(meta, [])


# --- soft failures stay soft --------------------------------------------------


def test_txn_id_prefix_mismatch_warns_but_does_not_raise(parsed):
    meta, transactions = parsed
    transactions[0].txn_id = "19990101999999"
    warnings = check(meta, transactions)
    assert any("does not match" in w for w in warnings)


def test_backwards_date_warns_but_does_not_raise(enriched):
    meta, transactions = enriched
    transactions[-1].txn_date = transactions[0].txn_date.replace(year=2020)
    warnings = check(meta, transactions)
    assert any("goes backwards" in w for w in warnings)


# --- §6.7 account safety assertion -------------------------------------------


def test_matching_account_passes(parsed):
    meta, _ = parsed
    assert_account(meta, FIXTURE_ACCOUNT) is None


def test_mismatched_account_refuses(parsed):
    meta, _ = parsed
    with pytest.raises(AccountMismatch, match="refusing to continue"):
        assert_account(meta, "123456789012")


def test_mismatch_message_never_leaks_a_full_account_number(parsed):
    meta, _ = parsed
    with pytest.raises(AccountMismatch) as exc:
        assert_account(meta, "123456789012")
    assert FIXTURE_ACCOUNT not in str(exc.value)
    assert "123456789012" not in str(exc.value)
    assert "****1111" in str(exc.value)


def test_accounts_sharing_last_four_still_mismatch(parsed):
    """Masking hides the difference; the comparison must not."""
    meta, _ = parsed
    with pytest.raises(AccountMismatch, match="differ before the last 4"):
        assert_account(meta, "111100001111")


def test_unset_account_number_refuses(parsed):
    meta, _ = parsed
    with pytest.raises(AccountMismatch, match="not set"):
        assert_account(meta, None)


def test_statement_meta_repr_hides_credentials(parsed):
    """The customer ID is also the PDF statement password. SPEC §11."""
    meta, _ = parsed
    text = repr(meta)
    assert meta.account_number not in text
    assert meta.customer_id not in text
