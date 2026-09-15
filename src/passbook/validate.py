"""Balance-continuity invariant and the supporting assertions. SPEC §6.6, §6.7.

This is the only thing standing between a parsing regression and silently wrong
financial data. It is verified to hold cleanly on real data — 93 rows, 0 breaks,
computed final balance equal to the Closing Balance sentinel exactly.

**So if it fails, the parser is wrong, not the data. Never soften or skip this
check to make a test pass.** CLAUDE.md non-negotiable #3.

Hard failures (raise) are reserved for things that mean the parse is wrong or
The ledger would be corrupted. Soft failures (warn) are observations about the
bank's habits that could legitimately change without any data being wrong.
"""

import re
from decimal import Decimal

from .models import StatementMeta, Transaction, mask_account

TOLERANCE = Decimal("0.01")

# A bank-numbered transaction id: `YYYYMMDD` + a daily ordinal, which is what
# Canara writes. A derived id (§44.4) is `d-<sha256[:16]>` and matches nothing
# here, which is the point.
_BANK_NUMBERED = re.compile(r"\d{8}\d+")


class BalanceBreak(Exception):
    """The running balance does not reconcile. SPEC §6.6."""


class IntegrityError(Exception):
    """A structural assertion about the statement failed. SPEC §6.6."""


class AccountMismatch(Exception):
    """Statement belongs to a different account than configured. SPEC §6.7."""


def check_continuity(meta: StatementMeta, transactions: list[Transaction]) -> Decimal:
    """balance[i] == balance[i-1] - debit[i] + credit[i], seeded from Opening.

    Returns the computed final balance. Raises BalanceBreak naming the sheet row
    and both balances.
    """
    running = meta.opening_balance
    for txn in transactions:
        expected = running - (txn.debit or Decimal(0)) + (txn.credit or Decimal(0))
        if abs(txn.balance - expected) >= TOLERANCE:
            raise BalanceBreak(
                f"balance break at sheet row {txn.sheet_row} "
                f"(txn {txn.txn_id}, {txn.txn_date}): "
                f"statement says {txn.balance}, continuity requires {expected} "
                f"(previous {running} - debit {txn.debit or 0} + credit {txn.credit or 0}). "
                f"A row was dropped, duplicated, or misparsed — fix the parser, not this check."
            )
        running = txn.balance
    return running


def check(meta: StatementMeta, transactions: list[Transaction]) -> list[str]:
    """Run every §6.6 assertion. Returns warnings; raises on hard failures."""
    if not transactions:
        raise IntegrityError("statement contains no transactions")

    warnings: list[str] = []

    # --- hard: exactly one of debit/credit per row ---------------------------
    for txn in transactions:
        if (txn.debit is None) == (txn.credit is None):
            both = "both" if txn.debit is not None else "neither"
            raise IntegrityError(
                f"sheet row {txn.sheet_row} (txn {txn.txn_id}) has {both} "
                f"debit and credit populated; exactly one is required"
            )

    # --- hard: txn_id unique within the file ---------------------------------
    seen: dict[str, int] = {}
    for txn in transactions:
        if txn.txn_id in seen:
            raise IntegrityError(
                f"duplicate transaction ID {txn.txn_id} at sheet rows "
                f"{seen[txn.txn_id]} and {txn.sheet_row}; it is used as the "
                f"ledger external_id and must be unique"
            )
        seen[txn.txn_id] = txn.sheet_row

    # --- hard: continuity, then final == closing sentinel --------------------
    final = check_continuity(meta, transactions)
    if abs(final - meta.closing_balance) >= TOLERANCE:
        raise BalanceBreak(
            f"computed final balance {final} != Closing Balance sentinel "
            f"{meta.closing_balance}"
        )

    # --- soft: the bank's habits, not correctness ----------------------------
    #
    # **Only for a bank that numbers its rows.** Canara's id is `YYYYMMDD` plus
    # a daily ordinal, so a prefix that disagrees with the date means a row was
    # misread. A bank with no reference column gets a DERIVED id instead
    # (§44.4) — `d-<sha256[:16]>` over the row's own fields — which contains no
    # date by construction, so checking it against one warns about something
    # the format guarantees.
    #
    # Measured on a real 32-row Union statement: 32 warnings, one per row,
    # every one of them noise, in a list the operator has to read to find the
    # seven that might matter. A check that cries wolf on every row of a whole
    # bank is worse than no check, because it trains the reader to skip the
    # section it lives in.
    for txn in transactions:
        if not _BANK_NUMBERED.fullmatch(txn.txn_id):
            continue
        prefix = txn.txn_id[:8]
        if prefix != txn.txn_date.strftime("%Y%m%d"):
            warnings.append(
                f"row {txn.sheet_row}: txn_id prefix {prefix} does not match "
                f"date {txn.txn_date}"
            )

    for prev, curr in zip(transactions, transactions[1:]):
        if curr.txn_date < prev.txn_date:
            warnings.append(
                f"row {curr.sheet_row}: date {curr.txn_date} goes backwards "
                f"from {prev.txn_date}"
            )

    # SPEC §6.5: direction lives in the narration too, but the
    # Withdrawals/Deposits columns are authoritative. A disagreement is worth
    # surfacing and is explicitly not an error.
    #
    # **`/DR/` and `/CR/` are Canara's convention**, and the marker sits in a
    # specific PLACE: segment 1 of a slash-delimited narration, immediately
    # after the channel — `UPI/DR/<utr>/…`. Verified against the fixture, which
    # is unanimous: marker index 1, twelve segments, channel `UPI`.
    #
    # Anywhere else those two letters are somebody else's reference. A first
    # attempt only required two slashes, which still fired on seven rows of a
    # real Union statement — and the balance chain on that file closes on its
    # own sentinel with zero breaks, which is proof the COLUMNS are right and
    # therefore that the narration marker means something other than this
    # row's direction. §6.6 is the arbiter; a soft warning is not.
    for txn in transactions:
        upper = txn.narration.upper()
        segments = upper.split("/")
        marker = segments[1] if len(segments) > 2 else ""
        says_debit = marker == "DR"
        says_credit = marker == "CR" or upper.startswith("NEFT CR")
        if says_debit and txn.debit is None:
            warnings.append(f"row {txn.sheet_row}: narration says DR but column says deposit")
        if says_credit and txn.credit is None:
            warnings.append(f"row {txn.sheet_row}: narration says CR but column says withdrawal")

    return warnings


class UnknownAccount(AccountMismatch):
    """The statement is for an account this install does not know. SPEC §21.2.

    A subclass of `AccountMismatch` on purpose: every front end already refuses
    that with a 422 and deletes the staged file, so an unregistered account
    inherits the guarantee that matters — **it can never silently import**.

    Carries the masked number and the known accounts so the UI can offer to add
    it without the caller re-deriving either.
    """

    def __init__(self, masked: str, known: list[str]) -> None:
        self.masked = masked
        self.known = known
        listed = ", ".join(known) if known else "none registered yet"
        super().__init__(
            f"statement is for account {masked}, which is not in the registry "
            f"(known: {listed}). Refusing to import it — add the account first, "
            "so it gets its own external_id namespace and its own opening "
            "balance instead of merging into another ledger."
        )


def assert_account(meta: StatementMeta, expected: str | None) -> None:
    """Refuse a statement from a different account. SPEC §6.7.

    Only masked numbers appear in the message — SPEC §11 forbids a full account
    number reaching any log or traceback.
    """
    if not expected:
        raise AccountMismatch(
            "PASSBOOK_ACCOUNT_NUMBER is not set in .env, so the statement cannot "
            "be confirmed to belong to your account (SPEC §6.7)."
        )
    if meta.account_number.strip() != expected.strip():
        detail = ""
        if meta.masked_account == mask_account(expected):
            # Comparison is on the full number, so two accounts sharing their
            # last 4 still mismatch. Say so, or the message reads as a bug.
            detail = " (they differ before the last 4, which masking hides)"
        raise AccountMismatch(
            f"statement is for account {meta.masked_account} but "
            f"PASSBOOK_ACCOUNT_NUMBER is {mask_account(expected)}{detail} — "
            f"refusing to continue so a misfiled statement cannot corrupt the ledger."
        )
