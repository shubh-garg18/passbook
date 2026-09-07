"""A bank that centres its column headings. SPEC §51.

**Reconstructed from a shape dump, not from a statement.** An operator hit
`row N: transaction has no balance` on a file nobody may read (§46) and sent the
page as geometry and token shapes (§50) — `92-92-94` for a date, `A5` for a
payee, `9,93.92` for an amount. This module turns those shapes back into words
of the right length at the right coordinates, with invented content.

So the geometry is real and measured; every character is made up. That is enough
to reproduce the bug, because the bug is made of geometry.

What the dump showed, and what Canara had hidden:

| column | heading x-range | where its values actually are |
|---|---|---|
| Date | 63.5 – 81.5 | **50.0** – 91.9 |
| Particulars | 149.9 – 190.1 | **105.0** – … |
| Withdrawal | 337.7 – 382.3 | ending **395.0** |
| Deposit | 425.8 – 454.2 | ending **475.0** |
| Balance | 512.7 – 542.3 | ending **560.0**, then a separate `Cr` |

Every heading is offset from its own column — this bank **centres** them, where
Canara aligns each one with its values. Every rule anchored on a heading missed.
"""

from __future__ import annotations

HEADINGS = {
    "date": (63.5, 81.5, "Date"),
    "narration": (149.9, 190.1, "Particulars"),
    "debit": (337.7, 382.3, "Withdrawal"),
    "credit": (425.8, 454.2, "Deposit"),
    "balance": (512.7, 542.3, "Balance"),
}

#: `SL` and `Chq No`, which the profile does not map. They are in the file and
#: therefore in the test: an unmapped column is one more cluster the learner has
#: to not be confused by.
EXTRA_HEADINGS = [(29.2, 35.8, "SL"), (263.0, 277.1, "Chq"), (279.2, 297.0, "No.")]

PROFILE = {
    "date": "date",
    "particulars": "narration",
    "withdrawal": "debit",
    "deposit": "credit",
    "balance": "balance",
}


def _word(text: str, x0: float, x1: float, top: float) -> dict:
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": top + 9.0}


def _amount(value: str, ends_at: float, top: float) -> dict:
    """Right-aligned, the way money is set. Width from the digit count."""
    return _word(value, ends_at - len(value) * 4.3, ends_at, top)


def lines() -> list[list[dict]]:
    """The page: a details table, a centred header, then transactions."""
    out: list[list[dict]] = []
    top = 40.0

    # --- the details table, above the header (§50.1) ------------------------
    # Deliberately carrying words the profile also maps, because that is what
    # made a details line win the header before it scored them.
    out.append([_word("Statement", 242.3, 279.5, top), _word("of", 282.0, 294.4, top)])
    top += 12
    out.append([
        _word("Name", 25.0, 47.3, top), _word("A", 49.3, 55.7, top),
        _word("Balance", 199.8, 235.4, top), _word("Date", 260.0, 301.0, top),
    ])
    top += 12
    out.append([
        _word("Account", 25.0, 51.0, top), _word("No.", 53.0, 74.5, top),
        _word("410250014782", 260.0, 329.1, top),
    ])
    top += 12
    out.append([_word("IFSC", 25.0, 46.0, top), _word("ABCD0001234", 205.6, 250.0, top)])
    top += 16

    # --- the header, CENTRED over its columns -------------------------------
    header = [_word(text, x0, x1, top) for x0, x1, text in EXTRA_HEADINGS]
    header += [_word(text, x0, x1, top) for x0, x1, text in HEADINGS.values()]
    out.append(sorted(header, key=lambda w: w["x0"]))
    header_index = len(out) - 1
    top += 14

    # --- transactions -------------------------------------------------------
    rows = [
        ("01-08-2026", "OPENING CARRY FORWARD", None, None, "10,000.00"),
        ("02-08-2026", "UPI/DR/194892411578/ABCDE", "1,418.91", None, "8,581.09"),
        ("03-08-2026", "NEFT/CR/883012455901/WXYZQ", None, "25,000.00", "33,581.09"),
        ("05-08-2026", "UPI/DR/933287115871/ABCDE", "5,978.53", None, "27,602.56"),
        ("08-08-2026", "IMPS/DR/450076279125/PQRST", "1,008.01", None, "26,594.55"),
        ("11-08-2026", "UPI/CR/740899331886/LMNOP", None, "994.18", "27,588.73"),
    ]
    for index, (date, narration, debit, credit, balance) in enumerate(rows, start=1):
        line = [
            _word(str(index), 35.5, 40.0, top),
            # The date VALUE starts at 50.0. Its heading starts at 63.5.
            _word(date, 50.0, 91.9, top),
            # The narration VALUE starts at 105.0. Its heading starts at 149.9.
            _word(narration, 105.0, 105.0 + len(narration) * 4.3, top),
        ]
        if debit:
            line.append(_amount(debit, 395.0, top))
        if credit:
            line.append(_amount(credit, 475.0, top))
        line.append(_amount(balance, 560.0, top))
        # `Cr` is its OWN token, after the figure — pdfplumber does not join them.
        line.append(_word("Cr", 562.1, 570.0, top))
        out.append(line)
        top += 11

        # Wrapped narration, under the narration column.
        out.append([_word("CONTINUED/TAIL/0000", 105.0, 223.5, top)])
        top += 11

    # --- page furniture, at the far right -----------------------------------
    out.append([
        _word("Page", 503.0, 519.0, top), _word("1", 521.0, 525.0, top),
        _word("of", 527.0, 533.0, top), _word("1", 535.0, 539.0, top),
    ])
    return out


def header_line() -> int:
    """Index of the header row in `lines()` — four details lines precede it."""
    return 4
