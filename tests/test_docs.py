"""Tracked documentation may only cite FIXTURE values. non-negotiable 16.

This repository is public. A real figure in a tracked file is a permanent,
searchable disclosure of one person's finances, and it survives every future
edit that copies the paragraph around it. A one-time scrub is worth nothing: the
next phase writes a new balance into a paragraph and it is public forever. So
the rule is checked rather than remembered.

Most of this file arrives by port from the private repository this one is
released from, which is where a real figure would enter if anything did.

**What this reads.** Prose, not program text — every `.md` file in full, and the
comments and docstrings of everything else. A test asserting `Decimal("20000.00")`
is arithmetic; a comment saying *"this ledger reads 20,000.00"* is a disclosure.
The distinction is the whole point, and scanning code literals as well produced
enough noise to make the check ignorable.

Account numbers and UTRs are the exception: those are scanned everywhere,
literals included, because a 12-digit run has no innocent form here.

**What it deliberately cannot catch.** A payee token, a category name, or a
person's name. Those have no machine-checkable shape, and pretending otherwise
would be a green tick for something never looked at (non-negotiable 11).
`DECISIONS.md` §40 carries the human half of the rule.

The amount allowlist is **derived from `tests/fixtures/statement.golden.json`**,
not typed out, so a regenerated fixture updates it and a figure that is not in
the fixture cannot quietly become allowed.

Run standalone with `make audit-docs`.
"""

from __future__ import annotations

import json
import re
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import REPO_ROOT

GOLDEN = REPO_ROOT / "tests" / "fixtures" / "statement.golden.json"

# This file quotes deliberately-fake figures at itself, to prove the check can
# fail. Scanning it would be the check reporting its own test data.
SKIP = {"uv.lock", "frontend/package-lock.json", "tests/test_docs.py"}
SKIP_SUFFIX = {".xls", ".pdf", ".png", ".svg", ".ico", ".woff2", ".json", ".csv", ".html"}

# Synthetic identifiers this repo owns. Every one is a constant in
# `tests/conftest.py`, `scripts/redact.py`, `scripts/pdfwrite.py` or
# `scripts/shoot.py`, or an invented value in a test or a reconstructed fixture.
# None came from a bank.
SYNTHETIC_IDS = {
    "999900001111",  # conftest.FIXTURE_ACCOUNT
    "888800001111",  # conftest.SECOND_ACCOUNT — collides on last four, on purpose
    "111100001111",  # test_validate: a different number sharing the last four
    "111100009999",  # test_validate / test_web: a plainly different account
    "123456789012",  # test_narration: an IMPS counterparty account
    "910000000000",  # pdfwrite.FIXTURE_PHONE
    "000000009999",  # scripts/shoot.py's demo account
    "000000000001",  # a derived-id sentinel in the PDF table tests
    "111111111111",  # test_web: three accounts that are plainly not anyone's
    "222222222222",
    "333333333333",
    # test_narration / declared_columns UTRs — sequential by construction.
    "412345678901", "412345678902", "412345678903", "412345678904",
    "512345678901", "512345678902", "512345678903",
    # tests/fixtures/centred_headings.py and declared_columns.py. Those modules
    # reconstruct a page from a GEOMETRY dump (§50, §51): the coordinates are
    # measured and every character is invented. Their own docstrings say so.
    "194892411578", "410250014782", "450076279125",
    "740899331886", "883012455901", "933287115871",
    # tests/test_pdf_fixture.py WRAP_CASES. That block's own comment says the
    # cases were "derived from the real statement's own breaks, then rewritten
    # with synthetic tokens of the same lengths" — the break positions are
    # measured, the characters are not the bank's. Already published in
    # passbook, which allowlists the same three.
    "649524006544", "650819822650", "621542479523",
    # Invented account numbers in loader tests and narration fixtures.
    "409302010012345",  # _table.py: the `Account No.:` docstring example
    "1234567890123", "1234567890124",  # test_narration: an ATM and a PMSBY row
    "99999999999999", "19990101999999",  # out-of-range sentinels in date tests
    # Not a bank identifier at all: the GitHub Actions run whose 403 settled
    # what a workflow token can read (§22.8). Cited so the measurement can be
    # checked, and it happens to be 11 digits.
    "32881543735",
}

# Amounts that are configuration, formatting examples or arithmetic rather than
# anyone's balance.
NON_LEDGER_AMOUNTS = {
    "10,000", "10000",          # LARGE_TXN_THRESHOLD default
    "1,000", "100,000", "1,000.00",
    "12,34,567.89", "1,234,567.89", "1234567.89",  # the en-IN grouping example
    "9999999.00", "999999.00",  # a Postgres FM format mask, and an absurd amount
    "9,999.99", "12,345.67",    # invented amounts in formatting examples
    "5000.10",                  # config.py: a float-precision illustration
    "1,245.00",                 # charts.tsx: the exact form of a `1.2k` tick
    "100,100",                  # charts.tsx: an SVG rotation centre, not money
    "5000.00",                  # attribution.example.yaml: an example `keep`
    "1,234.00",                 # docs: an example of a comma separator
    # The fixture's opening minus its closing: the drift `verify-ledger`
    # reports when every row has been purged and only the opening balance is
    # left. SPEC §19.7 quotes it as an example message.
    "4,931.91",
}

# A comma-grouped number whose final group is exactly three digits, or a bare
# number with 4+ integer digits and two decimals. Both are the shape a balance
# takes; nothing else in this repo's prose takes either.
MONEY = re.compile(
    r"(?<![\d.,])("
    r"\d{1,3}(?:,\d{2,3})*,\d{3}(?:\.\d{1,2})?"
    r"|\d{4,}\.\d{2}"
    r")(?!\d)(?!,\d)"
)
#: An account number or a UTR. 12 is the common length here, but the second
#: bank writes a 15-digit one into its interest narration, so the window is
#: wider — §22.1 found exactly that sitting in a tracked test.
#: Alphanumeric boundaries, not just digit ones: a run inside a UTR
#: (`AXI62CB44292417484E9BD…`) or a NEFT reference (`HDFCH25142509791`) is
#: part of a reference, not an account number standing on its own.
LONG_DIGITS = re.compile(r"(?<![0-9A-Za-z])\d{11,18}(?![0-9A-Za-z])")

#: `YYYYMMDD` + a 6-digit daily sequence — a bank transaction id (§6.1), not
#: an account number. Derivable by construction, and the fixture is full of
#: them.
TXN_ID = re.compile(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{6}$")

# An account's last four, in the two forms this project writes it: the masked
# display (`****1111`) and the slug that namespaces an external_id
# (`canara-1111`, and `canara-1111-20260509000001`). Four digits are far too
# short to detect by shape, so this checks the opposite direction: a last-four
# in a tracked file must be one the fixture owns. That is the rule that would
# have caught the operator's real Canara and Union last-fours sitting in 40
# places across docs, comments, tests and example config before it was caught.
#: `****1111`. Three or more, because Markdown bold around a number
#: (`**3752**` lines, `**0600**` the file mode) is two.
MASKED_LAST_FOUR = re.compile(r"\*{3,}(\d{4})(?!\d)")
#: Bank-prefixed only. A bare `<word>-<4 digits>` also matches a Chromium build
#: (`chromium-1228`), a dated dump (`the ledger-2026-07-01`) and a file mode, and a
#: check with false positives is a check that gets ignored. Add a bank here when
#: one ships in `src/passbook/banks/`.
SLUG_LAST_FOUR = re.compile(
    r"(?<![\w-])(?:canara|union|sbi|hdfc|icici|axis|kotak|pnb|bob|idbi|yes)"
    r"-(\d{4})(?![\d])"
)

#: Derived, not typed: the last four of everything this repo generates.
SYNTHETIC_LAST_FOUR = {i[-4:] for i in SYNTHETIC_IDS} | {
    "2222",  # the synthetic second bank in docs/ACCOUNTS.md's tab strip
}

# A grouped run immediately followed by a unit is a measurement, not money.
UNIT_AFTER = re.compile(
    r"\s*(px|pt|KB|MB|GB|ms|bytes?|chars?|characters?|rows?|runs?|draws?"
    r"|transactions?|tests?|breaks?|lines?|files?)"
)

HASH_COMMENT = re.compile(r"#(?!!)[^\n]*")
SLASH_COMMENT = re.compile(r"//[^\n]*")
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
TRIPLE_QUOTED = re.compile(r'"""(?:.|\n)*?"""' + r"|'''(?:.|\n)*?'''")


def tracked_files() -> list[Path]:
    """Everything git tracks. Falls back to a walk before the first commit."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout
        names = [n for n in out.split("\0") if n]
        if names:
            return [REPO_ROOT / n for n in names]
    except (OSError, subprocess.CalledProcessError):
        pass
    skip_dirs = {".git", "node_modules", "inbox", "archive", "backups",
                 ".venv", "__pycache__", "dist", ".pytest_cache", "recovery"}
    return sorted(
        p for p in REPO_ROOT.rglob("*")
        if p.is_file() and not (skip_dirs & set(p.relative_to(REPO_ROOT).parts))
    )


def prose(path: Path, text: str) -> str:
    """The part of a file a human reads as English."""
    if path.suffix in {".md", ""}:
        return text
    if path.suffix == ".py":
        return "\n".join(TRIPLE_QUOTED.findall(text) + HASH_COMMENT.findall(text))
    if path.suffix in {".ts", ".tsx", ".css"}:
        return "\n".join(BLOCK_COMMENT.findall(text) + SLASH_COMMENT.findall(text))
    if path.suffix in {".sh", ".yaml", ".yml", ".toml", ".example", ".ps1"}:
        return "\n".join(HASH_COMMENT.findall(text))
    return ""


def fixture_amounts() -> set[str]:
    """Every figure the committed fixture contains, in both the plain and the
    comma-grouped form a document would print it in."""
    data = json.loads(GOLDEN.read_text())
    raw = {data["meta"]["opening_balance"], data["meta"]["closing_balance"]}
    for txn in data["transactions"]:
        for key in ("debit", "credit", "balance"):
            if txn[key]:
                raw.add(txn[key])
    debits = sum(Decimal(t["debit"]) for t in data["transactions"] if t["debit"])
    credits = sum(Decimal(t["credit"]) for t in data["transactions"] if t["credit"])
    raw |= {str(debits), str(credits)}

    allowed: set[str] = set()
    for value in raw:
        allowed.add(value)
        whole, _, frac = value.partition(".")
        for grouped in (f"{int(whole):,}", _en_in(whole)):
            allowed.add(grouped)
            if frac:
                allowed.add(f"{grouped}.{frac}")
    return allowed


def _en_in(whole: str) -> str:
    """`107686` -> `1,07,686`. The last three digits, then pairs — how the bank
    prints and how `formatINR` renders, so it is how a document quotes it."""
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    parts = []
    while len(head) > 2:
        head, part = head[:-2], head[-2:]
        parts.insert(0, part)
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


ALLOWED_AMOUNTS = fixture_amounts() | NON_LEDGER_AMOUNTS


def offending_amounts(text: str) -> list[str]:
    hits = []
    for match in MONEY.finditer(text):
        value = match.group(1)
        if value in ALLOWED_AMOUNTS or UNIT_AFTER.match(text, match.end()):
            continue
        hits.append(value)
    return hits


def offending_ids(text: str) -> list[str]:
    return [
        m.group(0) for m in LONG_DIGITS.finditer(text)
        if m.group(0) not in SYNTHETIC_IDS
        and set(m.group(0)) != {"0"}
        and not TXN_ID.match(m.group(0))
    ]


def _readable(path: Path) -> str | None:
    rel = path.relative_to(REPO_ROOT).as_posix()
    if rel in SKIP or path.suffix in SKIP_SUFFIX:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


@pytest.mark.parametrize(
    "path", tracked_files(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_prose_cites_only_fixture_figures(path: Path):
    text = _readable(path)
    if text is None:
        pytest.skip("binary, lockfile or generated fixture")

    hits = offending_amounts(prose(path, text))
    assert not hits, (
        f"{path.relative_to(REPO_ROOT)} cites {sorted(set(hits))}, which are not "
        "the fixture's figures.\nTracked documentation cites FIXTURE values, "
        "never a live ledger (non-negotiable 16). Use a number from "
        "tests/fixtures/statement.golden.json, or state a ratio instead."
    )


@pytest.mark.parametrize(
    "path", tracked_files(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_unrecognised_account_or_utr_appears_anywhere(path: Path):
    text = _readable(path)
    if text is None:
        pytest.skip("binary, lockfile or generated fixture")

    hits = offending_ids(text)
    assert not hits, (
        f"{path.relative_to(REPO_ROOT)} contains long digit run(s) {sorted(set(hits))}. "
        "An account number or UTR has no innocent form in a tracked file. Add it "
        "to SYNTHETIC_IDS only if this repo generated it."
    )


@pytest.mark.parametrize(
    "path", tracked_files(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_account_last_fours_are_the_fixtures(path: Path):
    """A masked account or a slug must carry a last-four this repo owns."""
    text = _readable(path)
    if text is None:
        pytest.skip("binary, lockfile or generated fixture")

    hits = sorted({
        m.group(1)
        for pattern in (MASKED_LAST_FOUR, SLUG_LAST_FOUR)
        for m in pattern.finditer(text)
        if m.group(1) not in SYNTHETIC_LAST_FOUR
    })
    assert not hits, (
        f"{path.relative_to(REPO_ROOT)} carries account last-four(s) {hits}. "
        "A masked number or an external_id slug in a tracked file uses the "
        "fixture's last four, never a real account's (non-negotiable 16, §22.1)."
    )


def test_the_check_can_actually_fail():
    """A regression test that cannot fail is not a regression test. SPEC §17.6.

    Every figure below is invented for this test and is nobody's balance.
    """
    assert offending_amounts("the ledger closed at 73,412.88 that month")
    assert not offending_amounts("the ledger closed at 5,068.09 that month")
    assert offending_ids("account 470112345678 on the statement")
    assert not offending_ids("account 999900001111 on the statement")
    assert MASKED_LAST_FOUR.findall("Canara ****7777") == ["7777"]
    assert MASKED_LAST_FOUR.findall("**8708px** of page") == []
    assert MASKED_LAST_FOUR.findall("Canara ****1111") == ["1111"]
    assert SLUG_LAST_FOUR.findall("canara-7777-20260509000001") == ["7777"]
    assert SLUG_LAST_FOUR.findall("canara-1111-20260509000001") == ["1111"]
    # Prose only: a literal in program text is arithmetic, not a disclosure.
    assert not offending_amounts(prose(Path("x.py"), 'x = Decimal("73,412.88")'))
    assert offending_amounts(prose(Path("x.py"), '# it read 73,412.88 that month'))
