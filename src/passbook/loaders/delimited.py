"""Defensive fallback: delimited text via csv.Sniffer. SPEC §6.2.

Last resort when the file is neither OLE2, ZIP, HTML, nor PDF. Never fires on
the observed export.
"""

import csv
import logging

from ..models import StatementMeta, Transaction
from ._table import Rows, from_rows

log = logging.getLogger(__name__)


def load(path) -> tuple[StatementMeta, list[Transaction]]:
    text = open(path, encoding="utf-8", errors="replace").read()
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        log.info("delimited: sniffer undecided, assuming comma")
        dialect = csv.excel
    # skipinitialspace stays False: a lone ' ' in an amount column is meaningful
    # data here, not padding. SPEC §6.3.
    rows: Rows = [list(r) for r in csv.reader(text.splitlines(), dialect)]
    log.info("delimited: %d rows, delimiter %r", len(rows), dialect.delimiter)
    return from_rows(rows)
