"""Turn a real statement into a safe test fixture. SPEC §11 — required deliverable.

Rewrites, per SPEC §11's list:
  * the worksheet sheet name (it embeds the account number)
  * the entire metadata block (account number, customer ID, name, address)
  * every counterparty phone and account number inside narrations
  * the customer ID embedded in PMSBY scheme narrations
  * the debit-card last-4
  * every amount, and the running balance recomputed to match

Two properties are preserved deliberately, because the tests depend on them:

  * **Balance-column internal consistency.** Amounts are regenerated, so the
    balance chain is recomputed from a synthetic opening balance. The §6.6
    invariant therefore still holds on the fixture and the test stays
    meaningful rather than tautological.
  * **String lengths.** Fake payees are the same length as the originals, so
    the ~10-character truncation signature (SPEC §6.5, D10) survives redaction.

Deterministic: same input always yields the same fixture.

    uv run python scripts/redact.py inbox/statement.xls tests/fixtures/sample.csv

Four containers, one redacted grid: `.xls`, `.csv`, `.html` and `.pdf`. The PDF
is rendered by `scripts/pdfwrite.py` and encrypted RC4-40 like the real export.
It comes from the *same* grid as the others on purpose — `redact.py` regenerates
every amount, so a PDF fixture derived any other way could not be compared
figure-for-figure against the XLS one, which is the whole job of
`tests/test_pdf_matches_xls.py`.
"""

import argparse
import csv
import random
import re
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling: pdfwrite

from passbook import narration as narration_mod  # noqa: E402
from passbook.loaders._table import (  # noqa: E402
    _find_header,
    _find_sentinel,
    norm,
    parse_amount,
)

# Last 4 deliberately differ from any real account, so a fixture can never be
# mistaken for the operator's own statement in test output.
FIXTURE_ACCOUNT = "999900001111"

# The real worksheet is named "Account Number <full account number>" — SPEC §6.3
# flags the sheet name as a second place the number leaks. Must stay <= 31 chars
# and free of []:*?/\ or xlwt rejects it.
FIXTURE_SHEET_NAME = f"Account Number {FIXTURE_ACCOUNT}"

# The PDF password is the last four digits of the account number (SPEC §6.8.1),
# so the fixture's password follows from the fixture's account number. Not
# credential-class: those four digits are what §11's masking rule already
# prints.
FIXTURE_PDF_PASSWORD = FIXTURE_ACCOUNT[-4:]

# Nonsense syllables, not plausible names. A pool of realistic names risks
# colliding with a real counterparty ("VIVEK", "CLEARING"), which makes an
# audit of the fixture ambiguous — you cannot tell a coincidence from a leak.
SYLLABLES = ["ZOK", "VEX", "QIL", "MUR", "ZAB", "XEN", "PLY", "GRU",
             "THO", "KVA", "NYX", "WUB", "ZEP", "QOR", "FLU", "JYX"]

# Structural tokens that must survive intact or the grammars stop parsing.
KEEP = {
    "UPI", "DR", "CR", "REF", "NEFT", "INET", "IMPS", "SBINT", "PMSBY",
    "SMS", "CHARGES", "ON", "ACTUAL", "BASIS", "DEBIT", "CARD", "ANNUAL",
    "TO", "FROM", "THE", "PERIOD", "FOR", "RENEWAL", "BANK", "LIMITED",
    "MISC", "INR", "AC", "FBO", "CUR",
}


class Redactor:
    def __init__(self, seed: int = 0) -> None:
        self.rng = random.Random(seed)
        self._names: dict[str, str] = {}

    def digits(self, run: str) -> str:
        """Same-length replacement, stable per distinct input."""
        if run not in self._names:
            self._names[run] = "".join(str(self.rng.randint(0, 9)) for _ in run)
        return self._names[run]

    def name(self, original: str) -> str:
        """Same-length nonsense, stable per distinct input.

        Length and the position of spaces and punctuation are preserved: the
        length is what carries the ~10-char truncation signature (SPEC §6.5,
        D10) into the fixture, and the shape is what keeps the grammars
        parseable.
        """
        if original not in self._names:
            filler = "".join(
                self.rng.choice(SYLLABLES) for _ in range(len(original) // 3 + 2)
            )
            self._names[original] = "".join(
                ch if not ch.isalnum() else filler[i % len(filler)]
                for i, ch in enumerate(original)
            )
        return self._names[original]

    def scrub_narration(self, text: str, holder: str) -> str:
        if not text.strip():
            return text
        out = text

        # 1. the payee the parser itself identifies
        payee = narration_mod.parse(out).get("payee")
        if payee and payee not in ("PMSBY", "Bank Charges", "Savings Interest"):
            out = out.replace(payee, self.name(payee))

        # 2. the account holder's own name, and any prefix of it >= 5 chars.
        #    It appears inside NEFT narrations in truncated form.
        for length in range(len(holder), 4, -1):
            prefix = holder[:length].strip()
            if len(prefix) >= 5 and prefix in out:
                out = out.replace(prefix, self.name(prefix))

        # 3. any remaining uppercase word run that is not structural
        def _word(m: re.Match) -> str:
            token = m.group()
            return token if token.strip() in KEEP else self.name(token)

        out = re.sub(r"\b[A-Z]{4,}(?: [A-Z]{2,})*\b", _word, out)

        # 4. every digit run of 4+: counterparty accounts and phones, the
        #    embedded customer ID, the card last-4, UTRs and RRNs.
        #    Date/time components are 2 or 4 digits — the 4-digit year would be
        #    caught, so protect the trailing timestamp first.
        stamp = narration_mod.TRAILING_TIMESTAMP.search(out)
        tail = stamp.group() if stamp else ""
        head = out[: stamp.start()] if stamp else out
        head = re.sub(r"\d{4,}", lambda m: self.digits(m.group()), head)
        return head + tail


def build_grid(path: Path, seed: int, account: str = FIXTURE_ACCOUNT) -> list[list[str]]:
    import xlrd

    sheet = xlrd.open_workbook(str(path)).sheet_by_index(0)
    rows = [[str(sheet.cell(r, c).value) for c in range(sheet.ncols)] for r in range(sheet.nrows)]

    red = Redactor(seed)
    header_row, cols = _find_header(rows)
    open_row, _ = _find_sentinel(rows, header_row, "openingbalance")
    close_row, _ = _find_sentinel(rows, header_row, "closingbalance")

    holder = ""
    for r in range(header_row):
        if norm(rows[r][0]) == "name":
            holder = rows[r][1].strip()

    # --- metadata block -------------------------------------------------------
    fixed = {
        "accountnumber": account,
        "customerid": "888800011",
        "name": "TEST HOLDER",
        "address": "1 EXAMPLE ROAD, TESTVILLE, 000000 TEST STATE",
        # Branch code and IFSC identify a branch, and SPEC §11 lists both as
        # things the file leaks. Synthetic, not the real branch.
        "branchcode": "999",
        "ifsccode": "CNRB0009999",
        "branchname": "TEST BRANCH",
    }
    for r in range(header_row):
        for label_col in (0, 3):
            if label_col + 1 >= len(rows[r]):
                continue
            key = norm(rows[r][label_col])
            if key in fixed and rows[r][label_col + 1].strip():
                rows[r][label_col + 1] = fixed[key]

    # --- amounts and the balance chain ---------------------------------------
    opening = Decimal("10000.00")
    rows[open_row][cols["balance"]] = f"{opening:,.2f}"
    running = opening
    rng = random.Random(seed + 1)
    for r in range(open_row + 1, close_row):
        if not rows[r][cols["date"]].strip() and not rows[r][cols["txn_id"]].strip():
            continue
        was_debit = bool(rows[r][cols["debit"]].strip())
        amount = Decimal(rng.randrange(1000, 900000)) / 100
        # Never let a synthetic debit drive the balance negative.
        if was_debit and amount > running:
            amount = (running / 2).quantize(Decimal("0.01"))
        running = running - amount if was_debit else running + amount
        # The empty side is a single space, not '' — that is the real file's
        # quirk and the fixture must reproduce it. SPEC §6.3.
        rows[r][cols["debit"]] = f"{amount:,.2f}" if was_debit else " "
        rows[r][cols["credit"]] = " " if was_debit else f"{amount:,.2f}"
        rows[r][cols["balance"]] = f"{running:,.2f}"
        rows[r][cols["narration"]] = red.scrub_narration(rows[r][cols["narration"]], holder)
    rows[close_row][cols["balance"]] = f"{running:,.2f}"

    return rows


def _scannable(dest: Path) -> bytes:
    """Everything in the fixture a leak could hide in, as one blob.

    For the flat containers that is the file. For the PDF it cannot be: the
    bytes on disk are RC4-encrypted and Flate-compressed, so a plain byte scan
    of an encrypted fixture finds nothing and reports **clean** whatever is
    inside it — the exact failure mode an audit exists to prevent. So the PDF is
    decrypted and rewritten uncompressed first, which yields every content
    stream, every string and the trailer in one blob, and the raw file is
    appended as well to cover anything left outside the encryption (the
    encryption dictionary itself, and metadata if `/EncryptMetadata` is false).
    """
    raw = dest.read_bytes()
    if dest.suffix.lower() != ".pdf":
        return raw

    import io

    import pikepdf

    with pikepdf.open(dest, password=FIXTURE_PDF_PASSWORD) as pdf:
        buffer = io.BytesIO()
        pdf.save(buffer, compress_streams=False, normalize_content=True)
    return buffer.getvalue() + raw


def audit(source: Path, dest: Path) -> int:
    """Compare the fixture against its source and report anything that survived.

    Runs over cell values *and* the whole fixture (see `_scannable`), because a
    sheet name or a stale string table entry can carry data that no cell
    exposes.

    Two classes of match are expected and are not leaks:
      * transaction IDs, which are deliberately preserved (YYYYMMDD + a daily
        sequence, carrying no personal data, and needed by the §6.6 assertion);
      * short digit runs that collide by chance with the randomly generated
        replacements. Those are reported so they can be eyeballed, not hidden.
    """
    import xlrd

    real = xlrd.open_workbook(str(source)).sheet_by_index(0)
    digits, words = set(), set()
    for r in range(real.nrows):
        for c in range(real.ncols):
            v = str(real.cell(r, c).value)
            digits |= set(re.findall(r"\d{5,}", v))
            words |= set(re.findall(r"\b[A-Z]{5,}\b", v))
    digits |= set(re.findall(r"\d{5,}", real.name))

    structural = {
        "CHARGES", "ACTUAL", "BASIS", "DEBIT", "ANNUAL", "PERIOD", "RENEWAL",
        "LIMITED", "SBINT", "PMSBY", "XXXXXXXXXXX", "CANARA", "STATEMENT",
        "ACCOUNT", "BALANCE", "OPENING", "CLOSING", "TRASNACTION",
        "WITHDRAWALS", "DEPOSITS", "REMARKS",
    }
    raw = _scannable(dest)
    if dest.suffix.lower() == ".xls":
        sheet = xlrd.open_workbook(str(dest)).sheet_by_index(0)
        txn_ids = {str(sheet.cell(r, 1).value).strip() for r in range(sheet.nrows)}
    elif dest.suffix.lower() == ".pdf":
        # The PDF carries no transaction-id column at all (§6.8), so nothing is
        # exempt here and every 5+ digit run has to be accounted for.
        txn_ids = set()
    else:
        txn_ids = set(re.findall(r"\b\d{14}\b", raw.decode("utf-8", "replace")))

    bad_words = sorted(w for w in words if w not in structural and w.encode() in raw)
    hits = sorted(d for d in digits if d not in txn_ids and d.encode() in raw)

    print(f"  audit {dest}: {len(digits)} digit-runs and {len(words)} words checked")
    if bad_words:
        print(f"  LEAK: words present in fixture: {bad_words}")
    if hits:
        print(f"  review: digit runs also present (expect chance collisions): {hits}")
    if not bad_words and not hits:
        print("  clean: nothing from the source survives")
    return 1 if bad_words else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path)
    ap.add_argument("dest", type=Path)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--account",
        default=FIXTURE_ACCOUNT,
        help=(
            "synthetic account number for the fixture. A second fixture sharing "
            "the LAST FOUR with the first, and carrying identical transaction "
            "ids, is what SPEC §21.1's namespacing has to survive — the bank "
            "sequences ids per account, so two accounts really do collide."
        ),
    )
    ap.add_argument("--audit", action="store_true", help="diff the fixture against its source")
    args = ap.parse_args()

    rows = build_grid(args.source, args.seed, account=args.account)
    args.dest.parent.mkdir(parents=True, exist_ok=True)

    if args.dest.suffix.lower() == ".xls":
        # xlwt writes BIFF8, which xlrd 2.x reads — so the fixture exercises the
        # real OLE2 path, not just the shared row core. Dev-only dependency.
        import xlwt

        book = xlwt.Workbook(encoding="utf-8")
        # The real sheet name embeds the account number. SPEC §6.3, §11.
        sheet = book.add_sheet(f"Account Number {args.account}")
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                # Written as text, always: every cell in the real export is a
                # string, and the fixture must reproduce that.
                sheet.write(r, c, value)
        book.save(str(args.dest))
    elif args.dest.suffix.lower() == ".csv":
        with args.dest.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh, quoting=csv.QUOTE_ALL).writerows(rows)
    elif args.dest.suffix.lower() in (".html", ".htm"):
        cells = "\n".join(
            "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows
        )
        # Sheet name is dropped entirely rather than redacted: HTML has none.
        args.dest.write_text(f"<html><body><table>\n{cells}\n</table></body></html>", "utf-8")
    elif args.dest.suffix.lower() == ".pdf":
        # Rendered from this same grid and encrypted RC4-40, like the real
        # export. See scripts/pdfwrite.py for what is and is not reproduced.
        import pdfwrite

        pages = pdfwrite.write(rows, args.dest, FIXTURE_PDF_PASSWORD)
        print(f"  pdf: {pages} pages, RC4-40, password = last 4 of the fixture account")
    else:
        raise SystemExit(
            f"unsupported fixture format {args.dest.suffix!r}; use .xls, .csv, .html or .pdf"
        )

    # Deliberately reports counts only. Printing a redacted narration to a
    # terminal is how an unredacted one eventually gets printed by mistake.
    print(f"wrote {args.dest} ({len(rows)} rows, seed {args.seed})")

    if args.audit:
        raise SystemExit(audit(args.source, args.dest))


if __name__ == "__main__":
    main()
