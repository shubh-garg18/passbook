"""Primary loader: genuine OLE2 .xls via xlrd 2.x. SPEC §6.2, D4.

`openpyxl` raises InvalidFileException on this file. That is correct behaviour,
not a bug to fix — xlrd 2.x reads only .xls, openpyxl reads only .xlsx.
"""

import logging

import xlrd

from ..models import StatementMeta, Transaction
from ._table import Rows, from_rows

log = logging.getLogger(__name__)


def _to_rows(sheet) -> Rows:
    """Every cell as str. No dtype inference anywhere. SPEC §6.3."""
    return [
        [str(sheet.cell(r, c).value) for c in range(sheet.ncols)]
        for r in range(sheet.nrows)
    ]


def load(path) -> tuple[StatementMeta, list[Transaction]]:
    book = xlrd.open_workbook(str(path))
    sheet = book.sheet_by_index(0)
    # The sheet name embeds the account number (SPEC §6.3, §11) — never logged.
    log.info("xls: sheet 0, %d rows x %d cols", sheet.nrows, sheet.ncols)
    return from_rows(_to_rows(sheet))
