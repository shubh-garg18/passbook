"""A bank that does not print its column headings. SPEC §99.

**Reconstructed from geometry, not from a statement.** A real 7-page export was
probed the §50 way — every word reduced to a shape and an x-range — and the
coordinates below are that probe's. Every character is invented.

What the probe showed, and why none of it can be read by matching header text:

    header line   only the word `Balance`, at 514.4-550.6
                  the other five headings are a background graphic

`find_header` wants four matched column names before it will call a line a
header, so there is no header to find. The columns are declared instead.

Three more things this layout does that the reader had to learn:

* money is **centred**, not right-aligned: the same column ends at 379.7 for a
  six-character figure and 383.1 for an eight-character one, both centred on
  367.5. Every rule in `pdf_table` that bands by a right edge misses one of them;
* the description block runs from **7.1pt above its own dated line to 30.1pt
  below it**, with rows 50pt apart — so the last line of a block is nearer the
  next transaction than its own, and nearest-first files it on the wrong row;
* an empty money cell is printed `-`, including a reference column that is `-`
  on every row.

    top=531.3  x0=138   <block line 1>      <- belongs to the row BELOW
    top=538.4  x0=27.5  02/01/2026 ...      <- the transaction
    top=540.6  x0=138   <block line 2>      <- and the next four to this row
    ...
    top=568.5  x0=138   <block line 5>
    top=581.3  x0=138   <block line 1>      <- belongs to the NEXT row
    top=588.4  x0=27.5  05/01/2026 ...
"""

from __future__ import annotations

#: What the profile declares. Field -> the x-range its column occupies.
COLUMNS_AT = {
    "date": (20.0, 76.0),
    "narration": (128.0, 292.0),
    "txn_id": (294.0, 320.0),
    "debit": (330.0, 400.0),
    "credit": (410.0, 478.0),
    "balance": (495.0, 565.0),
}

#: And what it calls them. These words are nowhere on the page — they are the
#: header row `to_grid` synthesises so `from_rows` can read its own grid back.
PROFILE = {
    "date": "date",
    "description": "narration",
    "debit": "debit",
    "credit": "credit",
    "balance": "balance",
}

ORDER = ["date", "narration", "debit", "credit", "balance"]

#: Centres of the four money columns, measured.
DEBIT_AT, CREDIT_AT, BALANCE_AT, REF_AT = 367.5, 447.5, 530.0, 305.0

_TEXT_WIDTH = 5.6  # points per character, this font, measured
_MONEY_WIDTH = 3.9


def _word(text: str, x0: float, top: float) -> dict:
    return {"text": text, "x0": x0, "x1": x0 + _TEXT_WIDTH * len(text), "top": top}


def _centred(text: str, centre: float, top: float) -> dict:
    half = _MONEY_WIDTH * len(text) / 2
    return {"text": text, "x0": centre - half, "x1": centre + half, "top": top}


def _date(text: str, x0: float, top: float) -> dict:
    return {"text": text, "x0": x0, "x1": x0 + 4.0 * len(text), "top": top}


#: Where a description block sits relative to its own dated line, measured.
BLOCK_OFFSETS = (-7.1, 2.2, 11.5, 20.8, 30.1)
ROW_PITCH = 50.0


def _preamble(top: float) -> list[list[dict]]:
    """Label/value pairs, printed as words rather than cells — a PDF has no
    cells, which is the whole reason `_find_metadata` grew two more strategies
    (§44). The account number is the one thing never derived."""
    return [
        [_word("Account", 348.0, top), _word("Number", 386.9, top),
         _word("410250014782", 451.0, top)],
        [_word("IFS", 348.0, top + 12), _word("Code", 366.9, top + 12),
         _word("ABCD0001234", 451.0, top + 12)],
    ]


def lines(rows: list[tuple[str, str, str, str, list[str]]] | None = None) -> list[list[dict]]:
    """The page: preamble, the one printed heading, then transactions.

    Each row is `(date, debit, credit, balance, description lines)`. The
    description is laid out the way this bank lays one out — first line above
    the dated one, the rest below — so a fixture row with five lines straddles
    its own transaction exactly as the real file does.
    """
    if rows is None:
        rows = _ROWS
    out: list[list[dict]] = list(_preamble(40.0))
    # The one heading that is text. It is not a header line by any measure —
    # one matched name where four are needed — and it repeats on every page.
    out.append([_word("Balance", 514.4, 500.5)])

    top = 538.4
    for date, debit, credit, balance, block in rows:
        for offset, text in zip(BLOCK_OFFSETS, block):
            out.append([_word(text, 138.0, top + offset)])
        out.append(
            [
                _date(date, 27.5, top),
                # The value date. Declared by nobody, and dropped — glued to the
                # transaction date it would read as no date format there is.
                _date(date, 82.5, top),
                _centred("-", REF_AT, top),
                _centred(debit, DEBIT_AT, top),
                _centred(credit, CREDIT_AT, top),
                _centred(balance, BALANCE_AT, top),
            ]
        )
        top += ROW_PITCH

    # Page furniture. It starts inside the description column — which is why a
    # wrapped line cannot be recognised by where it starts — and carries a page
    # number in the reference column, which no description ever does.
    out.append([_word("Page", 269.0, top), _word("No.:", 295.1, top),
                _word("1", 313.0, top)])
    # **Down the page, which is the order a reader gets them in.** The block
    # above a row is emitted with the row it belongs to and therefore out of
    # order; `pdfplumber` never is, and `_reattach` reads position rather than
    # coordinates because `top` restarts on every page.
    return sorted(out, key=lambda words: words[0]["top"])


#: Two rows with full five-line blocks and one with none. The narrow and wide
#: debits share a centre and not an edge, which is the banding this exists for.
_ROWS = [
    ("02/01/2026", "250.00", "-", "3,913.43",
     ["AAA BBB", "UPI/DR/512345678901/PAYEEA", "X/YESB/9990001111", "9990001111 AT",
      "04100 EXAMPLE"]),
    ("05/01/2026", "1,000.00", "-", "2,913.43",
     ["CCC DDD", "UPI/DR/512345678902/PAYEEB", "X/UTIB/9990002222", "9990002222 AT",
      "04100 EXAMPLE"]),
    ("09/01/2026", "-", "5,000.00", "7,913.43", []),
]
