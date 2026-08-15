"""Format sniffing and grid parsing. SPEC §6.2, §6.3, §10."""

from datetime import date
from decimal import Decimal

import pytest

from conftest import CSV_FIXTURE, FIXTURE_ACCOUNT, FIXTURE_TXN_COUNT, HTML_FIXTURE
from passbook.loaders import UnsupportedFormat, load, sniff
from passbook.loaders._table import ParseError, from_rows, parse_amount, parse_date

# --- the single most likely silent bug ---------------------------------------


def test_empty_amount_cell_is_a_space_and_parses_as_none():
    """SPEC §6.3: the empty amount cell contains `' '`, not `''`.

    If emptiness is tested before stripping, this returns Decimal('0') and
    every one-sided row silently becomes a zero-value double entry.
    """
    assert parse_amount(" ") is None
    assert parse_amount("") is None
    assert parse_amount("   ") is None
    assert parse_amount(" ") is not Decimal("0")


def test_amounts_keep_comma_separators_and_never_become_floats():
    assert parse_amount("10,000.00") == Decimal("10000.00")
    assert isinstance(parse_amount("10,000.00"), Decimal)
    # 0.1 + 0.2 famously != 0.3 in binary float; Decimal must be exact.
    assert parse_amount("0.10") + parse_amount("0.20") == Decimal("0.30")


def test_unparseable_amount_raises_rather_than_guessing():
    with pytest.raises(ParseError):
        parse_amount("twelve rupees")


# --- dates --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("09-MAY-2026", date(2026, 5, 9)),  # transaction rows: uppercase
        ("07-May-2026", date(2026, 5, 7)),  # the period line: title case
        ("7-may-2026", date(2026, 5, 7)),
    ],
)
def test_date_parsing_is_case_insensitive_and_locale_free(text, expected):
    assert parse_date(text) == expected


def test_unknown_month_raises():
    with pytest.raises(ParseError):
        parse_date("09-XXX-2026")


# --- sniffing -----------------------------------------------------------------


def test_sniffer_detects_ole2_on_the_real_container(tmp_path):
    f = tmp_path / "misnamed.txt"
    f.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)
    assert sniff(f) == "xls"  # extension is never trusted


@pytest.mark.parametrize(
    "head,expected",
    [
        (b"PK\x03\x04rest", "xlsx"),
        (b"%PDF-1.7 rest", "pdf"),
        (b"<html><table>", "html_table"),
        (b"  <table>", "html_table"),
        (b"Date,Txn,Amt\n", "delimited"),
    ],
)
def test_sniffer_dispatch(tmp_path, head, expected):
    f = tmp_path / "x.xls"
    f.write_bytes(head)
    assert sniff(f) == expected


def test_xlsx_refuses_clearly_rather_than_misparsing(tmp_path):
    """xlsx is still unwired: xlrd 2.x cannot read it and openpyxl is
    deliberately not a dependency. PDF used to be refused here too — Phase 12
    built that loader, so it now parses instead (SPEC §6.8)."""
    f = tmp_path / "x.xls"
    f.write_bytes(b"PK\x03\x04rest")
    with pytest.raises(UnsupportedFormat):
        load(f)


def test_a_pdf_is_routed_to_the_pdf_loader_not_refused(tmp_path):
    """Dispatch only — a truncated PDF still fails, but as a PDF parse
    failure rather than 'no loader wired up'."""
    from passbook.loaders import sniff

    f = tmp_path / "x.xls"
    f.write_bytes(b"%PDF-1.7\ntruncated")
    assert sniff(f) == "pdf"
    with pytest.raises(Exception) as exc:
        load(f)
    assert "Phase 6" not in str(exc.value)


# --- the grid -----------------------------------------------------------------


def test_fixture_loads_the_expected_shape(parsed):
    meta, transactions = parsed
    assert len(transactions) == FIXTURE_TXN_COUNT
    assert meta.account_number == FIXTURE_ACCOUNT
    assert meta.masked_account == "****1111"
    assert meta.period_from == date(2026, 5, 7)
    assert meta.period_to == date(2026, 8, 7)


def test_every_row_has_exactly_one_of_debit_credit(parsed):
    _meta, transactions = parsed
    for txn in transactions:
        assert (txn.debit is None) != (txn.credit is None)
    # and the empty side really is None, not zero
    assert any(t.debit is None for t in transactions)
    assert all(t.debit != Decimal(0) for t in transactions)


def test_sentinel_rows_are_not_transactions(parsed):
    meta, transactions = parsed
    narrations = " ".join(t.narration for t in transactions).lower()
    assert "opening balance" not in narrations
    assert "closing balance" not in narrations
    assert meta.opening_balance == Decimal("10000.00")


def test_header_is_found_by_scanning_not_by_row_number(rows):
    """The header is at row 9 in the real file, but nothing may hardcode that."""
    shifted = [[""] * 6, [""] * 6] + rows  # push everything down two rows
    meta, transactions = from_rows(shifted)
    assert len(transactions) == FIXTURE_TXN_COUNT
    assert meta.account_number == FIXTURE_ACCOUNT


def test_the_banks_misspelling_is_matched(rows):
    header = next(r for r in rows if "Trasnaction ID" in r)
    assert "Trasnaction ID" in header  # sic — do not correct it
    assert "Remarks" in header  # not Particulars/Description/Narration


def test_corrected_spelling_would_also_work(rows):
    fixed = [[c.replace("Trasnaction", "Transaction") for c in row] for row in rows]
    _meta, transactions = from_rows(fixed)
    assert len(transactions) == FIXTURE_TXN_COUNT


def test_columns_are_mapped_by_header_text_not_position(rows):
    """Swap two columns; parsing must follow the header, not the index."""
    swapped = [list(r) for r in rows]
    for row in swapped:
        if len(row) >= 6:
            row[2], row[5] = row[5], row[2]  # Withdrawals <-> Remarks
    _meta, transactions = from_rows(swapped)
    assert len(transactions) == FIXTURE_TXN_COUNT
    assert any(t.debit is not None for t in transactions)


def test_missing_required_column_is_a_clear_error(rows):
    stripped = [[c for i, c in enumerate(row) if i != 4] for row in rows]  # drop Balance
    with pytest.raises(ParseError, match="balance"):
        from_rows(stripped)


def test_html_container_yields_the_same_result_as_csv():
    """Different envelope, same layout — SPEC D4's whole point."""
    csv_meta, csv_txns = load(CSV_FIXTURE)
    html_meta, html_txns = load(HTML_FIXTURE)
    assert len(html_txns) == len(csv_txns) == FIXTURE_TXN_COUNT
    assert html_meta.opening_balance == csv_meta.opening_balance
    assert [t.txn_id for t in html_txns] == [t.txn_id for t in csv_txns]
    assert [t.balance for t in html_txns] == [t.balance for t in csv_txns]
