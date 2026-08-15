"""Shared fixtures. Everything here reads tests/fixtures/, never inbox/.

The fixtures are produced by scripts/redact.py from a real statement, with the
metadata block, narrations and amounts rewritten but the balance chain
recomputed so the §6.6 invariant stays meaningful.
"""

import csv
from pathlib import Path

import pytest

# Anchored on this file, never on an absolute path. Four tests used to name one
# particular checkout, which made them pass on exactly one machine.
REPO_ROOT = Path(__file__).resolve().parent.parent

FIXTURES = Path(__file__).parent / "fixtures"
XLS_FIXTURE = FIXTURES / "statement.xls"
# A second account whose transaction ids collide with the first's completely, and
# whose last four digits collide too. SPEC §21.1 — the bank sequences ids per
# account, so this is the real shape of a second Canara account, not a contrivance.
SECOND_FIXTURE = FIXTURES / "statement-second.xls"
SECOND_ACCOUNT = "888800001111"
CSV_FIXTURE = FIXTURES / "statement.csv"
HTML_FIXTURE = FIXTURES / "statement.html"

# Matches scripts/redact.py's synthetic metadata block.
FIXTURE_ACCOUNT = "999900001111"
FIXTURE_SHEET_NAME = "Account Number 999900001111"
FIXTURE_TXN_COUNT = 93


@pytest.fixture
def rows() -> list[list[str]]:
    with CSV_FIXTURE.open(newline="", encoding="utf-8") as fh:
        return [list(r) for r in csv.reader(fh)]


@pytest.fixture
def parsed():
    from passbook.loaders import load

    return load(CSV_FIXTURE)


@pytest.fixture
def enriched(parsed):
    """Transactions with narration fields applied, as the CLI does."""
    from passbook import narration

    meta, transactions = parsed
    narration.enrich(transactions)
    return meta, transactions
