"""Regenerate tests/fixtures/statement.golden.json from the CSV fixture.

Run only after confirming a golden diff is an intended change:

    uv run python -m tests.regenerate_golden
"""

import json
from pathlib import Path

from passbook import narration
from passbook.loaders import load
from passbook.models import normalised

FIXTURES = Path(__file__).parent / "fixtures"


def main() -> None:
    meta, transactions = load(FIXTURES / "statement.csv")
    # No aliases: config/payee_aliases.yaml is operator knowledge about real
    # counterparties, and the fixture's payees are redacted nonsense.
    narration.enrich(transactions)
    target = FIXTURES / "statement.golden.json"
    target.write_text(json.dumps(normalised(meta, transactions, []), indent=2) + "\n")
    print(f"wrote {target} ({len(transactions)} transactions)")


if __name__ == "__main__":
    main()
