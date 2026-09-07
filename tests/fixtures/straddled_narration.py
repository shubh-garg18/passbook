"""A bank that centres a multi-line description on its transaction row. §96.

**Reconstructed from geometry, not from a statement.** A real 32-row file
produced every payee as `(unparsed)` and seven direction-check warnings; a dump
of the page showed why, and the numbers below are that dump's:

    top=351.5  x0=105.0   <continuation>     <- belongs to the row BELOW
    top=357.0  x0= 35.5   06-04-2024 ...     <- the transaction
    top=362.5  x0=105.0   <continuation>     <- belongs to the row ABOVE

The description block is vertically **centred** on its row at +/-5.5pt, and
rows are 20pt apart. Canara wraps downward only, so `grid[-1]` was always the
right owner there and the bug could not appear on the reference bank.

Every coordinate is measured. Every character is invented.
"""

from __future__ import annotations

HEADINGS = {
    "date": (35.0, 55.0, "Date"),
    "narration": (105.0, 140.0, "Particulars"),
    "debit": (300.0, 340.0, "Withdrawal"),
    "credit": (380.0, 415.0, "Deposit"),
    "balance": (500.0, 535.0, "Balance"),
}

PROFILE = {
    "bank": "straddle",
    # Keys NORMALISED, exactly as `profiles.load_profiles` stores them:
    # `find_header` looks up `norm(heading)`, so a raw "Date" key matches
    # nothing and the whole profile is silently skipped as "not this bank".
    "columns": {
        "date": "date",
        "particulars": "narration",
        "withdrawal": "debit",
        "deposit": "credit",
        "balance": "balance",
    },
    "metadata": {},
    "dates": ["%d-%m-%Y"],
    "derive_txn_id": True,
    "builtin": False,
}


def _word(text: str, x0: float, top: float) -> dict:
    # 5.6pt per character is this font's measured average; only the START of a
    # text word matters to the banding, and the END only for money.
    return {"text": text, "x0": x0, "x1": x0 + 5.6 * len(text), "top": top}


def _money(value: str, ends_at: float, top: float) -> dict:
    return {"text": value, "x0": ends_at - 5.6 * len(value), "x1": ends_at, "top": top}


def lines() -> list[list[dict]]:
    """Header, then three transactions — the middle one with a straddling
    description, the others with none.

    **Every row carries all three money columns**, one of them a `-` placeholder,
    because `_learn_columns` clusters the x-positions of amount tokens to find
    the money edges (§48): with a column that is empty on every row there are
    two clusters for three columns and the learner falls back to the headings.
    Real statements print the placeholder; the fixture has to as well.
    """
    out: list[list[dict]] = [
        [_word(text, x0, 300.0) for x0, _x1, text in HEADINGS.values()]
    ]

    def txn(date_text: str, debit: str, credit: str, balance: str, top: float,
            narration: str | None = None) -> list[dict]:
        row = [_word(date_text, 35.5, top)]
        if narration:
            row.append(_word(narration, 105.0, top))
        row.append(_money(debit, 340.0, top))
        row.append(_money(credit, 415.0, top))
        row.append(_money(balance, 535.0, top))
        return row

    out.append(txn("01-04-2024", "100.00", "-", "1,000.00", 337.5, "PLAIN"))
    # The straddle: one line ABOVE row 2, one BELOW it.
    out.append([_word("FIRSTHALF", 105.0, 351.5)])
    out.append(txn("06-04-2024", "50.00", "-", "950.00", 357.0))
    out.append([_word("SECONDHALF", 105.0, 362.5)])
    # Row 3, far below, must not collect either half.
    out.append(txn("30-04-2024", "25.00", "-", "925.00", 377.5, "OTHER"))
    return out
