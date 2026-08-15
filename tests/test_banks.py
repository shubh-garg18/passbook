"""The bank registry — the seam a second bank goes through. SPEC §22.5.

These are the tests a new bank's PR is measured against, so they double as the
worked example `docs/adding-a-bank.md` points at. The made-up bank below is
deliberately complete and deliberately tiny: if adding a dialect needs more than
this, the seam is in the wrong place.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from conftest import CSV_FIXTURE
from passbook import banks
from passbook.loaders import load
from passbook.loaders._table import ParseError, from_rows, norm


# ── the shipped dialect ──────────────────────────────────────────────────────

def test_canara_is_registered_and_reachable_by_slug():
    bank = banks.get("canara")
    assert bank.slug == "canara"
    assert bank.name == "Canara Bank"
    assert "canara" in banks.slugs()


def test_an_unregistered_bank_is_refused_with_the_list():
    with pytest.raises(banks.UnknownBank) as caught:
        banks.get("hdfc")
    message = str(caught.value)
    assert "hdfc" in message and "canara" in message
    assert "docs/adding-a-bank.md" in message


def test_the_misspelled_header_is_matched_as_the_bank_writes_it():
    """`Trasnaction ID` is the bank's own typo. SPEC §6.3."""
    aliases = banks.get("canara").column_aliases
    assert aliases["trasnactionid"] == "txn_id"
    assert aliases["transactionid"] == "txn_id", "still works if they ever fix it"
    assert aliases["remarks"] == "narration", "not Particulars, not Description"


def test_canara_dates_parse_without_a_locale():
    """`%b` reads the active locale, which differs between machines. SPEC §6.3."""
    assert banks.get("canara").parse_date("09-MAY-2026") == date(2026, 5, 9)
    assert banks.get("canara").parse_date("9-may-2026") == date(2026, 5, 9)
    with pytest.raises(ParseError):
        banks.get("canara").parse_date("2026-05-09")


def test_canara_claims_the_fixture():
    rows = _rows(CSV_FIXTURE)
    assert banks.get("canara").detect(rows) is True
    assert banks.detect(rows).slug == "canara"


# ── what a new bank has to satisfy ───────────────────────────────────────────

def _rows(path) -> list[list[str]]:
    import csv

    with path.open(newline="", encoding="utf-8") as handle:
        return [list(row) for row in csv.reader(handle)]


@pytest.fixture
def toy_bank(monkeypatch):
    """A complete second dialect, registered for the duration of one test.

    Registered into the live registry on purpose: the point is to prove that a
    second bank coexists with Canara rather than that a mock can be constructed.
    """
    def parse_date(text: str) -> date:
        return date(*(int(part) for part in text.strip().split("-")))

    def detect(rows) -> bool:
        return any(norm(cell) == "toybankplc" for row in rows[:50] for cell in row)

    bank = banks.Bank(
        slug="toy",
        name="Toy Bank plc",
        column_aliases={
            "when": "date", "ref": "txn_id", "out": "debit",
            "in": "credit", "running": "balance", "detail": "narration",
        },
        required_columns=frozenset(
            {"date", "txn_id", "debit", "credit", "balance", "narration"}
        ),
        metadata_labels={"acct": "account_number", "cust": "customer_id"},
        metadata_label_columns=(0,),
        opening_label="broughtforward",
        closing_label="carriedforward",
        period_pattern=re.compile(r"from\s+(\S+)\s+to\s+(\S+)", re.IGNORECASE),
        parse_date=parse_date,
        detect=detect,
    )
    saved = dict(banks._REGISTRY)
    banks._REGISTRY["toy"] = bank
    yield bank
    banks._REGISTRY.clear()
    banks._REGISTRY.update(saved)


TOY_ROWS = [
    ["Toy Bank plc"],
    ["Statement from 2026-05-07 to 2026-05-09"],
    ["Acct", "999900001111"],
    ["Cust", "888800011"],
    ["When", "Ref", "Out", "In", "Running", "Detail"],
    ["", "Brought forward", "", "", "1000.00", ""],
    ["2026-05-08", "T1", "250.00", " ", "750.00", "PAY/ZEPKV JYX"],
    ["2026-05-09", "T2", " ", "125.50", "875.50", "IN/NYXN XWUBQ"],
    ["", "Carried forward", "", "", "875.50", ""],
]


def test_a_new_bank_is_one_value_and_no_edits_elsewhere(toy_bank):
    """The whole contribution: a `Bank`, and rows parse. SPEC §22.5."""
    meta, transactions = from_rows(TOY_ROWS)
    assert meta.account_number == "999900001111"
    assert meta.opening_balance.quantize(meta.closing_balance) is not None
    assert len(transactions) == 2
    assert transactions[0].debit is not None and transactions[0].credit is None
    # The `' '` trap, in a bank that never heard of Canara.
    assert transactions[0].credit is None, "a single space is empty, not zero"
    assert transactions[1].debit is None


def test_detection_reads_content_so_two_banks_do_not_fight(toy_bank):
    """Each claims its own file and neither claims the other's. §21.6's rule."""
    canara_rows = _rows(CSV_FIXTURE)
    assert banks.detect(canara_rows).slug == "canara"
    assert banks.detect(TOY_ROWS).slug == "toy"
    assert toy_bank.detect(canara_rows) is False
    assert banks.get("canara").detect(TOY_ROWS) is False


def test_a_grid_no_bank_claims_is_a_parse_error_not_a_crash():
    """It has to be a ParseError, or the upload path answers 500 and leaves the
    file in inbox/ where the next sync would find it."""
    with pytest.raises(ParseError) as caught:
        from_rows([["nothing that looks like a statement"]])
    assert isinstance(caught.value, banks.UnknownBank)
    assert "canara" in str(caught.value)


def test_two_banks_claiming_one_file_refuses_rather_than_guesses(monkeypatch):
    greedy = banks.Bank(
        slug="greedy",
        name="Greedy Bank",
        column_aliases={},
        required_columns=frozenset(),
        metadata_labels={},
        parse_date=lambda text: date(2026, 1, 1),
        detect=lambda rows: True,
    )
    saved = dict(banks._REGISTRY)
    banks._REGISTRY["greedy"] = greedy
    try:
        with pytest.raises(banks.UnknownBank) as caught:
            banks.detect(_rows(CSV_FIXTURE))
        assert "2 banks claim" in str(caught.value)
    finally:
        banks._REGISTRY.clear()
        banks._REGISTRY.update(saved)


def test_a_slug_that_cannot_live_in_an_external_id_is_refused():
    """The slug is a prefix of every `external_id` and has to stay separable
    from the bank's own 14-digit id by regex alone. SPEC §21.1."""
    for bad in ("Canara", "canara_2", "canara 2", "canara.2"):
        with pytest.raises(ValueError, match="must match"):
            banks.Bank(
                slug=bad, name="x", column_aliases={}, required_columns=frozenset(),
                metadata_labels={}, parse_date=lambda t: date(2026, 1, 1),
                detect=lambda rows: False,
            )


def test_a_bank_without_the_two_required_functions_is_refused():
    with pytest.raises(ValueError, match="needs parse_date and detect"):
        banks.Bank(
            slug="half", name="Half", column_aliases={},
            required_columns=frozenset(), metadata_labels={},
        )


def test_the_registry_is_what_config_validates_against():
    """`config/accounts.yaml`'s `bank:` field resolves here, not against a
    second hardcoded list that would drift. SPEC §21.4."""
    from passbook.config import supported_banks

    assert supported_banks() == tuple(banks.slugs())
    assert "canara" in supported_banks()


def test_the_shipped_loaders_still_route_through_the_registry():
    """End to end: sniffer -> loader -> registry -> grid, on a real fixture."""
    meta, transactions = load(CSV_FIXTURE)
    assert len(transactions) == 93
    assert meta.masked_account == "****1111"
