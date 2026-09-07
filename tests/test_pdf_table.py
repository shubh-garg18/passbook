"""Any bank's PDF, banded from its header. SPEC §44.

**Most Indian banks hand out a password-protected PDF and nothing else**, which
makes this the main path rather than a fallback. `config/banks/*.yaml` could
describe any bank's columns and none of it reached a PDF, because `pdf.py` is a
reconstruction of *Canara's* line-wrapping algorithm and reads nothing else.

The hard part of testing this is honest evidence without another bank's file.
There is one: **the generic reader must reproduce the bespoke one, exactly, on
the PDF we have.** `pdf.py` was built against a real 3-month export and its
output is checked against the XLS elsewhere, so agreeing with it row for row is
a real result and not a tautology — the two share no code below `_lines`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from passbook.loaders import pdf
from passbook.loaders._table import from_rows
from passbook.loaders.pdf_table import find_header, to_grid

FIXTURES = Path(__file__).parent / "fixtures"
PDF_FIXTURE = FIXTURES / "statement.pdf"
PASSWORD = "1111"  # last four of FIXTURE_ACCOUNT, per §6.8.1

#: Canara's own layout, written the way an operator would write it on the
#: Add-a-bank page. Nothing here is imported from the Canara reader.
CANARA_AS_A_PROFILE = {
    "date": "date",
    "particulars": "narration",
    "withdrawals": "debit",
    "deposits": "credit",
    "balance": "balance",
}
ORDER = ["date", "narration", "debit", "credit", "balance"]


def _lines():
    import pdfplumber

    from passbook.loaders.pdf import _decrypt, _lines as lines_of

    with pdfplumber.open(_decrypt(PDF_FIXTURE, PASSWORD)) as document:
        return [line for page in document.pages for line in lines_of(page)]


@pytest.fixture(scope="module")
def lines():
    return _lines()


@pytest.fixture(autouse=True)
def derived_ids(tmp_path, monkeypatch):
    """Canara's layout registered as if it were another bank — including the
    part that matters most here: **no reference column**, which is the shape
    Union Bank and most others actually have (§44)."""
    from passbook.loaders import profiles

    directory = tmp_path / "banks"
    directory.mkdir()
    (directory / "other.yaml").write_text(
        "bank: other\n"
        "derive_txn_id: true\n"
        "columns:\n"
        '  "Date": date\n'
        '  "Particulars": narration\n'
        '  "Withdrawals": debit\n'
        '  "Deposits": credit\n'
        '  "Balance": balance\n'
        # The PDF prints `09-05-2026` where the spreadsheet prints
        # `09-MAY-2026`. A profile declares its bank's date format, and this is
        # the first thing that exercises that on a PDF.
        "dates: ['%d-%m-%Y']\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "PROFILES_DIR", directory)


# --- finding the header ------------------------------------------------------


def test_the_header_is_found_by_its_column_names(lines):
    found = find_header(lines, CANARA_AS_A_PROFILE)
    assert found is not None
    _index, columns, headings = found
    assert headings["debit"] == "Withdrawals", "the bank's own word, for the grid header"
    assert set(columns) == {"date", "narration", "debit", "credit", "balance"}
    # Right-aligned money, left-aligned text: the whole basis of the banding.
    assert columns["date"][0] < columns["narration"][0] < columns["debit"][0]


def test_a_profile_for_another_bank_simply_does_not_match(lines):
    """None is an ordinary answer. A caller may be trying several profiles, and
    "this is not that bank" is not a failure."""
    assert find_header(lines, {"txndate": "date", "particular": "narration"}) is None


def test_a_multi_word_heading_matches_longest_first(lines):
    """`Withdrawal` must not win where `Withdrawal Amt` is meant, or the column
    would be found at the wrong x-range and every figure in it mis-banded.

    Keys are normalised the way a profile stores them: alphanumerics only, so
    `Date Particulars` is `dateparticulars`.
    """
    found = find_header(lines, {**CANARA_AS_A_PROFILE, "dateparticulars": "txn_id"})
    assert found is not None
    _index, columns, _headings = found
    # The two-word match consumed both words, so `date` alone never matched.
    assert "txn_id" in columns
    assert "date" not in columns


# --- the banding itself ------------------------------------------------------


def test_the_generic_reader_reproduces_the_bespoke_one_exactly(lines):
    """The evidence. Two readers, no shared code below `_lines`, same rows.

    `pdf.py` recovers Canara's line wrapping from the character stream; this one
    knows nothing about Canara and only reads x-ranges off the header. If they
    agree on 93 transactions, the banding is right.
    """
    _bespoke_meta, bespoke = pdf.load(PDF_FIXTURE, PASSWORD)

    grid = to_grid(lines, CANARA_AS_A_PROFILE, ORDER)
    assert grid is not None
    generic_meta, generic = from_rows(grid)

    assert len(generic) == len(bespoke) == 93

    for mine, theirs in zip(generic, bespoke):
        assert mine.txn_date == theirs.txn_date
        assert mine.debit == theirs.debit
        assert mine.credit == theirs.credit
        assert mine.balance == theirs.balance

    assert generic_meta.opening_balance == _bespoke_meta.opening_balance
    assert generic_meta.closing_balance == _bespoke_meta.closing_balance


def test_the_banded_statement_passes_the_continuity_check(lines):
    """Non-negotiable 3, and the only thing that says the bands were right.

    A column mapped one place left produces a grid that parses and does not add
    up — which is exactly what this check exists to catch, and why nothing in
    the banding needs to be trusted on its own.
    """
    from passbook.validate import check_continuity

    meta, txns = from_rows(to_grid(lines, CANARA_AS_A_PROFILE, ORDER))
    check_continuity(meta, txns)


def test_a_wrapped_narration_joins_the_row_above_rather_than_making_one(lines):
    """The bank wraps one transaction over three printed lines. A continuation
    has no date; treating it as a row would triple the count and break the
    chain."""
    grid = to_grid(lines, CANARA_AS_A_PROFILE, ORDER)
    _meta, txns = from_rows(grid)

    # The fixture's first transaction is wrapped over three lines (§6.8.5).
    assert len(txns) == 93
    assert "UPI" in txns[0].narration
    assert len(txns[0].narration) > 40, "the wrapped tail was dropped"


def test_the_trailing_timestamp_stays_in_the_narration(lines):
    """The bug a midpoint rule would reintroduce.

    `01:51:33` sits at x 236.8-275.7 — its midpoint is past the
    Particulars/Withdrawals midpoint, so a midpoint rule hands a time to
    `parse_amount`. By where it STARTS, it is narration. Measured, not assumed.
    """
    grid = to_grid(lines, CANARA_AS_A_PROFILE, ORDER)
    _meta, txns = from_rows(grid)

    timestamped = [t for t in txns if ":" in t.narration]
    assert timestamped, "the fixture's UPI rows carry a trailing timestamp"
    # And none of them lost their amount to it.
    assert all(t.debit is not None or t.credit is not None for t in timestamped)


def test_a_label_printed_under_a_money_column_is_not_read_as_money(lines):
    """`Opening Balance` sits under Withdrawals on this bank's paper. Banded by
    position alone it would reach `parse_amount`; a word in a money column that
    is not money goes to the narration cell instead — which is also what lets
    the sentinel row survive the trip and be found."""
    grid = to_grid(lines, CANARA_AS_A_PROFILE, ORDER)
    joined = [" ".join(row).lower() for row in grid]
    assert any("opening balance" in row for row in joined)

    meta, _txns = from_rows(grid)
    # Found as a sentinel, not derived — which is the stronger of the two.
    from decimal import Decimal

    assert meta.opening_balance == Decimal("10000.00")


def test_the_preamble_is_split_into_cells_so_metadata_can_be_found(lines):
    """A preamble does not follow the table's columns, so those lines are split
    on wide gaps instead. `_find_metadata` reads label/value out of the first
    cells of a row, and without this the account number is unfindable."""
    grid = to_grid(lines, CANARA_AS_A_PROFILE, ORDER)
    meta, _txns = from_rows(grid)
    assert meta.account_number == "999900001111"


# --- the column edge is learned, not assumed. SPEC §48 -----------------------


def test_the_money_edges_are_learned_from_the_figures(lines):
    """The heading's right edge is not the column's.

    They coincide on this fixture — `Withdrawals` ends at 417.0 and its amounts
    at 418.0 — and that is luck, not a property of statements. A bank that
    centres its headings has every figure land outside every column, the balance
    cell comes out empty, and the import fails with `row N: transaction has no
    balance` while the number is plainly on the page. That is a real report.
    """
    from passbook.loaders.pdf_table import _learn_columns, _split, find_header

    at, columns, _headings = find_header(lines, CANARA_AS_A_PROFILE)
    _text, money = _learn_columns(lines[at + 1 :], columns, _split(columns))

    # On the figures, not the words — and exactly, because a right-aligned
    # column's amounts share an edge.
    assert money == {"debit": 418.0, "credit": 494.0, "balance": 573.0}


def test_the_text_edges_are_learned_too(lines):
    """§51. Canara left-aligns its text headings with their values, so this
    agrees with the headings — which is exactly why the bug was invisible here
    and fatal on a bank that centres them."""
    from passbook.loaders.pdf_table import _learn_columns, _split, find_header

    at, columns, _headings = find_header(lines, CANARA_AS_A_PROFILE)
    text, _money = _learn_columns(lines[at + 1 :], columns, _split(columns))

    assert set(text) == {"date", "narration"}
    assert text["date"] == pytest.approx(columns["date"][0], abs=2.0)
    assert text["narration"] == pytest.approx(columns["narration"][0], abs=2.0)


def test_page_furniture_does_not_become_a_column(lines):
    """`Page 1 of 6` is amount-shaped: a `1` and a `6`, on every page. Six pages
    of that must not turn into two extra money columns."""
    from passbook.loaders.pdf_table import _learn_columns, _split, find_header

    at, columns, _headings = find_header(lines, CANARA_AS_A_PROFILE)
    _text, money = _learn_columns(lines[at + 1 :], columns, _split(columns))

    assert money == {"debit": 418.0, "credit": 494.0, "balance": 573.0}
    assert 525.2 not in money.values() and 538.8 not in money.values()


# --- Cr / Dr, which is how Indian statements print a balance -----------------


def test_a_cr_suffix_is_a_figure_and_dr_is_a_negative_one():
    """`12,345.67 Cr` is not a number and `parse_amount` rightly refuses it.
    Nearly every Indian statement prints one, and refusing the whole import over
    a two-letter suffix would be the wrong place to be strict."""
    from decimal import Decimal

    from passbook.loaders.pdf_table import _amount_text

    assert _amount_text("12,345.67") == "12345.67"
    assert _amount_text("12,345.67Cr") == "12345.67"
    assert _amount_text("12,345.67 CR") == "12345.67"
    # Overdrawn. If that reading is ever wrong for some bank, the balance chain
    # says so immediately, with a row index.
    assert Decimal(_amount_text("500.00 Dr")) == Decimal("-500.00")

    # And it is still a question, not an assertion: nothing else is a figure.
    assert _amount_text("Opening") is None
    assert _amount_text("") is None
    assert _amount_text("UPI/DR/1234") is None


def test_a_narration_containing_dr_is_not_read_as_money():
    """`UPI/DR/...` is on almost every row of this fixture. Suffix matching that
    fired on it would turn a payee into a negative balance."""
    from passbook.loaders.pdf_table import _amount_text

    assert _amount_text("UPI/DR/194892411578/ZEPKV") is None
    assert _amount_text("DR") is None, "a bare suffix carries no figure"


# --- sharing the shape, not the statement. SPEC §50 --------------------------


def test_a_shape_keeps_the_structure_and_throws_away_the_content():
    """The operator offered their statement to get a bug fixed and did not want
    to. That should not be the price of a bug report."""
    from passbook.loaders.pdf_table import shape

    # Structure survives: two dates in the same format share a shape.
    assert shape("09-05-2026") == shape("25-08-2026") == "92-92-94"
    # And two formats do not.
    assert shape("09/05/2026") != shape("09-05-2026")
    assert shape("1,418.91") == shape("5,978.53") == "9,93.92"


def test_a_shape_cannot_be_read_back():
    """The property is **indistinguishability**, not the absence of characters.

    A shape is `A5` or `912` — a marker and a run length — so digits appear in
    it whatever the source was. Asserting "no source character survives" is the
    wrong test and passes or fails by luck: `912` shares a `1` with the account
    number that produced it and shares it with every other twelve-digit number
    too. What matters is that it shares it with every other one.
    """
    from passbook.loaders.pdf_table import shape

    same_shape = [
        ("999900001111", "410250014782", "000000000001"),
        ("UPI/DR/194892411578/ZEPKV", "UPI/CR/883012455901/WUBQX"),
        ("12,345.67", "99,999.99", "10,000.00"),
        ("CNRB0009999", "SBIN0004321"),
    ]
    for group in same_shape:
        shapes = {shape(value) for value in group}
        assert len(shapes) == 1, f"these should be indistinguishable: {group} -> {shapes}"

    # And a genuinely different structure is still different, or the dump would
    # be useless for debugging.
    assert shape("09-05-2026") != shape("09/05/2026") != shape("2026-05-09")


def test_describe_carries_geometry_and_nothing_else(lines):
    """A banding failure is made of geometry, so geometry is what has to travel."""
    from passbook.loaders.pdf_table import describe

    out = describe(lines, 12)
    assert len(out) == 12
    assert set(out[0]["words"][0]) == {"shape", "x0", "x1"}
    assert "Statement" not in str(out), "no source text, anywhere"
    assert "999900001111" not in str(out)


# --- two tables, which is how most banks print. SPEC §50.1 -------------------


def test_the_best_header_line_wins_not_the_first(lines):
    """Many bank PDFs open with a details table — name, address, account number,
    IFSC — and start the transactions in a second table below it.

    A details line carrying four of the mapped words would have been taken as
    the header, every band computed from the wrong place, and every transaction
    below it read as empty. That is `row N: transaction has no balance` on a
    statement whose balance is plainly printed.
    """
    from passbook.loaders.pdf_table import find_header

    real_at, real_cols, _ = find_header(lines, CANARA_AS_A_PROFILE)

    # A profile that also matches four words up in the details block: `Client`,
    # `Name`, `Address` and `Phone` all sit above the real header.
    decoyed = {
        **CANARA_AS_A_PROFILE,
        "client": "txn_id",
        "name": "narration",
        "address": "debit",
        "phone": "credit",
    }
    at, cols, _ = find_header(lines, decoyed)

    assert len(cols) >= len(real_cols)
    assert at == real_at, "a details line won the header"
