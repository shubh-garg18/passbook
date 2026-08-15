"""Tracked documentation may only cite FIXTURE values. CLAUDE.md non-negotiable 14.

A one-time scrub of a repository's docs is worth nothing: the next phase writes
a new balance into a paragraph and it is public forever. So the rule is checked
rather than remembered.

**What this reads.** Prose, not program text — every `.md` file in full, and the
comments and docstrings of everything else. A test asserting
`Decimal("20000.00")` is arithmetic; a comment saying *"this ledger reads
20,000.00"* is a disclosure. The distinction is the whole point, and scanning
code literals as well produced enough noise to make the check ignorable.

Account numbers and UTRs are the exception: those are scanned everywhere,
literals included, because a 12-digit run has no innocent form here.

**What it deliberately cannot catch.** A payee token, a category name, or a
person's name. Those have no machine-checkable shape, and pretending otherwise
would be a green tick for something never looked at (non-negotiable 11).
`CONTRIBUTING.md` carries the human half of the rule.

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
# `scripts/redact.py`, `scripts/pdfwrite.py`, `scripts/shoot.py` or
# `tests/conftest.py`, or an invented value in a test. None came from a bank.
SYNTHETIC_IDS = {
    "999900001111",  # conftest.FIXTURE_ACCOUNT
    "888800001111",  # conftest.SECOND_ACCOUNT — collides on last four, on purpose
    "111100001111",  # test_validate: a different number sharing the last four
    "111100009999",  # test_web / test_validate: a plainly different account
    "123456789012",  # test_narration: an IMPS counterparty account
    "910000000000",  # pdfwrite.FIXTURE_PHONE
    "000000009999",  # scripts/shoot.py's demo account
    "412345678901",  # test_narration UTRs
    "412345678902",
    "412345678903",
    "649524006544",  # test_pdf_fixture wrap cases
    "650819822650",
    "621542479523",
}

# Amounts that are configuration, formatting examples or arithmetic rather than
# anyone's balance.
NON_LEDGER_AMOUNTS = {
    "10,000", "10000",          # LARGE_TXN_THRESHOLD default
    "1,000", "100,000",
    "12,34,567.89", "1,234,567.89", "1234567.89",  # the en-IN grouping example
    "9999999.00", "999999.00",  # a Postgres FM format mask, and an absurd amount
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
TWELVE_DIGITS = re.compile(r"(?<!\d)\d{12}(?!\d)")

# A grouped run immediately followed by a unit is a measurement, not money.
UNIT_AFTER = re.compile(r"\s*(px|pt|KB|MB|GB|ms|bytes?|chars?|characters?|rows?)")

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
    if path.suffix in {".sh", ".yaml", ".yml", ".toml", ".example"}:
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
        grouped = f"{int(whole):,}"
        allowed.add(grouped)
        if frac:
            allowed.add(f"{grouped}.{frac}")
    return allowed


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
        m.group(0) for m in TWELVE_DIGITS.finditer(text)
        if m.group(0) not in SYNTHETIC_IDS and set(m.group(0)) != {"0"}
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
        "never a live ledger (CLAUDE.md non-negotiable 14). Use a number from "
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
        f"{path.relative_to(REPO_ROOT)} contains 12-digit run(s) {sorted(set(hits))}. "
        "An account number or UTR has no innocent form in a tracked file. Add it "
        "to SYNTHETIC_IDS only if this repo generated it."
    )


def test_the_check_can_actually_fail():
    """A regression test that cannot fail is not a regression test. SPEC §17.6."""
    assert offending_amounts("the balance was 6,543.21") == ["6,543.21"]
    assert offending_amounts("it read 45678.90 instead") == ["45678.90"]
    assert offending_ids("account 110099887766 held it") == ["110099887766"]
    # …and does not fire on the fixture's own numbers, a measurement, or a
    # synthetic identifier.
    assert offending_amounts("the closing balance is 5,068.09") == []
    assert offending_amounts("the page was 13,348px tall") == []
    assert offending_ids("account 999900001111") == []


def test_it_reads_comments_but_not_code_literals():
    """The distinction this file exists to draw, asserted rather than assumed."""
    src = Path("x.py")
    assert "6,543.21" in prose(src, '# the ledger read 6,543.21\nx = 1\n')
    assert prose(src, 'amount = Decimal("20000.00")\n').strip() == ""
