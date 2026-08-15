"""Canara Bank. SPEC §6.3, §6.5, §6.8.

The reference dialect, and the only one verified against a real export — 93
transactions over three months. Everything here was measured off that file, not
assumed; where a fact came from somewhere else it says so.

This module is what `docs/adding-a-bank.md` tells a contributor to copy.
"""

from __future__ import annotations

import re
from datetime import date

from .. import narration as _narration
from . import Bank, Rows, register

# Locale-independent on purpose. `%b` depends on the active locale, which
# differs between machines, and this bank writes its months uppercase.
# SPEC §6.3.
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_DATE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")

PERIOD = re.compile(
    r"from\s+(\d{1,2}-[A-Za-z]{3}-\d{4})\s+to\s+(\d{1,2}-[A-Za-z]{3}-\d{4})",
    re.IGNORECASE,
)

# The bank misspells "Transaction" in its own export. Match it as written;
# accept the correct spelling too, in case they ever fix it. SPEC §6.3.
COLUMNS = {
    "date": "date",
    "trasnactionid": "txn_id",
    "transactionid": "txn_id",
    "withdrawals": "debit",
    "withdrawal": "debit",
    "deposits": "credit",
    "deposit": "credit",
    "balance": "balance",
    "remarks": "narration",
}
REQUIRED = frozenset({"date", "txn_id", "debit", "credit", "balance", "narration"})

METADATA = {
    "accountnumber": "account_number",
    "customerid": "customer_id",
    # The PDF export calls it Client rather than Customer ID. SPEC §6.8.
    "client": "customer_id",
    "name": "account_name",
    "branchcode": "branch_code",
    "ifsccode": "ifsc",
    "ifsc": "ifsc",
}


def parse_date(text: str) -> date:
    from ..loaders._table import ParseError

    match = _DATE.match(text.strip())
    if not match:
        raise ParseError(f"unparseable date {text!r}")
    day, month, year = match.groups()
    if month.upper() not in MONTHS:
        raise ParseError(f"unknown month {month!r} in {text!r}")
    return date(int(year), MONTHS[month.upper()], int(day))


def detect(rows: Rows) -> bool:
    """Two independent markers, either of which is enough.

    The header spelling is the strong one: `Trasnaction ID` is this bank's own
    typo and nobody else's. The bank name in the preamble is the readable one,
    and survives a future export that fixes the spelling.

    Deliberately not the filename — Canara names every export for a date range,
    and §21.6's rule is that a statement is attributed by what it says, never by
    where it sits.
    """
    from ..loaders._table import norm

    for row in rows[:50]:
        for cell in row:
            text = norm(cell)
            if text in {"trasnactionid", "canarabank"}:
                return True
            if text.startswith("canarabank"):
                return True
    return False


CANARA = register(
    Bank(
        slug="canara",
        name="Canara Bank",
        column_aliases=COLUMNS,
        required_columns=REQUIRED,
        metadata_labels=METADATA,
        metadata_label_columns=(0, 3),
        opening_label="openingbalance",
        closing_label="closingbalance",
        period_pattern=PERIOD,
        parse_date=parse_date,
        narration_matchers=_narration.CANARA_MATCHERS,
        strip_trailing_timestamp=_narration.strip_trailing_timestamp,
        extract_time=_narration.extract_time,
        # MEASURED, and earlier guidance in this project was wrong: it is the
        # last four digits of the account number, not the Customer ID. All 94
        # numeric candidates in the statement were tried. SPEC §6.8.1.
        pdf_password_hint=(
            "Canara's PDF is the last FOUR DIGITS of the account number "
            "(not the Customer ID). Set CANARA_PDF_PASSWORD."
        ),
        detect=detect,
    )
)
