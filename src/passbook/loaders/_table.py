"""Shared row-grid -> (StatementMeta, list[Transaction]) core. SPEC §6.3, §22.5.

Every loader normalises its container (OLE2 sheet, HTML table, delimited text,
PDF page) into a plain list-of-rows-of-strings and hands it here. A bank's
layout is identical across containers; only the envelope differs. That is
exactly the case SPEC D4's sniffer exists to catch — the bank changing its
export backend without changing the statement itself.

**This module knows about statements, not about any particular bank.** Column
spellings, date formats, sentinel labels and metadata labels all come from a
`banks.Bank` value, detected from the grid's own content. That is the seam a
second bank goes through, and it is why `from_rows` is the one function no bank
may override: the balance-continuity invariant downstream of it (§6.6) must
never fork.

Nothing here converts a cell to anything but `str` first. Every cell in the real
export is text, including amounts, and inferring dtypes is how you end up with
floats in a ledger.
"""

import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from ..models import StatementMeta, Transaction

log = logging.getLogger(__name__)

Rows = list[list[str]]


class ParseError(ValueError):
    """The grid does not look like a statement any registered bank writes."""


# ── Canara constants, kept importable ────────────────────────────────────────
# These moved to `banks/canara.py` when the registry landed (§22.5). They are
# re-exported here because tests, `scripts/redact.py` and `scripts/pdfwrite.py`
# import them by these names, and a rename that breaks a fixture generator is a
# rename that silently stops the fixtures being regenerated.
#
# New code should read `bank.parse_date`, `bank.column_aliases` and friends off
# the detected bank rather than reaching for these.
def _canara():
    from ..banks import get

    return get("canara")


MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def norm(text: str) -> str:
    """Strip non-alphanumerics and lowercase, for tolerant header matching."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def parse_date(text: str) -> date:
    """Canara's `DD-MMM-YYYY`. Kept for callers that predate the registry.

    Bank-aware code calls `bank.parse_date` instead; this is the same function.
    """
    return _canara().parse_date(text)


def parse_amount(text: str) -> Decimal | None:
    """`'10,000.00'` -> Decimal. `' '` -> None.

    The empty amount cell is a single space, not an empty string, in all 93
    transaction rows of the reference statement. Stripping before testing
    emptiness is what makes this return None rather than a silent
    Decimal('0'). SPEC §6.3 calls this the likeliest source of a silent bug.
    """
    cleaned = text.strip().replace(",", "")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ParseError(f"unparseable amount {text!r}") from exc


def _at(rows: Rows, r: int, c: int) -> str:
    if r < 0 or r >= len(rows) or c < 0 or c >= len(rows[r]):
        return ""
    return rows[r][c]


def _find_header(rows: Rows, bank) -> tuple[int, dict[str, int]]:
    """Scan downward for the header rather than hardcoding a row. SPEC §6.3.

    Columns are mapped by header TEXT, never by index: a bank that inserts a
    column would otherwise shift every field silently, which is the shape of
    error that reaches the ledger looking correct.
    """
    for r in range(min(len(rows), 50)):
        mapping: dict[str, int] = {}
        for c in range(len(rows[r])):
            field = bank.column_aliases.get(norm(_at(rows, r, c)))
            if field and field not in mapping:
                mapping[field] = c
        if len(mapping) >= 4:
            missing = bank.required_columns - mapping.keys()
            if missing:
                raise ParseError(
                    f"header at row {r} is missing {sorted(missing)} "
                    f"for {bank.name}"
                )
            log.info("header row %d, columns %s", r, mapping)
            return r, mapping
    raise ParseError(f"no {bank.name} header row found in the first 50 rows")


def _find_metadata(rows: Rows, header_row: int, bank) -> dict[str, str]:
    """Label/value pairs above the header; a value sits right of its label.

    Which columns carry labels is the bank's business — Canara uses two pairs
    side by side, at 0-1 and 3-4. SPEC §6.3.
    """
    found: dict[str, str] = {}
    for r in range(header_row):
        for label_col in bank.metadata_label_columns:
            field = bank.metadata_labels.get(norm(_at(rows, r, label_col)))
            if field and field not in found:
                value = _at(rows, r, label_col + 1).strip()
                if value:
                    found[field] = value
    return found


def _find_sentinel(rows: Rows, header_row: int, want: str) -> tuple[int, Decimal]:
    """Opening/Closing Balance rows: label in col 1, balance further right."""
    for r in range(header_row + 1, len(rows)):
        for c in range(len(rows[r])):
            if norm(_at(rows, r, c)) == want:
                for probe in range(len(rows[r]) - 1, c, -1):
                    amount = parse_amount(_at(rows, r, probe))
                    if amount is not None:
                        return r, amount
                raise ParseError(f"{want} row {r} carries no balance")
    raise ParseError(f"no {want} sentinel row found")


def from_rows(rows: Rows, bank=None) -> tuple[StatementMeta, list[Transaction]]:
    """The one path every container and every bank funnels through. SPEC §22.5.

    `bank` is detected from the grid's own content when not given, so a
    statement is read by the dialect it was written in rather than by where the
    file happens to sit (§21.6 applies the same rule to accounts).

    Deliberately not overridable per bank. Everything downstream — the
    continuity invariant, the account assertion, the push — trusts that a
    `Transaction` means the same thing whoever produced it.
    """
    from ..banks import detect as detect_bank

    bank = bank or detect_bank(rows)
    header_row, cols = _find_header(rows, bank)
    meta_fields = _find_metadata(rows, header_row, bank)

    period_from = period_to = None
    for r in range(header_row):
        for c in range(len(rows[r])):
            m = bank.period_pattern.search(_at(rows, r, c))
            if m:
                period_from = bank.parse_date(m.group(1))
                period_to = bank.parse_date(m.group(2))
                break
        if period_from:
            break
    if period_from is None:
        raise ParseError("no 'Statement for Account from <date> to <date>' line found")

    open_row, opening = _find_sentinel(rows, header_row, bank.opening_label)
    close_row, closing = _find_sentinel(rows, header_row, bank.closing_label)

    missing = {"account_number", "customer_id"} - meta_fields.keys()
    if missing:
        raise ParseError(f"metadata block is missing {sorted(missing)}")

    meta = StatementMeta(
        account_number=meta_fields["account_number"],
        customer_id=meta_fields["customer_id"],
        account_name=meta_fields.get("account_name", ""),
        branch_code=meta_fields.get("branch_code", ""),
        ifsc=meta_fields.get("ifsc", ""),
        period_from=period_from,
        period_to=period_to,
        opening_balance=opening,
        closing_balance=closing,
    )

    transactions: list[Transaction] = []
    for r in range(open_row + 1, close_row):
        date_text = _at(rows, r, cols["date"]).strip()
        id_text = _at(rows, r, cols["txn_id"]).strip()
        if not date_text and not id_text:
            continue  # blank spacer row
        balance = parse_amount(_at(rows, r, cols["balance"]))
        if balance is None:
            raise ParseError(f"row {r}: transaction has no balance")
        transactions.append(
            Transaction(
                txn_id=id_text,
                txn_date=bank.parse_date(date_text),
                narration=_at(rows, r, cols["narration"]),
                debit=parse_amount(_at(rows, r, cols["debit"])),
                credit=parse_amount(_at(rows, r, cols["credit"])),
                balance=balance,
                sheet_row=r,
            )
        )

    log.info(
        "parsed %d %s transactions (rows %d-%d)",
        len(transactions), bank.slug, open_row + 1, close_row - 1,
    )
    return meta, transactions
