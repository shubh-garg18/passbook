"""A bank whose column headings are not text. SPEC §99.

`find_header` needs four matched column names on one line. SBI prints five of
its six headings as part of a background graphic, so there are not four words to
match and never will be — no spelling, no alias and no tolerance recovers a
header that is a picture.

So a profile may state the geometry instead: `columns_at`, an x-range per
column, measured off the operator's own page. What that buys and what it costs
are both tested here. It buys a bank nobody could read; it costs the one thing a
matched header gave for free, which is **evidence that this is that bank** —
tested in `test_a_declared_profile_does_not_claim_another_banks_file`.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from declared_columns import COLUMNS_AT, ORDER, PROFILE, lines  # noqa: E402

from passbook.loaders import _table  # noqa: E402
from passbook.loaders._table import from_rows  # noqa: E402
from passbook.loaders.pdf_table import to_grid  # noqa: E402
from passbook.validate import check_continuity  # noqa: E402


@pytest.fixture(scope="module")
def page():
    return lines()


@pytest.fixture
def grid(page):
    return to_grid(page, PROFILE, ORDER, at=COLUMNS_AT)


def _rows(grid):
    """The transactions: everything after the synthesised header row."""
    header = next(i for i, row in enumerate(grid) if row[0] == "date")
    return grid[header + 1 :]


# --- locating a table that announces itself with nothing ---------------------


def test_the_table_is_found_without_a_header_line(grid):
    assert grid is not None
    assert len(_rows(grid)) == 3


def test_the_synthesised_header_carries_the_profiles_own_words(grid):
    """`from_rows` re-runs `_find_header` over this grid and matches header
    TEXT. A row of passbook's field names would match nothing, so the header
    written here has to be the words the profile registered as aliases."""
    header = next(row for row in grid if row[0] == "date")
    assert header == ["date", "description", "debit", "credit", "balance"]


def test_the_preamble_above_the_table_is_still_read_as_label_and_value(grid):
    """The account number routes every row to a ledger and is never derived
    (§21.7). Nothing about declaring columns may cost the preamble."""
    assert ["Account Number", "410250014782"] in grid


def test_the_one_printed_heading_is_not_mistaken_for_a_row(grid):
    """`Balance` is the only word of the header that exists as text. It has no
    date and no figure, so it is furniture like a page number."""
    assert not any(row[ORDER.index("date")] == "" and "Balance" in row for row in _rows(grid))


# --- the banding -------------------------------------------------------------


def test_money_is_placed_by_its_centre_not_its_edge(grid):
    """This bank CENTRES its figures. `250.00` ends at 379.7 and `1,000.00` at
    383.1 in the same column, so `MONEY_ALIGN_TOLERANCE` — which is a right-edge
    test, and right for a bank that right-aligns — rejects one of them."""
    rows = _rows(grid)
    # Normalised on the way out — `_amount_text` strips the separators — so the
    # figure is what matters and not the punctuation it arrived with.
    assert rows[0][ORDER.index("debit")] == "250.00"
    assert Decimal(rows[1][ORDER.index("debit")]) == Decimal("1000.00")
    assert rows[1][ORDER.index("balance")] != "", "the wide figure lost its column"


def test_the_dash_placeholder_does_not_become_narration():
    """An empty money cell printed `-` is empty, not a word. Left in, it
    prefixes every description with the placeholders of every blank column."""
    rows = _rows(to_grid(lines(), PROFILE, ORDER, at=COLUMNS_AT))
    assert rows[0][ORDER.index("credit")] == ""
    assert "-" not in rows[2][ORDER.index("narration")]


def test_the_undeclared_value_date_column_is_dropped(grid):
    """This bank prints a value date beside the transaction date, and it is not
    in `columns_at`. Folded into the date column instead it would read
    `02/01/2026 02/01/2026`, which no date format parses."""
    for row in _rows(grid):
        assert row[ORDER.index("date")].count("/") == 2


def test_the_page_footer_is_not_a_wrapped_description(grid):
    """It starts at x 269, INSIDE the declared description column — so "does it
    start under the narration" cannot reject it. What rejects it is that it also
    puts a page number in the reference column, and a description never does."""
    assert not any("Page" in row[ORDER.index("narration")] for row in _rows(grid))


# --- the description block, which straddles its own row ----------------------


def test_the_line_above_the_first_row_is_not_lost_to_the_preamble(grid):
    """A block begins above its own dated line, so the first transaction's
    opening words sit above the row that locates the table. Filed as preamble
    they are gone, silently, and only for the first payee."""
    assert _rows(grid)[0][ORDER.index("narration")].startswith("AAA BBB")


def test_the_tail_of_a_block_stays_on_its_own_row(grid):
    """The regression §99 exists for. This bank's block runs 30pt below its row
    and the next row is 50pt down, so the last two lines of every description
    are NEARER the following transaction than their own — and nearest-first,
    which is what §96 left behind, files them there.

    Invisible to the balance chain: §6.6 checks amounts, and every amount is
    right either way.
    """
    rows = _rows(grid)
    assert "04100 EXAMPLE" in rows[0][ORDER.index("narration")]
    assert "PAYEEB" not in rows[0][ORDER.index("narration")]
    assert "PAYEEA" not in rows[1][ORDER.index("narration")]


def test_a_row_with_no_description_collects_nobody_elses(grid):
    assert _rows(grid)[2][ORDER.index("narration")] == ""


# --- and the whole statement -------------------------------------------------


def test_it_parses_and_the_balance_chain_holds(grid, monkeypatch):
    monkeypatch.setattr(_table, "DATE_FORMATS_OVERRIDE", ["%d/%m/%Y"])
    meta, txns = from_rows(grid, derive_txn_id=True)

    assert len(txns) == 3
    assert meta.account_number == "410250014782"
    assert txns[0].debit == Decimal("250.00") and txns[0].credit is None
    assert txns[2].credit == Decimal("5000.00")
    check_continuity(meta, txns)


def test_every_id_is_derived_and_distinct(grid, monkeypatch):
    """The reference column is `-` on every row, so there is nothing to key on.
    §44.4 hashes the whole row, and the running balance is what keeps two
    identical payments apart."""
    monkeypatch.setattr(_table, "DATE_FORMATS_OVERRIDE", ["%d/%m/%Y"])
    _meta, txns = from_rows(grid, derive_txn_id=True)
    ids = [t.txn_id for t in txns]
    assert all(i.startswith("d-") for i in ids)
    assert len(set(ids)) == len(ids)


# --- what a declared layout cannot prove -------------------------------------


def test_a_declared_profile_does_not_claim_another_banks_file():
    """**Declaring coordinates is not recognising a bank.** A header-matched
    profile had to find four of its own column names together on one line; a
    declared one states where the columns sit and will band any statement into
    a table-shaped grid, whoever printed it.

    Measured, and this is the file it was measured on: the shipped `sbi` profile
    read Canara's PDF as 93 transactions with correct narrations, correct dates,
    and 44 rows whose money had fallen down the gap between two declared ranges.
    `from_rows` accepted every one of them and the failure surfaced two layers
    later as a a rejected write.

    The balance chain is what says no, and using it to answer *is this that
    bank* softens nothing: the statement that wins is checked again on the way
    in, by the same function.
    """
    from passbook.loaders import load, profiles

    fixture = Path(__file__).parent / "fixtures" / "statement.pdf"
    saved = profiles.PROFILES_DIR
    profiles.PROFILES_DIR = Path("/nonexistent")  # shipped profiles only
    try:
        meta, txns = load(fixture, "1111")
    finally:
        profiles.PROFILES_DIR = saved

    assert len(txns) == 93
    # Canara's own reader, not a profile: it prints a per-row reference and
    # every id here is that reference rather than a hash of the row.
    assert not any(t.txn_id.startswith("d-") for t in txns)
    check_continuity(meta, txns)
