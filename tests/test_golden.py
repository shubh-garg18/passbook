"""Golden-file test: fixture in, expected normalised JSON out. SPEC §10.

Regenerate deliberately, never reflexively, after confirming the diff is an
intended change:

    uv run python -m tests.regenerate_golden
"""

import json
from pathlib import Path

from conftest import FIXTURE_TXN_COUNT
from passbook.models import normalised

GOLDEN = Path(__file__).parent / "fixtures" / "statement.golden.json"


def test_normalised_output_matches_the_golden_file(enriched):
    meta, transactions = enriched
    got = normalised(meta, transactions, [])
    expected = json.loads(GOLDEN.read_text())
    assert got["meta"] == expected["meta"]
    assert len(got["transactions"]) == FIXTURE_TXN_COUNT
    for i, (a, b) in enumerate(zip(got["transactions"], expected["transactions"])):
        assert a == b, f"transaction {i} drifted"
    assert got == expected


def test_golden_file_carries_no_customer_id():
    """It is committed to the repo. SPEC §11."""
    text = GOLDEN.read_text()
    assert "customer_id" not in text
    assert "888800011" not in text  # redact.py's synthetic customer ID


def test_golden_amounts_are_strings_not_floats():
    """Decimal must survive serialisation. A float here means a float in the
    ledger. CLAUDE.md non-negotiable #1."""
    data = json.loads(GOLDEN.read_text())
    for txn in data["transactions"]:
        for field in ("debit", "credit", "balance"):
            assert txn[field] is None or isinstance(txn[field], str)
