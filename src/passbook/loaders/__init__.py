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


def load(path, password: str | None = None) -> tuple[StatementMeta, list[Transaction]]:
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
        # SPEC §44. **Most Indian banks hand out a password-protected PDF and
        # nothing else**, so this is the main path for every bank but one.
        #
        # A registered profile is tried FIRST, because it is a description the
        # operator wrote about their own file and outranks anything inferred.
        # `pdf.py` is Canara's own line-wrapping algorithm reconstructed
        # (§6.8.5) and stays the reader for Canara, where it is exact.
        from . import pdf

        # Resolve the stored password HERE, before either reader runs.
        #
        # `pdf.load` has always fallen back to `CANARA_PDF_PASSWORD` when given
        # none, and putting the profiled reader in front of it skipped that
        # fallback entirely — so an operator with the password in `.env` and a
        # bank profile registered was asked to type it. Caught by the one test
        # that walks the whole path: magic bytes -> loader -> settings.
        if password is None:
            from ..config import load_settings

            password = load_settings().canara_pdf_password or None

        refused: ParseError | None = None
        try:
            profiled, unchained = _profiled_pdf(path, password)
        except ParseError as exc:
            profiled, unchained, refused = None, None, exc
        if profiled is not None:
            return profiled

        try:
            # The only loader that can need a secret. Passed through rather than
            # read from settings here, so the upload path can prompt for it and
            # use it in memory without ever storing it (§30).
            return pdf.load(path, password)
        except ParseError:
            # Neither reader could read it, so the useful answer is whichever
            # complaint names something. **A profile's beats Canara's**: it
            # gives the bank, the row and what was missing (§54.3), where the
            # bespoke reader only says this is not a Canara statement, which the
            # operator already knew.
            if unchained is not None:
                # It read rows and they do not chain. Handed back so §6.6 can
                # say which row and by how much, downstream, where it always
                # does — a break named at sheet row 41 is a diagnosis and "no
                # profile matched" is not. Nothing is pushed either way.
                log.info("no reader parsed this cleanly; returning the unchained parse")
                return unchained
            if refused is not None:
                raise refused from None
            # Canara's reader is tried last, so its complaint is the one that
            # surfaces — and it said "this is not a Canara statement" on an
            # install that reads three banks, which reads as "only Canara is
            # supported" to somebody holding an SBI export.
            raise ParseError(_unreadable(path)) from None
    if kind == "xlsx":
        raise UnsupportedFormat(
            "file is a ZIP container (.xlsx). The Canara export is OLE2 .xls and "
            "this path has never been observed. Reading it needs openpyxl, which "
            "is deliberately not a dependency — xlrd 2.x cannot read .xlsx. "
            "If the bank has switched formats, say so and it gets wired up."
        )
    raise UnsupportedFormat(f"no loader is wired up for {kind!r}")


def _unreadable(path) -> str:
    """Why no reader could read this PDF, and what can actually be done.

    Names the banks this install reads, because the alternative — naming the
    one bank whose bespoke reader happened to run last — tells somebody with an
    SBI statement that passbook is a Canara tool.

    **And it does not promise that Add a bank will fix it.** That page maps
    column headings, so it works when the statement prints them. A statement
    that prints none cannot be described that way, and sending somebody there
    to name headings that do not exist is a loop, not a remedy. §51.
    """
    # `supported_banks`, not `known_banks`: the latter lists profiles, and
    # Canara has a bespoke reader rather than a profile — so the message that
    # exists to stop passbook looking Canara-only left Canara out of it.
    from ..config import supported_banks

    banks = ", ".join(supported_banks()) or "none"
    return (
        f"no reader could make sense of this PDF. This install reads: {banks}. "
        "If your bank is in that list, the file may be a different export from "
        "the same bank, or a print-to-file rather than the bank's own download. "
        "If it is not in the list, and your statement prints its column "
        "headings, Accounts \u2192 Add a bank can describe it. If it prints no "
        "headings \u2014 the columns are part of a background image \u2014 it "
        "cannot be described that way, and the layout has to be measured off "
        "the page instead."
    )


def _profiled_pdf(path, password: str | None):
    """Parse a PDF through a bank profile. §44, §99.

    Returns `(parsed, unchained)` — the statement a profile read, and, if none
    could be read cleanly, the best broken attempt for the caller to fall back
    on. Both None is an ordinary answer, not a failure: with no profiles
    registered — the single-account Canara install this project started as —
    every PDF takes the original path and behaves exactly as it did.

    **A profile that names its columns has already proved it found them**; four
    of its headings had to appear together on one line. A profile that declares
    *coordinates* (`columns_at`, §99) has proved nothing at all — it states
    where the columns sit and will band any statement into a table-shaped grid,
    whoever printed it. Measured: `sbi.yaml` read Canara's own PDF as 93
    transactions with correct narrations, correct dates, and 44 rows whose money
    had fallen down the gap between two declared ranges. `from_rows` accepted
    every one of them.

    So a declared-geometry profile has to show that the numbers it read **chain**
    — `balance[i-1] - debit + credit == balance[i]`, §6.6, the same invariant
    every import runs on. Nothing is softened by this and nothing is skipped:
    the chosen statement goes through `validate.check` afterwards exactly as
    before. The chain is used here to answer a different question — *is this
    that bank* — and it is the only honest answer available, because a layout
    that reads the wrong columns does not add up.
    """
    import pdfplumber

    from ._table import from_rows
    from .pdf import _decrypt, _lines
    from .pdf_table import to_grid
    from .profiles import ProfileError, load_profiles

    try:
        profiles = load_profiles()
    except ProfileError as exc:
        # Loud, and as a parse failure: a broken profile silently falling back
        # to the Canara reader is how someone's SBI statement gets read with the
        # wrong columns and still balances by luck.
        raise ParseError(str(exc)) from exc
    if not profiles:
        return None, None

    with pdfplumber.open(_decrypt(path, password)) as document:
        lines = [line for page in document.pages for line in _lines(page)]

    refused: ParseError | None = None
    unchained: tuple | None = None
    for profile in profiles:
        wanted = profile["columns"]
        order = ["date", "txn_id", "narration", "debit", "credit", "balance"]
        if profile["derive_txn_id"]:
            order.remove("txn_id")
        # `columns_at` is where this bank's columns ARE, for one that does not
        # print its headings as text (§99). Empty for every bank that does.
        grid = to_grid(lines, wanted, order, at=profile["columns_at"])
        if grid is None:
            continue
        log.info("pdf matched bank profile %r", profile["bank"])
        try:
            # This profile's own flag, not "does any profile derive" — with two
            # banks registered the global answer is right for at most one of
            # them.
            parsed = from_rows(grid, derive_txn_id=profile["derive_txn_id"])
        except ParseError as exc:
            # **Banding a file is not recognising it**, and a profile that
            # measured its columns (§99) claims a geometry rather than a set of
            # words — so it can band another bank's statement into something
            # shaped like a table and wrong. Before this, the first profile to
            # produce a grid ended the search: with profiles tried in filename
            # order, `sbi.yaml` reached a Union statement first and took the
            # whole import down with it.
            #
            # So a profile that cannot parse what it banded is not that bank,
            # and the next one gets a turn. The FIRST refusal is kept and
            # re-raised if none of them work, because it is the diagnosis an
            # operator with one registered bank needs — losing it would turn
            # every broken profile into a silent "no profile matched".
            log.info("profile %r banded the file but could not read it: %s",
                     profile["bank"], exc)
            if refused is None:
                refused = ParseError(f"{profile['bank']}: {exc}")
            continue

        if not profile["columns_at"] or _chains(*parsed):
            return parsed, None
        log.info(
            "profile %r banded %d row(s) but their balances do not chain — "
            "declared columns are not proof of a bank",
            profile["bank"],
            len(parsed[1]),
        )
        # Kept, in case nothing reads this file: a break §6.6 can name beats
        # "no profile matched".
        if unchained is None or len(parsed[1]) > len(unchained[1]):
            unchained = parsed

    if unchained is None and refused is not None:
        raise refused
    return None, unchained


def _chains(meta, transactions) -> bool:
    """Whether this reading of the file adds up. SPEC §6.6, asked as a question.

    `check_continuity` raises, which is right for an import and wrong for
    "is this that bank". Nothing here is softened: the statement this helps
    choose is checked again, by the same function, on the way in.
    """
    from ..validate import BalanceBreak, check_continuity

    try:
        check_continuity(meta, transactions)
    except BalanceBreak:
        return False
    return True


def read_grid(path, kind: str | None = None, password: str | None = None):
    """The file as a plain list of rows of strings, or None if unreadable here.

    The layer under every loader, exposed on its own for **`passbook inspect`
    and the Add-a-bank page** (§27, §34): both need to show the operator what a
    parser sees, including for a file no profile can parse yet. That is the only
    case they exist for.

    Validates nothing and stages nothing. Raises `PdfPasswordRequired` for an
    encrypted PDF rather than prompting — a reader that prompts is a reader only
    a terminal can use, and the browser needs to put a password field on screen
    instead. That distinction was a real bug: the web page reported "could not
    read it" for every locked PDF.
    """
    path = Path(path)
    kind = kind or sniff(path)

    if kind == "xls":
        import xlrd

        from .xls import _to_rows

        return _to_rows(xlrd.open_workbook(str(path)).sheet_by_index(0))

    if kind == "html_table":
        from .html_table import _to_rows as html_rows

        return html_rows(path.read_text(encoding="utf-8", errors="replace"))

    if kind == "delimited":
        import csv

        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            sample = handle.read(4096)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample)
            except csv.Error:
                dialect = csv.excel
            return [list(row) for row in csv.reader(handle, dialect)]

    if kind == "pdf":
        import pdfplumber

        from .pdf import _decrypt, _lines

        # Raises PdfPasswordRequired. Decryption is pikepdf, in this process —
        # never an online converter (non-negotiable 7).
        with pdfplumber.open(_decrypt(path, password)) as document:
            return [
                [str(word["text"]) for word in line]
                for page in document.pages
                for line in _lines(page)
            ]

    return None
