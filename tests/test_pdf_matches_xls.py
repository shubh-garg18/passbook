"""Cross-validation: the PDF loader against the XLS loader. SPEC §6.8.

The same statement exists in both containers, so every field is comparable
directly. **Result: identical on everything except narration whitespace** — see
`test_narration_whitespace_is_not_recoverable` for why that is a property of the
PDF and not a bug in the loader.

Two pairs, and the distinction matters:

* **fixture** — `tests/fixtures/statement.xls` and `statement.pdf`, both
  rendered by `scripts/redact.py` from *one* redacted grid. Committed, so this
  runs everywhere, and it is what stops a change to either loader going
  unnoticed.
* **real** — the operator's own two exports of 07-May-2026 → 07-Aug-2026. This
  is the original evidence and it is gitignored (§11), so it skips on any other
  machine. Keeping it here keeps the fixture honest: the numbers pinned for the
  fixture pair are within one row of the numbers the real pair produces, and if
  the emulated renderer in `pdfwrite.py` ever drifted from the bank's, the two
  parametrisations would disagree.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from passbook.loaders import pdf, xls

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"

REAL_XLS = ROOT / "archive/2026-08/Acnt_stmt__07052026_07082026.xls"
REAL_PDF = ROOT / "inbox/Acnt_stmt__07052026_07082026.pdf"


@dataclass(frozen=True)
class Pair:
    xls: Path
    pdf: Path
    rows: int
    # Pinned, not asserted-as-equal: these are the KNOWN LIMIT of §6.8.2, and a
    # number that moves means a loader changed.
    exact_narration: int
    collapsed_narration: int
    counterparty_bank: int

    def password(self) -> str | None:
        raise NotImplementedError


class FixturePair(Pair):
    def password(self) -> str:
        # The last four digits of the fixture's synthetic account number
        # (§6.8.1). `test_pdf_fixture.py` asserts the two still agree, rather
        # than this reaching into conftest for a constant.
        return "1111"


class RealPair(Pair):
    def password(self) -> str | None:
        explicit = os.environ.get("CANARA_PDF_PASSWORD")
        if explicit:
            return explicit
        account = ""
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("CANARA_PDF_PASSWORD="):
                return line.split("=", 1)[1].strip().strip("\"'")
            if line.startswith("PASSBOOK_ACCOUNT_NUMBER="):
                account = line.split("=", 1)[1].strip().strip("\"'")
        return account[-4:] or None


FIXTURE = FixturePair(
    xls=FIXTURES / "statement.xls",
    pdf=FIXTURES / "statement.pdf",
    rows=93,
    exact_narration=58,
    collapsed_narration=69,
    counterparty_bank=92,
)
REAL = RealPair(
    xls=REAL_XLS,
    pdf=REAL_PDF,
    rows=93,
    exact_narration=57,
    collapsed_narration=69,
    counterparty_bank=90,
)


@pytest.fixture(
    scope="module",
    params=[
        pytest.param(FIXTURE, id="fixture"),
        pytest.param(
            REAL,
            id="real",
            marks=pytest.mark.skipif(
                not (REAL_XLS.is_file() and REAL_PDF.is_file()),
                reason="the operator's own exports are gitignored (§11)",
            ),
        ),
    ],
)
def pair(request) -> Pair:
    return request.param


@pytest.fixture(scope="module")
def both(pair):
    return pair, xls.load(pair.xls), pdf.load(pair.pdf, password=pair.password())


@pytest.fixture(scope="module")
def enriched(both):
    """The same rows with §6.5 applied — once, since `both` is module-scoped.

    `narration.enrich` mutates in place, so calling it per test would mean a
    later test's aliases silently overwriting an earlier test's. The real
    aliases key on real tokens, so on the fixture pair they match nothing and
    both sides get `None`; that still asserts the two loaders agree, just more
    cheaply than on the real pair.
    """
    from passbook import narration
    from passbook.config import load_payee_aliases

    _, (_, xt), (_, pt) = both
    aliases = load_payee_aliases()
    narration.enrich(xt, aliases)
    narration.enrich(pt, aliases)
    return both


def test_same_row_count(both):
    pair, (_, xt), (_, pt) = both
    assert len(pt) == len(xt) == pair.rows


@pytest.mark.parametrize("field", ["txn_id", "txn_date", "debit", "credit", "balance"])
def test_every_row_matches_field_for_field(both, field):
    """txn_id included: the PDF has no id column at all, so this also asserts
    that reconstructing it from date + within-day ordinal reproduces the
    bank's own value exactly."""
    _, (_, xt), (_, pt) = both
    differing = [
        (i, getattr(a, field), getattr(b, field))
        for i, (a, b) in enumerate(zip(xt, pt))
        if getattr(a, field) != getattr(b, field)
    ]
    assert not differing, f"{len(differing)} rows differ on {field}: {differing[:3]}"


@pytest.mark.parametrize(
    "field",
    ["account_number", "customer_id", "account_name", "ifsc",
     "period_from", "period_to", "opening_balance", "closing_balance"],
)
def test_metadata_matches(both, field):
    _, (xm, _), (pm, _) = both
    assert getattr(pm, field) == getattr(xm, field)


def test_branch_code_is_the_one_metadata_field_the_pdf_cannot_carry(both):
    """Not an omission from the list above — a measured property of the layout.

    The PDF draws the Branch Code *value* one point higher than its label, so
    the value groups into a line of its own and `Branch Code\\s+(\\d+)` has no
    digits to find. Confirmed on the real export, and `pdfwrite.py` reproduces
    the 1pt lift deliberately so the fixture cannot quietly be kinder than the
    bank. Nothing reads `branch_code`, which is why this is recorded rather
    than fixed.
    """
    _, (xm, _), (pm, _) = both
    assert xm.branch_code, "the XLS carries a branch code"
    assert pm.branch_code == ""


def test_the_ledger_closes_at_the_same_balance(both):
    """§6.6 on the PDF's own numbers — the invariant that matters most."""
    _, (xm, xt), (pm, pt) = both
    running = pm.opening_balance
    for txn in pt:
        running += (txn.credit or 0) - (txn.debit or 0)
        assert running == txn.balance, f"continuity broke at {txn.txn_id}"
    assert running == pm.closing_balance == xm.closing_balance


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_narration_whitespace_is_not_recoverable(both):
    """The one field that does NOT match, recorded rather than papered over.

    Canara's PDF wraps a narration across up to four lines, and the renderer
    **discards the whitespace run at the break**. Measured on the real
    statement:

      * the space glyphs are absent from the char stream — `page.chars` has no
        trailing space on any wrapped fragment;
      * leftover width does not separate the cases (0 spaces: 0-41 units,
        1 space: 4-132, 2 spaces: 104-117 — overlapping);
      * the fragment's last character predicts it only where the break falls
        outside a payee name (`/` and digits: 0 spaces, 32/32) and is ambiguous
        inside one (`A`: 0, 1 and 2 spaces all observed).

    193 breaks across 93 rows: 105 consumed nothing, 75 one space, 12 two, 1
    four. Nothing in the file distinguishes the last three.

    Asserted as a KNOWN LIMIT, not skipped, so the number moves if either
    loader changes.
    """
    pair, (_, xt), (_, pt) = both
    exact = sum(1 for a, b in zip(xt, pt) if a.narration == b.narration)
    collapsed = sum(
        1 for a, b in zip(xt, pt) if _collapse(a.narration) == _collapse(b.narration)
    )
    # Low on purpose. `WRAP_EPSILON` is tuned for PAYEE agreement (93/93),
    # which decides behaviour, not for narration bytes, which go to notes
    # verbatim and are expected to differ per format (§6.8.3). Tuning the
    # other way gives 88/93 collapsed and costs three payee tokens.
    assert exact == pair.exact_narration, f"exact narration matches moved: {exact}/93"
    assert collapsed == pair.collapsed_narration, f"collapsed matches moved: {collapsed}/93"


def test_what_the_whitespace_loss_actually_costs(enriched):
    """The consequence, measured through the enrichment §6.5 performs.

    Whitespace inside a narration is not cosmetic: the payee token IS the
    string rules match on (D10), so a lost space silently renames the
    counterparty and a different rule fires — or none does.

    Everything that does not depend on the payee field survives intact.
    """
    pair, (_, xt), (_, pt) = enriched

    def agree(field: str) -> int:
        return sum(1 for a, b in zip(xt, pt) if getattr(a, field) == getattr(b, field))

    # Unaffected: nothing here is inside the payee field.
    assert agree("channel") == pair.rows
    assert agree("utr") == pair.rows
    assert agree("is_reversal") == pair.rows

    # The point of §6.8.3: after collapsing whitespace in `payee` — and only in
    # `payee` — the two formats behave identically. A PDF-sourced push and an
    # XLS-sourced push of the same statement categorise the same way.
    assert agree("payee") == pair.rows, "payee agreement moved"
    assert agree("payee_alias") == pair.rows, "alias agreement moved"
    assert agree("txn_time") == pair.rows, "txn_time agreement moved"


def test_counterparty_bank_is_the_residual_and_is_pinned(enriched):
    """The field the original cross-validation never looked at.

    §6.8.4's table lists every field that drives behaviour and
    `counterparty_bank` is not among them — accurate, but silent about the fact
    that it is the one *parsed* field the two formats disagree on. Measured on
    the real pair: **90/93**. The three misses are breaks where the wrap
    heuristic guessed a space that was not there and the guess landed inside
    the handle token (`OKSBI` -> `O KSBI`), so this is the residual error
    `WRAP_EPSILON` trades away to keep `payee` at 93/93.

    Tolerable because nothing reads it: it is not pushed (§7.2 sends
    description, notes, external_id, amount, date), no rule matches on it
    (D10 matches the display name), and it appears nowhere in the UI. Pinned so
    that stops being an assumption.
    """
    pair, (_, xt), (_, pt) = enriched
    agree = sum(1 for a, b in zip(xt, pt) if a.counterparty_bank == b.counterparty_bank)
    assert agree == pair.counterparty_bank, f"counterparty_bank agreement moved: {agree}/93"
