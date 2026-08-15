"""What the PDF fixture must be, independent of the XLS it is compared against.

`test_pdf_matches_xls.py` asserts the two loaders agree. This file asserts the
things that make that comparison worth anything: that the fixture is genuinely
encrypted the way the bank's export is, that it carries no leftover PII, that it
has no ruled grid (the evidence for SPEC §6.8's Camelot decision), and that the
line-breaking `pdfwrite.py` emulates is the one measured off the real file.

The wrap cases are synthetic strings shaped like the real ones. The real strings
cannot be committed — they carry the operator's name, counterparty tokens and
UTRs (§11) — so each case records which real row it was derived from and which
branch it exercises.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from passbook.loaders import pdf, xls
from passbook.loaders._table import ParseError

from conftest import FIXTURE_ACCOUNT

ROOT = Path(__file__).resolve().parent.parent
PDF_FIXTURE = Path(__file__).parent / "fixtures" / "statement.pdf"
PASSWORD = FIXTURE_ACCOUNT[-4:]

sys.path.insert(0, str(ROOT / "scripts"))  # pdfwrite is a dev tool, not a package
import pdfwrite  # noqa: E402


# ── encryption ──────────────────────────────────────────────────────────────

def test_the_fixture_is_rc4_40_like_the_real_export():
    """`/V 1 /R 2`, verbatim — §6.8.1.

    Not decoration. `loaders/pdf._decrypt` is the only code that handles an
    encrypted statement, and before this fixture existed nothing in the suite
    ever ran it: every committed fixture was plaintext.
    """
    import pikepdf

    with pikepdf.open(PDF_FIXTURE, password=PASSWORD) as document:
        assert document.encryption.R == 2
        assert document.encryption.V == 1
        assert document.encryption.bits == 40


def test_the_password_is_the_last_four_of_the_account_number():
    """§6.8.1's correction, asserted rather than trusted to a comment."""
    meta, _ = pdf.load(PDF_FIXTURE, password=PASSWORD)
    assert meta.account_number.endswith(PASSWORD)
    assert meta.account_number == FIXTURE_ACCOUNT


def test_without_the_password_the_loader_refuses_and_names_the_variable():
    with pytest.raises(pdf.PdfPasswordRequired) as caught:
        pdf.load(PDF_FIXTURE, password=None)
    assert "CANARA_PDF_PASSWORD" in str(caught.value)
    # The password must not appear in the message, the class name or anywhere
    # else a traceback would print (§11).
    assert PASSWORD not in str(caught.value)


def test_a_wrong_password_is_refused_rather_than_half_read():
    with pytest.raises(pdf.PdfPasswordRequired):
        pdf.load(PDF_FIXTURE, password="0000")


def test_the_sniffer_routes_it_and_the_password_comes_from_the_environment(monkeypatch):
    """The whole path an uploaded PDF actually takes: magic bytes -> loader ->
    `CANARA_PDF_PASSWORD`. Nothing tested this end to end before, because every
    committed fixture was a format that needs no password."""
    from passbook.loaders import load, sniff

    monkeypatch.setenv("CANARA_PDF_PASSWORD", PASSWORD)
    assert sniff(PDF_FIXTURE) == "pdf"
    meta, transactions = load(PDF_FIXTURE)
    assert len(transactions) == 93
    assert meta.masked_account == "****1111"


def test_a_truncated_pdf_is_a_parse_error_not_a_crash(tmp_path):
    """The upload path answers 422 on this, not 500 — so it must be ParseError."""
    broken = tmp_path / "truncated.pdf"
    broken.write_bytes(PDF_FIXTURE.read_bytes()[: 1024])
    with pytest.raises(ParseError):
        pdf.load(broken, password=PASSWORD)


# ── the fixture carries nothing real ────────────────────────────────────────

def _plaintext() -> str:
    """Everything inside the fixture, decrypted and decompressed.

    A byte scan of the file itself proves nothing: RC4 plus Flate means any
    string at all "does not appear". This is the same blob
    `scripts/redact.py --audit` scans.
    """
    import io

    import pikepdf

    with pikepdf.open(PDF_FIXTURE, password=PASSWORD) as document:
        buffer = io.BytesIO()
        document.save(buffer, compress_streams=False, normalize_content=True)
    return buffer.getvalue().decode("latin-1")


def test_the_fixture_carries_no_document_metadata():
    """No /Info, no XMP: a Producer or ModDate string is one more place a real
    path, username or timestamp can ride along into a committed file."""
    import pikepdf

    with pikepdf.open(PDF_FIXTURE, password=PASSWORD) as document:
        assert "/Info" not in document.trailer
        assert "/Metadata" not in document.Root


def test_the_fixture_metadata_block_is_the_synthetic_one():
    meta, _ = pdf.load(PDF_FIXTURE, password=PASSWORD)
    assert meta.account_number == FIXTURE_ACCOUNT
    assert meta.customer_id == "888800011"
    assert meta.account_name == "TEST HOLDER"
    assert meta.ifsc == "CNRB0009999"


def test_no_real_looking_contact_details_survive():
    """The real page 1 prints the holder's phone and postal address and the
    branch's MICR code (§11). All three are replaced by synthetic constants this
    repo owns, and nothing else numeric is allowed to survive into the block."""
    blob = _plaintext()
    assert pdfwrite.FIXTURE_PHONE in blob
    assert "TEST BANK" in blob
    assert "MICR Code : 000000000" in blob

    # Stated as a property, not as a list of one real branch's name and address.
    # Listing those in a public test would be the leak the test exists to
    # prevent. Instead: every digit run in the metadata block — the region above
    # the table header, where §11's account number, customer id, phone and MICR
    # code live — has to come from a synthetic constant.
    import pdfplumber

    with pdfplumber.open(PDF_FIXTURE, password=PASSWORD) as document:
        words = document.pages[0].extract_words()
        header_top = min(w["top"] for w in words if w["text"] == "Particulars")
        block = " ".join(w["text"] for w in words if w["top"] < header_top)

    synthetic = (
        FIXTURE_ACCOUNT, "888800011", pdfwrite.FIXTURE_PHONE, "CNRB0009999",
        pdfwrite.FIXTURE_MICR_LINE, pdfwrite.FIXTURE_BANK_ADDRESS,
    )
    # 5+, so the statement period's year and the 3-digit branch code are not
    # mistaken for identifiers. Everything §11 names is longer than that.
    for run in re.findall(r"\d{5,}", block):
        assert any(run in s for s in synthetic), f"non-synthetic digit run {run!r}"


def test_the_fixture_has_no_embedded_images():
    """The real export carries the Canara logo as an image XObject. It is bank
    branding the parser never reads, so it is not reproduced — and asserting
    that keeps a future generator from copying one out of a real statement."""
    import pikepdf

    with pikepdf.open(PDF_FIXTURE, password=PASSWORD) as document:
        for page in document.pages:
            assert "/XObject" not in page.Resources


# ── no ruled grid: the evidence for choosing pdfplumber over Camelot ────────

def test_there_is_no_ruled_table_to_detect():
    """SPEC §6.8 rules out Camelot's `lattice` flavour because the page has
    only the metadata panels' frame on it — no cell borders anywhere. That claim
    had no test. Page 1 carries the two rounded panels; the continuation pages
    carry nothing at all, so a lattice detector has literally nothing to find.
    """
    import pdfplumber

    with pdfplumber.open(PDF_FIXTURE, password=PASSWORD) as document:
        first, *rest = document.pages
        verticals = [e for e in first.edges if e["orientation"] == "v"]
        assert len(verticals) == 4, "page 1 should show only the two panels"
        for page in rest:
            assert page.edges == [], "a continuation page has no rules at all"
            assert page.extract_words(), "...but it does have text"


def test_the_money_columns_are_where_the_loader_expects_them():
    """`_bounds` derives every boundary from the header words' own geometry, so
    this asserts the fixture's header is placed such that no word can land in
    two columns — the defect §6.8's docstring records from the first version."""
    import pdfplumber

    with pdfplumber.open(PDF_FIXTURE, password=PASSWORD) as document:
        columns = pdf._columns(document.pages)
        assert columns is not None
        date_edge, narration_edge, debit_edge, credit_edge = pdf._bounds(columns)
        assert date_edge < narration_edge < debit_edge < credit_edge
        checked = 0
        for page in document.pages:
            # Only the table region: the title line above the header is one long
            # run of running text and its words cross every boundary there is,
            # which is fine — the loader never slices columns out of it.
            header_top = min(
                w["top"] for w in page.extract_words() if w["text"] == "Particulars"
            )
            for word in page.extract_words():
                if word["top"] <= header_top:
                    continue
                checked += 1
                assert word["x1"] <= narration_edge or word["x0"] >= narration_edge, (
                    f"{word['text']!r} straddles the narration boundary"
                )
        assert checked > 300, f"only {checked} words examined — is the fixture empty?"


# ── the wrap emulation, pinned ──────────────────────────────────────────────

# Each case is (text, expected lines, what it exercises). Derived from the real
# statement's own breaks, then rewritten with synthetic tokens of the same
# lengths so the same branch fires at the same character.
WRAP_CASES = [
    (
        "UPI/DR/649524006544/ZOKVE"
        " KVA/YESB/**15659@YBL/UPI//AXI62CB44292417484E9BD6542D4A7523C8"
        "/09/05/2026 01:51:33",
        [
            "UPI/DR/649524006544/ZOKVE",
            "KVA/YESB/**15659@YBL/UPI//AXI62CB44292417484E",
            "9BD6542D4A7523C8/09/05/2026 01:51:33",
        ],
        "break at a space (the run is discarded), then two mid-token breaks",
    ),
    (
        "UPI/DR/650819822650/ZOKVE VEXQ/UBIN/**KIU-2@OKICICI/UPI//"
        "AXIB320731F9AA045C480249EF0F7F5CFB0/22/05/2026 10:53:56",
        [
            "UPI/DR/650819822650/ZOKVE VEXQ/UBIN/**KIU-",
            "2@OKICICI/UPI//AXIB320731F9AA045C480249EF0F7F",
            "5CFB0/22/05/2026 10:53:56",
        ],
        "a hyphen beats an earlier space, and stays on the line",
    ),
    (
        "SBINT FOR THE PERIOD FROM28-MAR-26 TO 27-JUN-26",
        ["SBINT FOR THE PERIOD FROM28-MAR-26 TO 27-JUN", "-26"],
        "the hyphen is the character that did not fit, so it goes to the next "
        "line — one row of 93 breaks this way",
    ),
    (
        "UPI/DR/621542479523/QILM -"
        " II/YESB/**50HVE@PTY/UPI//AXIFAAE21CC9DD447F989DFD979D5E223E5"
        "/03/08/2026 20:19:07",
        [
            "UPI/DR/621542479523/QILM -",
            "II/YESB/**50HVE@PTY/UPI//AXIFAAE21CC9DD447F98",
            "9DFD979D5E223E5/03/08/2026 20:19:07",
        ],
        "a hyphen surrounded by spaces: the space after it wins, and both the "
        "hyphen and the trailing space survive on the line",
    ),
    (
        "SMS CHARGES ON ACTUAL BASIS",
        ["SMS CHARGES ON ACTUAL BASIS"],
        "short enough not to wrap at all",
    ),
]


@pytest.mark.parametrize("text,expected,why", WRAP_CASES)
def test_wrap_reproduces_the_renderers_breaks(text, expected, why):
    assert pdfwrite.wrap(text) == expected, why


def test_the_wrap_budget_is_the_narration_columns_own_width():
    """254pt = x 85 (Particulars) to x 339. Verified against the real export:
    254.0 reproduces all 93 rows fragment-for-fragment, 254.5 reproduces 92.
    A budget derived from anything else would be a coincidence."""
    assert pdfwrite.NARRATION_WIDTH == 254.0
    assert pdfwrite.X_NARRATION + pdfwrite.NARRATION_WIDTH == 339.0
    for _, expected, _ in WRAP_CASES:
        for line in expected:
            assert pdfwrite.text_width(line) <= pdfwrite.NARRATION_WIDTH


def test_every_narration_in_the_fixture_stays_inside_its_column():
    """Belt and braces on the generator: a line wider than the budget would put
    narration text into the Withdrawals column and be parsed as an amount."""
    _, transactions = xls.load(Path(__file__).parent / "fixtures" / "statement.xls")
    for txn in transactions:
        for line in pdfwrite.wrap(txn.narration):
            assert pdfwrite.text_width(line) <= pdfwrite.NARRATION_WIDTH


def test_the_fixture_is_regenerated_byte_for_byte(tmp_path):
    """`make fixtures` must not produce a diff when nothing changed.

    qpdf refuses to generate a deterministic `/ID` for an encrypted file, and
    with `/R 2` the ID feeds the key derivation — so a naive save is different
    bytes every run and the committed fixture would churn. `pdfwrite.write`
    writes plain-with-deterministic-ID first and encrypts that.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import redact

    source = ROOT / "archive/2026-08/Acnt_stmt__07052026_07082026.xls"
    if not source.is_file():
        pytest.skip("needs the operator's own export, which is gitignored (§11)")

    again = tmp_path / "statement.pdf"
    pdfwrite.write(redact.build_grid(source, seed=0), again, PASSWORD)
    assert again.read_bytes() == PDF_FIXTURE.read_bytes()


def test_the_generator_refuses_a_character_it_cannot_measure():
    """A glyph outside Helvetica's AFM table would be laid out at width zero and
    silently overprint. It raises instead, naming the character."""
    with pytest.raises(ValueError, match="Helvetica advance"):
        pdfwrite.text_width("₹100")


def test_narration_line_counts_match_the_real_distribution():
    """The real statement wraps to 1, 2, 3 and 4 lines (3 / 2 / 73 / 15 rows).
    The fixture has to cover the same shapes or the continuation rule is only
    half exercised."""
    _, transactions = xls.load(Path(__file__).parent / "fixtures" / "statement.xls")
    counts = {n: 0 for n in (1, 2, 3, 4)}
    for txn in transactions:
        counts[len(pdfwrite.wrap(txn.narration))] += 1
    assert counts[1] and counts[3] and counts[4], counts
    assert sum(counts.values()) == 93


def test_the_wrap_evidence_is_recorded_next_to_the_code():
    """The budget and the break rule are measurements. If the docstring goes,
    the next reader has a magic number."""
    assert "93/93" in pdfwrite.WRAP_EVIDENCE
    assert re.search(r"193 breaks", pdfwrite.WRAP_EVIDENCE)
