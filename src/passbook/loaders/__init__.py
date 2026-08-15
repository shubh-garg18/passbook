"""Magic-byte sniffer and dispatch. SPEC §6.2.

Never trust the extension. Canara names its export `.xls`; SPEC D4 keeps this
sniffer because PSU banks do silently change export backends, and eight bytes
is a cheap guard against parsing an HTML table as a spreadsheet.
"""

import logging
from pathlib import Path

from ..models import StatementMeta, Transaction
from ._table import ParseError

log = logging.getLogger(__name__)

OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # genuine .xls — the observed case
ZIP = b"\x50\x4b\x03\x04"  # .xlsx
PDF = b"%PDF"


class UnsupportedFormat(ParseError):
    """Recognised the container, but no loader is wired up for it."""


def sniff(path) -> str:
    """Return a loader name from the first 8 bytes."""
    head = Path(path).open("rb").read(8)
    if head.startswith(OLE2):
        return "xls"
    if head.startswith(ZIP):
        return "xlsx"
    if head.startswith(PDF):
        return "pdf"
    if head.lstrip()[:1] == b"<":
        return "html_table"
    return "delimited"


def load(path) -> tuple[StatementMeta, list[Transaction]]:
    kind = sniff(path)
    log.info("selected loader: %s", kind)

    if kind == "xls":
        from . import xls

        return xls.load(path)
    if kind == "html_table":
        from . import html_table

        return html_table.load(path)
    if kind == "delimited":
        from . import delimited

        return delimited.load(path)
    if kind == "pdf":
        # SPEC D4: fallback, not replacement. XLS stays what `make sync`
        # expects; this runs for the weeks the bank hands over a PDF only.
        from . import pdf

        return pdf.load(path)
    if kind == "xlsx":
        raise UnsupportedFormat(
            "file is a ZIP container (.xlsx). The Canara export is OLE2 .xls and "
            "this path has never been observed. Reading it needs openpyxl, which "
            "is deliberately not a dependency — xlrd 2.x cannot read .xlsx. "
            "If the bank has switched formats, say so and it gets wired up."
        )
    raise UnsupportedFormat(f"no loader is wired up for {kind!r}")
