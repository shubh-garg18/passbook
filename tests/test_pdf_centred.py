"""A bank that centres its column headings. SPEC §51.

Reconstructed from a **shape dump**, not from a statement (§50). An operator hit
`row N: transaction has no balance` on a file nobody may read, and sent the page
as geometry and token shapes — `92-92-94` for a date, `A5` for a payee. The
fixture turns those back into words of the right length at the right
coordinates, with invented content: the geometry is measured, every character is
made up.

What it caught, all of which Canara had hidden by aligning each heading with its
own values:

* the **date values start 13pt left of the `Date` heading**;
* the **narration values start 45pt left of `Particulars`**;
* the **figures end 18pt right of `Balance`**;
* `Cr` arrives as its own token after the amount;
* an unmapped serial-number column sits left of everything.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from centred_headings import PROFILE, header_line, lines  # noqa: E402

from passbook.loaders import _table  # noqa: E402
from passbook.loaders._table import date_formats_that_parse, from_rows  # noqa: E402
from passbook.loaders.pdf_table import (  # noqa: E402
    _learn_columns,
    _split,
    find_header,
    to_grid,
)
from passbook.validate import BalanceBreak, check_continuity  # noqa: E402

ORDER = ["date", "narration", "debit", "credit", "balance"]


@pytest.fixture(scope="module")
def page():
    return lines()


def _parse(grid):
    """Parse with the date format the real path infers for itself."""
    dates = [row[0] for row in grid[header_line() + 1 :] if row and row[0].strip()]
    _table.DATE_FORMATS_OVERRIDE = date_formats_that_parse(dates)
    try:
        return from_rows(grid, derive_txn_id=True)
    finally:
        _table.DATE_FORMATS_OVERRIDE = None


# --- the header, past a details table ----------------------------------------


def test_the_details_table_does_not_win_the_header(page):
    """§50.1. The details block carries `Name`, `Balance` and `Date` — four
    mapped words between them — and used to be taken as the header."""
    at, columns, _headings = find_header(page, PROFILE)
    assert at == header_line()
    assert set(columns) == {"date", "narration", "debit", "credit", "balance"}


# --- the columns, learned from the data --------------------------------------


def test_every_column_is_found_where_its_values_are_not_where_its_heading_is(page):
    at, columns, _headings = find_header(page, PROFILE)
    text, money = _learn_columns(page[at + 1 :], columns, _split(columns))

    # Text: left edges, well left of their headings.
    assert text["date"] == 50.0 and columns["date"][0] == 63.5
    assert text["narration"] == 105.0 and columns["narration"][0] == 149.9

    # Money: right edges, right of their headings.
    assert money["debit"] == 395.0 and columns["debit"][1] == 382.3
    assert money["balance"] == 560.0 and columns["balance"][1] == 542.3
    # Deposit has too few figures to cluster, so its edge is estimated from the
    # displacement the others showed — and lands within tolerance of the truth.
    assert money["credit"] == pytest.approx(475.0, abs=10.0)


def test_nearest_first_matching_would_put_the_balances_in_deposits(page):
    """The assignment is chosen for **offset consistency**, not proximity.

    With clusters at 395 and 560 against headings at 382, 454 and 542,
    nearest-first gives 395 to the first and then hands 560 to the *second*,
    because that is the only one left. Every figure lands one column to the
    left, silently, and the balances turn up as deposits.
    """
    at, columns, _headings = find_header(page, PROFILE)
    _text, money = _learn_columns(page[at + 1 :], columns, _split(columns))

    assert money["credit"] != 560.0, "the balance cluster was handed to Deposit"
    assert money["balance"] == 560.0


def test_an_unmapped_serial_column_does_not_steal_the_date(page):
    """A cluster at x 35 competes with the real Date column at x 50 for a
    heading at 63.5. What separates them is that the date values lie **under**
    their heading and the serial numbers lie beside it."""
    at, columns, _headings = find_header(page, PROFILE)
    text, _money = _learn_columns(page[at + 1 :], columns, _split(columns))
    assert text["date"] == 50.0, "the serial-number column won the Date heading"


# --- the whole statement ------------------------------------------------------


def test_it_parses_and_the_balance_chain_holds(page):
    grid = to_grid(page, PROFILE, ORDER)
    meta, txns = _parse(grid)

    assert len(txns) == 6
    assert meta.account_number == "410250014782"
    assert meta.opening_balance == Decimal("10000.00")
    assert meta.closing_balance == Decimal("27588.73")
    check_continuity(meta, txns)


def test_the_serial_number_is_not_glued_to_the_date(page):
    """`1 01-08-2026` reads as no date format there is. Words left of every
    mapped column belong to a column nobody mapped and are dropped."""
    grid = to_grid(page, PROFILE, ORDER)
    for row in grid[header_line() + 1 :]:
        if row[0].strip():
            assert " " not in row[0].strip(), f"date cell carries extra: {row[0]!r}"


def test_a_detached_cr_does_not_land_in_the_narration(page):
    """`12,345.67 Cr` arrives as two words. A bare `Cr` is not a figure, so
    without this it drifts into the narration of every single row."""
    grid = to_grid(page, PROFILE, ORDER)
    narrations = [row[1] for row in grid[header_line() + 1 :] if row[1].strip()]
    assert narrations
    assert not any(n.strip().endswith("Cr") for n in narrations)

    _meta, txns = _parse(grid)
    assert all("Cr" not in (t.narration or "").split()[-1:] for t in txns)


def test_a_wrapped_narration_joins_the_row_above(page):
    grid = to_grid(page, PROFILE, ORDER)
    _meta, txns = _parse(grid)
    assert all("CONTINUED" in t.narration for t in txns)


def test_the_page_footer_is_not_a_row(page):
    grid = to_grid(page, PROFILE, ORDER)
    _meta, txns = _parse(grid)
    assert len(txns) == 6, "`Page 1 of 1` became a transaction"


def test_the_balance_chain_still_bites(page):
    """Non-negotiable 3. Everything above is inference — learned edges,
    estimated edges, an alignment chosen for consistency. This is the one thing
    that does not infer, and none of it may have softened it.

    Swapping the two headings in the *profile* does not test this: `from_rows`
    re-derives the mapping from the header text the grid carries, so it swaps
    them straight back. A figure that is simply wrong is unambiguous.
    """
    grid = [list(row) for row in to_grid(page, PROFILE, ORDER)]
    at = header_line()
    rows = [i for i in range(at + 1, len(grid)) if grid[i][0].strip()]
    grid[rows[2]][4] = "99999.99"  # a balance that does not follow

    meta, txns = _parse(grid)
    with pytest.raises(BalanceBreak) as caught:
        check_continuity(meta, txns)
    # And it names where, which is the whole point of failing loudly.
    assert "row" in str(caught.value).lower() or str(rows[2] - at) in str(caught.value)


# --- an unmapped column must never claim a heading. SPEC §53 -----------------


def test_a_serial_column_can_never_be_assigned_to_date():
    """The failure was `unparseable date '1'` — every date cell came out as
    `1 06-04-2024`, with the serial number glued on.

    Filtering candidate clusters by "overlaps **some** heading" was not enough.
    A serial-number column at x 30-40 overlaps nothing, but it survived the
    global filter on another anchor's behalf and was then handed to Date —
    whose heading, at 63.5-81.5, it is nowhere near. Overlap has to be checked
    for the pairing, not for the cluster.
    """
    from passbook.loaders.pdf_table import _match

    anchors = [
        (63.5, "date", (63.5, 81.5)),
        (149.9, "narration", (149.9, 190.1)),
    ]
    # The serial column, the real date column, the real narration column.
    spans = {35.5: (35.5, 40.0), 50.0: (50.0, 91.9), 105.0: (105.0, 200.0)}

    out = _match(anchors, list(spans), right_aligned=False, under=spans)
    assert out == {"date": 50.0, "narration": 105.0}

    # And the tempting wrong answer is genuinely tempting: (35.5, 105.0) has
    # offsets -28.0 and -44.9, a tighter spread than the correct (50.0, 105.0)
    # at -13.5 and -44.9. Consistency alone would pick it.
    assert abs(-28.0 - -44.9) < abs(-13.5 - -44.9)


def test_a_multi_page_statement_learns_from_its_rows_not_its_headers(page):
    """The header prints again at the top of every page, and its words sit at
    the HEADING positions — a cluster whose offset from the heading is zero.
    Nothing scores better than zero, so it learned the headings as the columns,
    which is the fallback wearing a disguise. One page of test data can never
    show it. §53."""
    import copy

    from passbook.loaders.pdf_table import _learn_columns, _split, find_header

    at, columns, _headings = find_header(page, PROFILE)
    one = _learn_columns(page[at + 1 :], columns, _split(columns))

    # Six pages, each repeating the header line.
    many = list(page)
    for _ in range(5):
        many += [copy.deepcopy(row) for row in page[at:]]

    at2, columns2, _h2 = find_header(many, PROFILE)
    learn_from = [
        words for words in many[at2 + 1 :] if find_header([words], PROFILE) is None
    ]
    six = _learn_columns(learn_from, columns2, _split(columns2))

    # The text columns must be identical — that is the regression. (The money
    # side legitimately improves: six pages give the sparse Deposit column
    # enough figures to cluster, so its edge is measured rather than estimated.)
    assert six[0] == one[0] == {"date": 50.0, "narration": 105.0}
    assert six[1]["credit"] == 475.0, "measured, not estimated, with enough rows"
    assert one[1]["credit"] == pytest.approx(475.0, abs=10.0), "estimated, in tolerance"


# --- errors that name their own remedy. SPEC §54.3 ---------------------------


def test_a_date_with_no_declared_format_says_which_one_would_work():
    """`unparseable date '06-04-2024'` is true and useless: the date is
    perfectly readable, it is the *profile* that does not say how to read it,
    and the remedy is a line in a YAML file the operator never opens."""
    from passbook.loaders import _table

    _table.DATE_FORMATS_OVERRIDE = []
    try:
        with pytest.raises(_table.ParseError) as caught:
            _table.parse_date("06-04-2024")
    finally:
        _table.DATE_FORMATS_OVERRIDE = None

    said = str(caught.value)
    assert "%d-%m-%Y" in said, "it should name the format that works"
    assert "Add a bank" in said, "and where to fix it"


def test_a_date_that_matches_no_declared_format_says_so_separately():
    from passbook.loaders import _table

    _table.DATE_FORMATS_OVERRIDE = ["%Y-%m-%d"]
    try:
        with pytest.raises(_table.ParseError, match="does not match the format"):
            _table.parse_date("06-04-2024")
    finally:
        _table.DATE_FORMATS_OVERRIDE = None


def test_something_that_is_not_a_date_at_all_stays_blunt():
    """No format would help, so none is offered."""
    from passbook.loaders import _table

    with pytest.raises(_table.ParseError, match="unparseable date"):
        _table.parse_date("Generated")


def test_skipping_every_row_reports_why_not_that_none_were_found(page):
    """§54 skips rows whose date will not parse — footer prose. When that
    swallows *every* row the reason is what matters, and "no transaction rows
    were found under it" hides it. Measured: 32 real rows skipped for a missing
    date format, and the message said nothing about dates."""
    from passbook.loaders import _table
    from passbook.loaders._table import ParseError, from_rows

    grid = to_grid(page, PROFILE, ORDER)
    _table.DATE_FORMATS_OVERRIDE = []  # no format declared, as an old profile
    try:
        with pytest.raises(ParseError) as caught:
            from_rows(grid, derive_txn_id=True)
    finally:
        _table.DATE_FORMATS_OVERRIDE = None

    said = str(caught.value)
    assert "could not be read as transactions" in said
    assert "%d-%m-%Y" in said, "the first row's reason has to survive"
