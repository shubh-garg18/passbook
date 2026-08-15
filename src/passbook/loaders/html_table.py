"""Defensive fallback: an HTML table mislabelled `.xls`. SPEC §6.2.

A common Indian PSU bank pattern, and the most likely shape if Canara changes
its export backend. The observed export is genuine OLE2, so this path has never
fired against real data — it assumes the same row/column layout in a different
envelope, which is all D4 claims the sniffer is for.
"""

import logging
from html.parser import HTMLParser

from ..models import StatementMeta, Transaction
from ._table import Rows, from_rows

log = logging.getLogger(__name__)


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: Rows = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            # Deliberately no .strip() — the single-space empty amount cell must
            # survive to parse_amount, which is where emptiness is decided.
            self._row.append("".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def load(path) -> tuple[StatementMeta, list[Transaction]]:
    parser = _TableParser()
    parser.feed(open(path, encoding="utf-8", errors="replace").read())
    parser.close()
    log.info("html_table: %d rows", len(parser.rows))
    return from_rows(parser.rows)
