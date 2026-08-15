"""Data model. SPEC §6.1.

Money is Decimal everywhere. Never float. No exceptions.
"""

from datetime import date, time
from decimal import Decimal

from pydantic import BaseModel, Field

# SPEC §6.1. Channels the narration matchers can assign.
UPI = "UPI"
IMPS = "IMPS"
NEFT = "NEFT"
INT = "INT"  # savings interest
CHG = "CHG"  # bank charges
SCHEME = "SCHEME"  # insurance / government scheme
OTHER = "OTHER"


def mask_account(number: str) -> str:
    """Last 4 only. SPEC §11 — a full account number must never reach a log."""
    tail = number.strip()[-4:]
    return f"****{tail}"


class StatementMeta(BaseModel):
    """The metadata block above the header row. SPEC §6.3.

    `account_number` and `customer_id` are repr=False on purpose. The customer
    ID is also the password for Canara's PDF statements (SPEC §11), so an
    accidental `print(meta)` or a pydantic validation error must not spill it.
    """

    account_number: str = Field(repr=False)
    customer_id: str = Field(repr=False)
    account_name: str = Field(repr=False)
    branch_code: str
    ifsc: str
    period_from: date
    period_to: date
    opening_balance: Decimal
    closing_balance: Decimal

    @property
    def masked_account(self) -> str:
        return mask_account(self.account_number)


class Transaction(BaseModel):
    txn_id: str
    txn_date: date
    narration: str  # raw Remarks cell, untouched
    debit: Decimal | None
    credit: Decimal | None
    balance: Decimal

    # populated by narration.py
    channel: str = OTHER
    payee: str | None = None  # truncated to ~10 chars for UPI. SPEC §6.5
    utr: str | None = None
    counterparty_bank: str | None = None
    is_reversal: bool = False

    # Canonical display name from config/payee_aliases.yaml, when one applies.
    # `payee` above always keeps the bank's raw truncated token — an alias is a
    # display concern and never rewrites source data. SPEC D10.
    payee_alias: str | None = None

    # Time of day from the narration's embedded timestamp, where it carries one.
    # The statement is 82/93 UPI and the hour is real signal — a canteen at
    # 01:51 is a different thing from a canteen at 13:00. There is no time
    # column; this is recovered from the narration. SPEC §6.5.
    txn_time: time | None = None

    # Not in SPEC §6.1's listing. Added so §6.6 can name the offending *sheet*
    # row when the balance invariant breaks — a position in the transaction
    # list is not actionable when you are staring at the file in a spreadsheet.
    sheet_row: int = -1

    @property
    def amount(self) -> Decimal:
        """Signed movement: negative for a withdrawal."""
        return (self.credit or Decimal(0)) - (self.debit or Decimal(0))

    @property
    def display_payee(self) -> str | None:
        """What a human should see. Falls back to the raw token."""
        return self.payee_alias or self.payee


def normalised(
    meta: StatementMeta, transactions: list["Transaction"], warnings: list[str]
) -> dict:
    """Stable JSON shape, shared by `passbook parse --json` and the golden test.

    The account number is masked and the customer ID omitted entirely: this is
    written to stdout and to a committed fixture, both of which are logs. SPEC §11.
    """
    return {
        "meta": {
            "account": meta.masked_account,
            "branch_code": meta.branch_code,
            "ifsc": meta.ifsc,
            "period_from": meta.period_from.isoformat(),
            "period_to": meta.period_to.isoformat(),
            "opening_balance": str(meta.opening_balance),
            "closing_balance": str(meta.closing_balance),
        },
        "transactions": [
            t.model_dump(mode="json", exclude={"sheet_row"}) for t in transactions
        ],
        "warnings": warnings,
    }
