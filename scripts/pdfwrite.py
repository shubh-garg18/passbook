"""Render a redacted statement grid as a Canara-shaped, encrypted PDF fixture.

SPEC §6.8, §11. Used only by `scripts/redact.py`; nothing here ships in the
package.

**Why a renderer and not a rewrite of the real file.** The XLS fixture
regenerates every amount and recomputes the balance chain (§11), so a PDF
fixture built by string-substituting the real export would disagree with it on
every figure — and `tests/test_pdf_matches_xls.py` exists precisely to compare
the two. Both fixtures therefore come from the *same* redacted grid: one
container each, identical content by construction.

**The layout is measured from the real export, not invented.** Page geometry,
column positions, the 11.64pt line step, the right edges the money columns are
aligned to, the 1pt lift on the Branch Code value: all read off
`Acnt_stmt__07052026_07082026.pdf` (Helvetica, base-14, one `BT/Tm/Tj/ET` per
run, no embedded widths). Font metrics come from pdfminer's own AFM table,
which is the table pdfplumber will measure the fixture back with.

**The line breaking is verified, not modelled.** `wrap()` reproduces all 193
line breaks across the 93 rows of the reference statement byte for byte — see
`WRAP_EVIDENCE`. That is what makes the fixture a real test of §6.4's
continuation rule rather than a test of a guess about it: the loader has to
invert the bank's actual algorithm, including the whitespace it discards.

What is *not* reproduced, deliberately:

  * **The Canara logo.** It is an image XObject carrying bank branding and
    nothing the parser reads.
  * **Exact vertical rhythm.** The real renderer's row pitch is not a function
    of the number of lines it drew (3-line rows appear at both 40pt and 52pt
    pitch), so it cannot be reproduced from the drawn output. Row pitch has no
    effect on parsing — the loader groups characters into lines and orders them
    by `top` — so the fixture uses a simple, honest rule and says so here.
"""

from __future__ import annotations

import io
import re

from pathlib import Path

Rows = list[list[str]]

# ── page geometry, all measured from the real export ────────────────────────
PAGE_WIDTH, PAGE_HEIGHT = 595, 842

X_DATE = 23.0
X_NARRATION = 85.0
# Header word origins. `loaders/pdf._bounds` derives every column boundary from
# these plus the rendered width of the header words, so placing them here is
# what puts the fixture's columns where the loader expects them.
HEADERS = (
    ("Date", 23.0),
    ("Particulars", 85.0),
    ("Withdrawals", 362.55),
    ("Deposits", 455.10),
    ("Balance", 536.87),
)
# The money columns are right-aligned; these are the edges they align to.
RIGHT_DEBIT = 418.0
RIGHT_CREDIT = 494.0
RIGHT_BALANCE = 573.0
# An absent amount is drawn as an empty string at a fixed origin. Kept because
# the real file does it; it contributes no characters either way.
X_EMPTY_DEBIT = 340.0
X_EMPTY_CREDIT = 419.0

Y_TITLE = 738.86
TITLE_SIZE = 12.0
BODY_SIZE = 10.0
FOOTER_SIZE = 7.0

Y_HEADER_FIRST = 546.72   # page 1, below the metadata block
Y_HEADER_CONT = 809.72    # pages 2..n
Y_OPENING = 524.72
Y_ROW_FIRST = 503.72
Y_ROW_CONT = 783.72
Y_CLOSING = 119.72
X_OPENING_LABEL = 379.15
X_CLOSING_LABEL = 381.38
Y_FOOTER = 33.5
X_FOOTER = 503.03
X_FOOTER_COUNT = 533.0

LINE_STEP = 11.64        # between the wrapped lines of one narration
ROW_GAP = 16.72          # from a row's last line to the next row's first
ROW_GAP_SINGLE = 29.0    # measured: an unwrapped row is given more air
Y_FLOOR = 50.0           # a row must fit entirely above this

# The narration column is 254pt wide: x 85 to x 339. Every one of the 193 line
# breaks in the reference statement falls exactly where this budget and the
# rule in `wrap()` put it.
NARRATION_WIDTH = 254.0

WRAP_EVIDENCE = """\
Measured against inbox/Acnt_stmt__07052026_07082026.pdf, 93 rows / 193 breaks:
budget 254.0 reproduces 93/93 rows fragment-for-fragment; 254.5 gives 92/93.
Break opportunities are a space (the whole run is discarded) and a hyphen (kept
on the line if it fits, carried to the next line if it does not). Where the
fitted prefix contains no opportunity the break is mid-token and consumes
nothing, which is the case §6.4's continuation rule cannot recover.\
"""


def _advance_widths() -> dict[str, float]:
    """Helvetica advances, em/1000, from pdfminer's AFM table.

    Deliberately the same source pdfplumber measures with, so a width computed
    here and a width measured off the finished fixture cannot disagree.
    """
    from pdfminer.fontmetrics import FONT_METRICS

    return {c: w / 1000.0 for c, w in FONT_METRICS["Helvetica"][1].items() if len(c) == 1}


WIDTHS = _advance_widths()


def text_width(text: str, size: float = BODY_SIZE) -> float:
    """Rounded to 4dp deliberately.

    Every Helvetica advance is a whole thousandth of an em, so at 10pt every
    width is an exact multiple of 0.01pt and 4dp loses nothing real. What it
    does lose is float noise: `wrap()` compares a whole-string sum against a
    per-character accumulation of the same values, and unrounded those two can
    differ in the last bits — which at exactly the column edge would decide a
    line break on an artefact.
    """
    try:
        return round(sum(WIDTHS[c] for c in text) * size, 4)
    except KeyError as exc:
        raise ValueError(
            f"no Helvetica advance for {exc.args[0]!r}: the fixture would be "
            "laid out wrongly. Keep redacted text inside the statement's own "
            "character set."
        ) from None


def wrap(text: str, budget: float = NARRATION_WIDTH, size: float = BODY_SIZE) -> list[str]:
    """Break a narration into rendered lines the way Canara's renderer does.

    Greedy fill to `budget`, then retreat to the last break opportunity inside
    what fitted. Opportunities, in the order they were established from the
    real file:

      * a space — the line is right-stripped and the whole whitespace run is
        discarded, which is the loss §6.8.2 measures;
      * immediately after a hyphen — the hyphen stays on the line;
      * immediately before a hyphen, but only when the hyphen is the character
        that did not fit (one row of 93 breaks this way).

    With no opportunity in the fitted prefix the break is mid-token and nothing
    is consumed. See `WRAP_EVIDENCE`.
    """
    lines: list[str] = []
    while True:
        if text_width(text, size) <= budget:
            lines.append(text.rstrip())
            return lines

        cut = len(text)
        for i in range(1, len(text) + 1):
            if text_width(text[:i], size) > budget:
                cut = i - 1
                break

        breaks = [p for p in range(1, cut + 1) if text[p:p + 1] == " " or text[p - 1] == "-"]
        if text[cut:cut + 1] == "-":
            breaks.append(cut)
        at = max(breaks) if breaks else cut

        lines.append(text[:at].rstrip())
        text = text[at:].lstrip(" ")


# ── content stream helpers ──────────────────────────────────────────────────

def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


class _Page:
    """One page's content stream, in the real export's own idiom."""

    def __init__(self) -> None:
        self.parts: list[str] = ["2 J\n"]

    def show(self, x: float, y: float, text: str, size: float = BODY_SIZE) -> None:
        self.parts.append(
            f"BT\n1 0 0 1 {x:g} {y:g} Tm\n/F1 {size:g} Tf\n"
            f"0 0 0 rg\n({_escape(text)})Tj\n0 g\nET\n"
        )

    def show_right(self, right: float, y: float, text: str, size: float = BODY_SIZE) -> None:
        self.show(right - text_width(text, size), y, text, size)

    def rounded_box(self, x0: float, y0: float, x1: float, y1: float, r: float = 5.52) -> None:
        """The two panels behind the metadata block on page 1.

        Reproduced because they are the whole reason SPEC §6.8 rules Camelot
        out: they are the only edges on the page, so there is no ruled grid for
        `lattice` to find. A fixture without them could not carry that claim.
        """
        self.parts.append(
            "0 0 0 RG\n1 1 1 rg\n1 w\n[] 0 d\n"
            f"{x0 + r:g} {y0:g} m\n{x1 - r:g} {y0:g} l\n"
            f"{x1 - r + r:g} {y0:g} {x1:g} {y0 + r:g} {x1:g} {y0 + 2 * r:g} c\n"
            f"{x1:g} {y1 - 2 * r:g} l\n"
            f"{x1:g} {y1 - r:g} {x1 - r:g} {y1:g} {x1 - 2 * r:g} {y1:g} c\n"
            f"{x0 + 2 * r:g} {y1:g} l\n"
            f"{x0 + r:g} {y1:g} {x0:g} {y1 - r:g} {x0:g} {y1 - 2 * r:g} c\n"
            f"{x0:g} {y0 + 2 * r:g} l\n"
            f"{x0:g} {y0 + r:g} {x0 + r:g} {y0:g} {x0 + 2 * r:g} {y0:g} c\nB\n"
        )

    def render(self) -> bytes:
        return "".join(self.parts).encode("latin-1")


# ── the statement, in the PDF's own dialect ─────────────────────────────────

MONTH_NUMBER = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
    "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}


def _pdf_date(canonical: str) -> str:
    """`09-MAY-2026` -> `09-05-2026`, the form the PDF prints."""
    day, month, year = canonical.split("-")
    return f"{int(day):02d}-{MONTH_NUMBER[month.upper()]}-{year}"


def _title_date(canonical: str) -> str:
    """`07-MAY-2026` -> `07-May-2026`, the form the PDF's period line prints."""
    day, month, year = canonical.split("-")
    return f"{int(day):02d}-{month[:1].upper()}{month[1:].lower()}-{year}"


def _grid_fields(rows: Rows) -> dict:
    """Read the canonical (XLS-dialect) grid `redact.py` produced.

    The PDF speaks a different dialect of the same statement, so this is the
    inverse of the translation table in `loaders/pdf.py`'s docstring. Doing it
    in both directions is what makes the round trip a test.
    """
    import re
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from passbook.loaders._table import _find_header, _find_sentinel, norm

    header_row, cols = _find_header(rows)
    open_row, _ = _find_sentinel(rows, header_row, "openingbalance")
    close_row, _ = _find_sentinel(rows, header_row, "closingbalance")

    meta: dict[str, str] = {}
    period = None
    for r in range(header_row):
        for label_col in (0, 3):
            if label_col + 1 < len(rows[r]):
                meta.setdefault(norm(rows[r][label_col]), rows[r][label_col + 1].strip())
        for cell in rows[r]:
            found = re.search(
                r"from\s+(\d{1,2}-[A-Za-z]{3}-\d{4})\s+to\s+(\d{1,2}-[A-Za-z]{3}-\d{4})",
                cell, re.IGNORECASE,
            )
            if found:
                period = found.groups()

    if period is None:
        raise ValueError("grid carries no 'from <date> to <date>' period line")

    entries = []
    for r in range(open_row + 1, close_row):
        if not rows[r][cols["date"]].strip() and not rows[r][cols["txn_id"]].strip():
            continue
        entries.append({
            "date": _pdf_date(rows[r][cols["date"]].strip()),
            "debit": rows[r][cols["debit"]].strip(),
            "credit": rows[r][cols["credit"]].strip(),
            "balance": rows[r][cols["balance"]].strip(),
            "narration": rows[r][cols["narration"]],
        })

    return {
        "meta": meta,
        "period": period,
        "opening": rows[open_row][cols["balance"]].strip(),
        "closing": rows[close_row][cols["balance"]].strip(),
        "entries": entries,
    }


def _metadata_block(page: _Page, meta: dict[str, str]) -> None:
    """The two panels above the table, laid out as the real page 1 lays them.

    One quirk is reproduced on purpose: the **Branch Code value sits 1pt above
    its label**, so it groups into a line of its own and `Branch Code\\s+(\\d+)`
    never matches. That is why `StatementMeta.branch_code` comes back empty
    from a PDF and populated from an XLS, and why
    `test_pdf_matches_xls.py::test_metadata_matches` does not compare it.
    """
    page.rounded_box(20, 580, 294, 722)
    page.rounded_box(302, 580, 574, 722)

    page.show(30, 699.72, "Client")
    page.show(105, 699.72, meta.get("customerid", ""))
    page.show(30, 685.72, "Name")
    page.show(105, 685.72, meta.get("name", ""))
    page.show(30, 672.72, "Address")
    address = [part.strip() for part in meta.get("address", "").split(",") if part.strip()]
    for i, line in enumerate(address[:4]):
        page.show(105, 671.72 - i * LINE_STEP, line + ("," if i < len(address) - 1 else ""))
    page.show(30, 601.72, "Phone")
    page.show(105, 601.72, FIXTURE_PHONE)

    page.show(312, 699.72, "Branch Code")
    page.show(396, 700.72, meta.get("branchcode", ""))     # the 1pt lift, verbatim
    page.show(312, 685.72, "Branch Name")
    page.show(396, 685.72, meta.get("branchname", FIXTURE_BRANCH_NAME))
    page.show(312, 672.72, "Address")
    page.show(396, 672.72, FIXTURE_BANK_ADDRESS)
    page.show(396, 661.08, FIXTURE_MICR_LINE)
    page.show(312, 601.72, "IFSC Code")
    page.show(396, 601.72, meta.get("ifsccode", ""))


# Synthetic, and matching scripts/redact.py's block. The real page carries the
# holder's phone number and postal address and the branch's MICR code; SPEC §11
# lists all three.
FIXTURE_PHONE = "910000000000"
FIXTURE_BRANCH_NAME = "TEST BRANCH"
FIXTURE_BANK_ADDRESS = "TEST BANK 0/000 TEST ROAD"
FIXTURE_MICR_LINE = "TESTVILLE MICR Code : 000000000"


def build_pages(rows: Rows) -> list[_Page]:
    grid = _grid_fields(rows)
    meta = grid["meta"]

    pages = [_Page()]
    page = pages[0]
    page.show(
        20, Y_TITLE,
        f"Statement for A/c {meta.get('accountnumber', '')} "
        f"Between {_title_date(grid['period'][0])} and {_title_date(grid['period'][1])}",
        TITLE_SIZE,
    )
    _metadata_block(page, meta)
    for label, x in HEADERS:
        page.show(x, Y_HEADER_FIRST, label)
    page.show(X_OPENING_LABEL, Y_OPENING, "Opening Balance")
    page.show_right(RIGHT_BALANCE, Y_OPENING, grid["opening"])

    y = Y_ROW_FIRST
    for entry in grid["entries"]:
        lines = wrap(entry["narration"])
        if y - LINE_STEP * (len(lines) - 1) < Y_FLOOR:
            page = _Page()
            pages.append(page)
            for label, x in HEADERS:
                page.show(x, Y_HEADER_CONT, label)
            y = Y_ROW_CONT

        page.show(X_DATE, y, entry["date"])
        for i, line in enumerate(lines):
            page.show(X_NARRATION, y - i * LINE_STEP, line)
        if entry["debit"]:
            page.show_right(RIGHT_DEBIT, y, entry["debit"])
        else:
            page.show(X_EMPTY_DEBIT, y, "")
        if entry["credit"]:
            page.show_right(RIGHT_CREDIT, y, entry["credit"])
        else:
            page.show(X_EMPTY_CREDIT, y, "")
        page.show_right(RIGHT_BALANCE, y, entry["balance"])

        y -= LINE_STEP * (len(lines) - 1) + (
            ROW_GAP_SINGLE if len(lines) == 1 else ROW_GAP
        )

    if y < Y_CLOSING:
        page = _Page()
        pages.append(page)
        for label, x in HEADERS:
            page.show(x, Y_HEADER_CONT, label)
    page.show(X_CLOSING_LABEL, Y_CLOSING, "Closing Balance")
    page.show_right(RIGHT_BALANCE, Y_CLOSING, grid["closing"])

    for number, page in enumerate(pages, start=1):
        page.show(X_FOOTER, Y_FOOTER, f"Page {number} of", FOOTER_SIZE)
        page.show(X_FOOTER_COUNT, Y_FOOTER, f" {len(pages)}", FOOTER_SIZE)

    return pages


def write(rows: Rows, dest: Path, password: str) -> int:
    """Write `dest` as an RC4-40 encrypted PDF. Returns the page count.

    **RC4-40 (`/V 1 /R 2`) on purpose**, matching the real export. §6.8.1
    records that as nominal protection over a four-digit secret, and the
    fixture has to reproduce it or `loaders/pdf._decrypt` — the only code that
    handles an encrypted statement — is never exercised by the suite.

    The password is the last four digits of the fixture's synthetic account
    number, which §6.8.1 establishes is not credential-class: the UI already
    prints those four digits as the masked account.
    """
    import pikepdf

    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name.Helvetica,
            Encoding=pikepdf.Name.WinAnsiEncoding,
        )
    )
    pages = build_pages(rows)
    for page in pages:
        pdf.pages.append(
            pikepdf.Page(
                pdf.make_indirect(
                    pikepdf.Dictionary(
                        Type=pikepdf.Name.Page,
                        MediaBox=[0, 0, PAGE_WIDTH, PAGE_HEIGHT],
                        Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)),
                        Contents=pdf.make_stream(page.render()),
                    )
                )
            )
        )

    # Written twice, and both halves are load-bearing.
    #
    # qpdf refuses to generate a deterministic `/ID` for an encrypted file, and
    # for `/R 2` the ID is an input to the key derivation — so a directly
    # encrypted save is different bytes on every run, and a committed fixture
    # would show a diff every time `make fixtures` ran. Writing plain first with
    # `deterministic_id=True` and encrypting the result keeps the ID, so
    # identical content yields an identical file. Verified: two runs, one sha.
    #
    # No /Info and no XMP either: a fixture's provenance is its generator, and a
    # Producer string is one more place a real path or username can leak.
    plain = io.BytesIO()
    pdf.save(plain, deterministic_id=True)
    plain.seek(0)
    with pikepdf.open(plain) as reopened:
        reopened.save(
            dest,
            # `metadata=False` is not a choice: RC4-40 predates
            # `/EncryptMetadata` and qpdf refuses the combination. There is no
            # metadata to protect.
            encryption=pikepdf.Encryption(
                owner=password, user=password, R=2, aes=False, metadata=False
            ),
        )

    # ...and one 16-byte hole is left. qpdf keeps the deterministic **first**
    # `/ID` element but mints a fresh random **second** one on every write, per
    # the spec's "identifier at last update". For `/R 2` only the first element
    # feeds the key derivation, and nothing anywhere reads the second, so
    # copying the first over it is safe and closes the last source of churn.
    raw = dest.read_bytes()
    patched, replaced = re.subn(
        rb"/ID \[<([0-9a-fA-F]{32})><[0-9a-fA-F]{32}>\]",
        lambda m: b"/ID [<" + m.group(1) + b"><" + m.group(1) + b">]",
        raw,
    )
    if replaced != 1:
        raise ValueError(f"expected exactly one trailer /ID to pin, found {replaced}")
    dest.write_bytes(patched)
    return len(pages)
