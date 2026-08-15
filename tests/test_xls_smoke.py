"""The real OLE2 path, driven by a committed redacted .xls. SPEC §6.2, D4.

The CSV and HTML fixtures exercise the shared `_table.from_rows` core but not
xlrd itself. This module closes that gap with `tests/fixtures/statement.xls`, a
genuine BIFF8 workbook written by `scripts/redact.py` (via the dev-only xlwt)
from a real statement. It runs everywhere, including a fresh clone and CI —
nothing here reads `inbox/`.
"""

from decimal import Decimal

import pytest
import xlrd

from conftest import FIXTURE_ACCOUNT, FIXTURE_SHEET_NAME, FIXTURE_TXN_COUNT, XLS_FIXTURE
from passbook.loaders import load, sniff
from passbook.validate import check_continuity


@pytest.fixture(scope="module")
def real():
    return load(XLS_FIXTURE)


def test_sniffer_identifies_the_canara_export_as_ole2():
    assert XLS_FIXTURE.read_bytes()[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    assert sniff(XLS_FIXTURE) == "xls"


def test_it_is_ole2_not_zip_which_is_why_openpyxl_cannot_read_it():
    """SPEC D4: openpyxl raising on this file is expected, not a bug to fix.

    The underlying reason is testable without openpyxl — .xlsx is a ZIP
    container and this is OLE2. When openpyxl happens to be installed the
    stronger assertion runs too, but it is not a dependency and its absence
    must not skip this test.
    """
    head = XLS_FIXTURE.read_bytes()[:4]
    assert head != b"PK\x03\x04"
    assert sniff(XLS_FIXTURE) != "xlsx"

    try:
        import openpyxl
    except ImportError:
        return  # not a dependency; the assertions above already hold
    with pytest.raises(Exception):
        openpyxl.load_workbook(XLS_FIXTURE)


def test_real_statement_parses_and_reconciles(real):
    meta, transactions = real
    assert len(transactions) == FIXTURE_TXN_COUNT
    assert check_continuity(meta, transactions) == meta.closing_balance


def test_every_cell_was_read_as_text_so_no_float_ever_appears(real):
    _meta, transactions = real
    for txn in transactions:
        for value in (txn.debit, txn.credit, txn.balance):
            assert value is None or isinstance(value, Decimal)


def test_empty_amount_side_is_none_not_zero(real):
    """The `' '` single-space cell, read through xlrd rather than csv."""
    _meta, transactions = real
    sheet = xlrd.open_workbook(str(XLS_FIXTURE)).sheet_by_index(0)
    spaces = sum(
        1
        for r in range(sheet.nrows)
        for c in (2, 3)
        if sheet.cell(r, c).value == " "
    )
    assert spaces == FIXTURE_TXN_COUNT, "the ' ' quirk did not survive into the fixture"
    assert all(t.debit is None or t.credit is None for t in transactions)
    assert all(t.debit != Decimal(0) and t.credit != Decimal(0) for t in transactions)


def test_the_xls_container_agrees_with_the_csv_container():
    """Same statement, three envelopes, identical result. SPEC D4."""
    from conftest import CSV_FIXTURE

    xls_meta, xls_txns = load(XLS_FIXTURE)
    csv_meta, csv_txns = load(CSV_FIXTURE)
    assert [t.txn_id for t in xls_txns] == [t.txn_id for t in csv_txns]
    assert [t.balance for t in xls_txns] == [t.balance for t in csv_txns]
    assert [t.narration for t in xls_txns] == [t.narration for t in csv_txns]
    assert xls_meta.opening_balance == csv_meta.opening_balance


# --- fixture hygiene: these run on every clone, with no source to compare to --


def test_sheet_name_is_redacted():
    """The real sheet name embeds the account number. SPEC §6.3, §11."""
    sheet = xlrd.open_workbook(str(XLS_FIXTURE)).sheet_by_index(0)
    assert sheet.name == FIXTURE_SHEET_NAME
    assert FIXTURE_ACCOUNT in sheet.name  # synthetic account, by construction


def test_fixture_contains_no_real_configured_account_number():
    """Whatever account this machine is configured for must not be in there."""
    from passbook.config import load_settings

    configured = (load_settings().passbook_account_number or "").strip()
    if not configured:
        return  # nothing configured on this machine; nothing to compare against
    assert configured != FIXTURE_ACCOUNT
    assert configured.encode() not in XLS_FIXTURE.read_bytes()
    assert configured not in XLS_FIXTURE.with_suffix(".csv").read_text()
