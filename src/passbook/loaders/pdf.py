"""Canara PDF statement loader. SPEC §6.8, D4.

The fallback, never the primary. XLS remains what `make sync` expects; this
exists for the weeks when net banking will hand over a PDF and nothing else.

**It normalises into the canonical grid and hands off to `from_rows`.** Not one
line of transaction construction lives here. The PDF speaks a different dialect
of the same statement, and every difference is translated on the way in:

| The PDF says | The canonical grid wants |
|---|---|
| `Particulars` | `Remarks` |
| `09-05-2026` | `09-MAY-2026` |
| `Between <a> and <b>` | `from <a> to <b>` |
| `Client` | `Customer ID` |
| *(no transaction id column at all)* | `Trasnaction ID` |

That last one is the interesting one; see `_synthesise_id`.

**Why pdfplumber and not Camelot.** Camelot's `lattice` flavour finds cells by
detecting ruled lines, and this statement has **six horizontal and four
vertical edges on a page** — the page frame, nothing else. There is no grid to
detect, so `lattice` is out and only `stream` would apply, which is
column-position guessing of exactly the kind done here. Camelot then costs
OpenCV and **Ghostscript**, a system package, in an image whose whole design is
`python:slim` with no compiler. pdfplumber is pdfminer.six underneath: pure
Python, no system dependency, **no Ghostscript**, and it exposes per-word
coordinates, which is what the continuation rule needs.
"""

from __future__ import annotations

import io
import logging
import re
from collections import defaultdict
from pathlib import Path

from ..models import StatementMeta, Transaction
from ._table import MONTHS, ParseError, Rows, from_rows

log = logging.getLogger(__name__)

# How far short of the wrap boundary a line must stop before the break counts
# as having fallen on whitespace. See `_join`.
#
# Tuned for PAYEE agreement, not narration bytes, and that is the deliberate
# trade: `payee` is the string rules match on (D10), so it decides behaviour;
# `narration` goes to Firefly's notes verbatim (§7.2) and is expected to differ
# between formats. Measured across the 93-row cross-validation:
#
#     eps    payee      narration (whitespace-collapsed)
#       2    93/93      69/93
#       8    92/93      86/93
#      34    90/93      88/93
#
# 2.0 is the only value that makes the two formats behave identically.
WRAP_EPSILON = 2.0

_PDF_DATE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")
_PERIOD_PDF = re.compile(
    r"Between\s+(\d{1,2}-[A-Za-z]{3}-\d{4})\s+and\s+(\d{1,2}-[A-Za-z]{3}-\d{4})",
    re.IGNORECASE,
)
_MONTH_NAMES = {v: k for k, v in MONTHS.items()}


class PdfPasswordRequired(ParseError):
    """The statement is encrypted and no usable password was supplied."""


def _decrypt(path: Path, password: str | None) -> io.BytesIO:
    """Return a decrypted copy in memory. The file on disk is never rewritten.

    The password is a credential (§11) and appears in no log line, no exception
    message and no traceback — only ever as the `password=` argument.
    """
    import pikepdf

    candidates = [password] if password else []
    try:
        with pikepdf.open(path) as pdf:  # unencrypted, or empty user password
            buffer = io.BytesIO()
            pdf.save(buffer)
            buffer.seek(0)
            return buffer
    except pikepdf.PasswordError:
        pass
    except Exception as exc:
        # Truncated or malformed: a ParseError so the upload path answers 422
        # ("we could not read this file") rather than leaking a 500.
        raise ParseError(f"not a readable PDF: {type(exc).__name__}") from exc

    for candidate in candidates:
        try:
            with pikepdf.open(path, password=candidate) as pdf:
                buffer = io.BytesIO()
                pdf.save(buffer)
                buffer.seek(0)
                log.info("pdf: decrypted (password not logged)")
                return buffer
        except pikepdf.PasswordError:
            continue

    # The hint comes from whichever bank registered one, so a second bank's
    # PDF gets its own instruction rather than Canara's (§22.5). Listing every
    # registered hint is deliberate: at this point the file has not been parsed,
    # so which bank wrote it is not yet known.
    from ..banks import registered

    hints = [b.pdf_password_hint for b in registered() if b.pdf_password_hint]
    raise PdfPasswordRequired(
        "the statement is encrypted and the password did not work. "
        + " ".join(hints)
        + " The password is never logged or displayed."
    )


def _lines(page) -> list[list[dict]]:
    """Words grouped into visual lines, each sorted left to right."""
    buckets: dict[float, list[dict]] = defaultdict(list)
    for word in page.extract_words(keep_blank_chars=False, use_text_flow=False):
        buckets[round(word["top"], 1)].append(word)
    return [sorted(ws, key=lambda w: w["x0"]) for _, ws in sorted(buckets.items())]


def _char_lines(page) -> dict[float, list[dict]]:
    """Raw characters grouped the same way, spaces included.

    `extract_words` splits on whitespace, so rebuilding a line by joining words
    with `" "` collapses runs of spaces. The bank writes `XENGRU  EP` with two —
    and that string IS the payee token the rules match on (D10), so collapsing
    it silently renames the counterparty. The literal space glyphs are in the
    char stream; this keeps them.
    """
    buckets: dict[float, list[dict]] = defaultdict(list)
    for char in page.chars:
        buckets[round(char["top"], 1)].append(char)
    return {top: sorted(cs, key=lambda c: c["x0"]) for top, cs in buckets.items()}


def _slice(chars: list[dict], low: float, high: float) -> tuple[str, float]:
    """(text, right edge) for the characters starting within [low, high)."""
    picked = [c for c in chars if low <= c["x0"] < high]
    if not picked:
        return "", 0.0
    return "".join(c["text"] for c in picked).rstrip(), picked[-1]["x1"]


def _columns(pages) -> dict[str, tuple[float, float]] | None:
    """(x0, x1) of each header word, from the first page carrying the header."""
    for page in pages:
        for words in _lines(page):
            text = " ".join(w["text"] for w in words)
            if text.startswith("Date Particulars"):
                found = {w["text"].lower(): (w["x0"], w["x1"]) for w in words}
                if {"date", "particulars", "withdrawals", "deposits", "balance"} <= found.keys():
                    return found
    return None


# Clear of the widest narration line, and left of where any amount can start.
NARRATION_MARGIN = 12.0


def _bounds(cols: dict[str, tuple[float, float]]) -> tuple[float, float, float, float]:
    """Boundaries that assign each word to exactly ONE column.

    The first version mixed rules — narration by `x0`, amounts by `x1` — so a
    long narration token starting inside its own column but ending past the
    midpoint was counted as narration *and* as a withdrawal, and
    `UPI/DR/.../PANWAR` was handed to `parse_amount`.

    Narration and date are decided by where a word STARTS. Amounts are decided
    by where they END, because the money columns are right-aligned and a wide
    figure starts further left than a narrow one in the same column.
    """
    # Midpoint is right for CLASSIFYING a line (does it start with a date?),
    # but wrong for SLICING characters: `09-05-2026` starts at the Date column
    # and runs past the midpoint, so a midpoint slice prefixed every narration
    # with `026`. The narration slice therefore begins at the Particulars
    # column itself.
    date_edge = cols["particulars"][0] - 4.0
    # Amounts cannot begin before the Withdrawals header does, less a margin.
    narration_edge = cols["withdrawals"][0] - NARRATION_MARGIN
    debit_edge = (cols["withdrawals"][1] + cols["deposits"][0]) / 2
    credit_edge = (cols["deposits"][1] + cols["balance"][0]) / 2
    return date_edge, narration_edge, debit_edge, credit_edge


def _join(previous: str, addition: str, ended_at: float, wrap_at: float) -> str:
    """Append a continuation line to the narration it belongs to. SPEC §6.4.

    The rule that has never run until now. A wrapped narration produces a line
    with no date and no amounts, and it has to rejoin the transaction above it —
    but *how* it rejoins is the whole problem, because the renderer drops the
    information about whether a space was there.

    Measured on the reference statement: the renderer breaks at a space when
    one falls near the column edge, and mid-token when one does not. So a line
    that stops short of the wrap boundary ended at a real space; a line that
    runs to the boundary was cut through a token and must rejoin with nothing
    between. Getting this backwards produces narration that is *almost* right,
    which §6.5's grammars then tokenise into plausible nonsense.
    """
    if not previous:
        return addition
    # A break after a slash or a digit is inside the reference, VPA or UTR
    # fields, which hold no spaces — measured 32/32. Restricted to exactly
    # those two: the first version said "any non-letter", which swallowed the
    # real spaces in `JY. MURQO` and `XENN - UB` after `.` and `-`. Everything
    # else falls through to geometry.
    if previous[-1:] == "/" or previous[-1:].isdigit():
        return previous + addition
    return f"{previous} {addition}" if ended_at < wrap_at - WRAP_EPSILON else previous + addition


# `.../DD/MM/YYYY HH:MM:SS` — the trailing UPI timestamp (§6.5). Its single
# space is grammar, not guesswork, so a break landing on it is repaired exactly
# rather than left to the wrap heuristic. It matters more than the rest: §6.5
# strips this to populate `txn_time`, and without the space there is no clock.
# Not anchored to the end: the R01 reversal shape (§6.5) carries a trailing
# `/<ref>` AFTER the timestamp, so `$` missed it and that one row lost its
# clock — 92/93 instead of 93/93.
_TIMESTAMP = re.compile(r"(\d{2}/\d{2}/\d{4})(\d{2}:\d{2}:\d{2})")


def _repair_grammar(narration: str) -> str:
    return _TIMESTAMP.sub(r"\1 \2", narration)


def _synthesise_id(txn_date: str, ordinal: int) -> str:
    """`YYYYMMDD` + a 6-digit within-day sequence. SPEC §6.1, §6.8.

    The PDF has no transaction-id column, and `external_id` is what makes the
    push idempotent (§7.2) — without it every re-sync would duplicate the
    ledger. Reconstructed rather than hashed, because the bank's own id is
    exactly this: verified across all 45 days of the reference statement, the
    6-digit tail is a plain 1..n ordinal within each date, so date plus
    position reproduces the bank's value byte for byte.

    This holds only while the PDF lists a day's rows in the same order the XLS
    does. `test_pdf_matches_xls.py` is what keeps that honest.
    """
    day, month, year = txn_date.split("-")
    return f"{year}{month}{day}{ordinal:06d}"


def _to_rows(pages) -> Rows:
    """Rebuild the canonical grid: metadata block, header, transactions.

    Two passes, because the wrap boundary is a property of the whole document
    and the first transaction has to be joined before the widest line has been
    seen. Joining as we go used a `wrap_at` that only grew, so early rows were
    joined against a boundary that was still wrong.
    """
    cols = _columns(pages)
    if cols is None:
        raise ParseError(
            "no 'Date Particulars ... Balance' header found. This does not look "
            "like a Canara account statement PDF."
        )
    date_edge, narration_edge, debit_edge, credit_edge = _bounds(cols)

    meta_text: list[str] = []
    records: list[dict] = []
    seen_header = False

    # --- pass 1: classify every line, and learn the wrap boundary ----------
    for page in pages:
        char_lines = _char_lines(page)
        for words in _lines(page):
            text = " ".join(w["text"] for w in words)
            if text.startswith("Date Particulars"):
                seen_header = True
                continue
            if not seen_header:
                meta_text.append(text)
                continue

            first = words[0]
            starts = bool(
                first["x0"] < cols["particulars"][0] and _PDF_DATE.match(first["text"])
            )
            chars = char_lines.get(round(words[0]["top"], 1), [])
            narration, narration_end = _slice(chars, date_edge, narration_edge)
            if not starts and not narration:
                meta_text.append(text)  # page footers, "Closing Balance", totals
                continue

            record = {
                "starts": starts,
                "date": first["text"] if starts else "",
                "narration": narration,
                "ends": narration_end,
                "debit": "", "credit": "", "balance": "",
            }
            if starts:
                for w in words:
                    if w["x0"] < narration_edge:
                        continue  # date or narration — already taken
                    if w["x1"] < debit_edge:
                        record["debit"] = w["text"]
                    elif w["x1"] < credit_edge:
                        record["credit"] = w["text"]
                    else:
                        record["balance"] = w["text"]
            records.append(record)

    wrap_at = max((r["ends"] for r in records), default=0.0)

    # --- pass 2: fold continuations into the transaction above them -------
    entries: list[dict] = []
    for record in records:
        if record["starts"]:
            entries.append(dict(record))
            entries[-1]["last_end"] = record["ends"]
        elif entries:
            entries[-1]["narration"] = _join(
                entries[-1]["narration"], record["narration"],
                entries[-1]["last_end"], wrap_at,
            )
            entries[-1]["last_end"] = record["ends"]
        else:
            meta_text.append(record["narration"])

    return _grid(meta_text, entries, wrap_at)


def _canonical_date(pdf_date: str) -> str:
    day, month, year = pdf_date.split("-")
    return f"{day}-{_MONTH_NAMES[int(month)]}-{year}"


def _grid(meta_text: list[str], entries: list[dict], wrap_at: float) -> Rows:
    """Emit the grid `from_rows` expects, in the XLS's own vocabulary."""
    blob = "\n".join(meta_text)
    rows: Rows = []

    def meta(label: str, pattern: str) -> None:
        m = re.search(pattern, blob)
        if m:
            rows.append([label, m.group(1).strip(), "", "", "", ""])

    period = _PERIOD_PDF.search(blob)
    if not period:
        raise ParseError("no 'Between <date> and <date>' statement period line found")
    rows.append([f"Statement for Account from {period.group(1)} to {period.group(2)}",
                 "", "", "", "", ""])

    meta("Account Number", r"Statement for A/c\s+(\d+)")
    meta("Customer ID", r"Client\s+(\d+)")
    meta("Name", r"Name\s+([A-Z][A-Z .]+?)\s+Branch Name")
    meta("Branch Code", r"Branch Code\s+(\d+)")
    meta("IFSC Code", r"IFSC Code\s+([A-Z0-9]+)")

    rows.append(["Date", "Trasnaction ID", "Withdrawals", "Deposits", "Balance", "Remarks"])

    opening = re.search(r"Opening Balance\s+([\d,]+\.\d{2})", blob)
    if not opening:
        raise ParseError("no Opening Balance sentinel found")
    rows.append(["", "Opening Balance", "", "", opening.group(1), ""])

    per_day: dict[str, int] = defaultdict(int)
    closing = ""
    for entry in entries:
        canonical = _canonical_date(entry["date"])
        # A Closing Balance line carries a balance and no narration.
        per_day[entry["date"]] += 1
        rows.append([
            canonical,
            _synthesise_id(entry["date"], per_day[entry["date"]]),
            entry["debit"],
            entry["credit"],
            entry["balance"],
            _repair_grammar(entry["narration"]),
        ])
        closing = entry["balance"]

    tail = re.search(r"Closing Balance\s+([\d,]+\.\d{2})", blob)
    rows.append(["", "Closing Balance", "", "", tail.group(1) if tail else closing, ""])
    return rows


def load(path, password: str | None = None) -> tuple[StatementMeta, list[Transaction]]:
    import pdfplumber

    path = Path(path)
    if password is None:
        from ..config import load_settings

        password = load_settings().canara_pdf_password

    with pdfplumber.open(_decrypt(path, password)) as document:
        log.info("pdf: %d page(s)", len(document.pages))
        rows = _to_rows(document.pages)
    return from_rows(rows)
