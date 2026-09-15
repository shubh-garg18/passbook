"""Ledger integrity, and the purge intent that makes an interruption visible.

SPEC §19, §20. On 2026-08-11 a purge completed and the re-push stopped after 21
of 93 rows. The result was a ledger with a **self-consistent balance and no error
anywhere**, and it stayed that way for seven hours while 349 tests, `doctor`,
`make check` and the status strip all passed.

The first test here is the one that matters: it reconstructs that exact state and
asserts the check catches it, naming what is missing and by how much. Everything
else guards a way of getting it wrong.

No network: `verify_ledger` takes a store, and a fake one is enough.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import FIXTURE_ACCOUNT, XLS_FIXTURE, StoreDouble
from passbook import ops, service
from passbook.config import Account

ACCOUNT = "Test Account"


class FakeLedger(StoreDouble):
    """Just enough ledger. `splits` are what the account holds."""

    def __init__(self, splits, *, balance="6073.38", account=ACCOUNT, opening="12612.64"):
        self.splits = splits
        self.balance = balance
        self.account = account
        self.opening = opening

    def asset_accounts(self):
        return [
            {
                "name": self.account,
                "current_balance": self.balance,
                "opening_balance": self.opening,
                "opening_on": date(2026, 5, 6),
                "currency": "INR",
            }
        ]

    def account_transactions(self, account):
        assert account == self.account
        return self.splits


def row(external_id, amount="10.00", *, slug="canara-1111"):
    """A stored row. Ids are namespaced; `slug=None` covers the pre-migration
    form, which reads must still tolerate."""
    return {
        "kind": "withdrawal",
        "amount": amount,
        "external_id": f"{slug}-{external_id}" if slug else external_id,
    }


@pytest.fixture
def archive(tmp_path):
    """One archived statement: the 93-row fixture."""
    import shutil

    folder = tmp_path / "archive" / "2026-08"
    folder.mkdir(parents=True)
    shutil.copy(XLS_FIXTURE, folder / "statement.xls")
    return tmp_path / "archive"


@pytest.fixture
def settings():
    """The account under test, as a registry entry.

    `verify_ledger` takes an `Account` since §21.6 — every check is scoped to one
    account, because a ledger holding two would otherwise report each one's rows
    as missing from the other. The account number must match the fixture's or
    `statements_for` correctly filters everything out.
    """
    return Account(
        slug="canara-1111",
        bank="canara",
        account_number=FIXTURE_ACCOUNT,
        asset_account=ACCOUNT,
    )


def closing_balance(archive) -> Decimal:
    """The fixture's own closing figure, read through the parser.

    Not hardcoded: §16.6 — every number asserted anywhere comes from
    `tests/fixtures/statement.xls`, and the first version of this file hardcoded
    the *real* ledger's closing figure, which the fixture does not close at.
    """
    statements = service.archived_statements(archive)
    newest = max(statements, key=lambda s: (s.meta.period_to, s.path.stat().st_mtime))
    return newest.meta.closing_balance


def running_balance_after(archive, rows: int) -> Decimal:
    """What the account would hold after only the first `rows` transactions —
    the coherent-but-wrong balance an interrupted re-push leaves behind."""
    statements = service.archived_statements(archive)
    statement = statements[0]
    total = statement.meta.opening_balance
    for txn in statement.transactions[:rows]:
        total += (txn.credit or 0) - (txn.debit or 0)
    return total


def verdict_for(archive, settings, splits, *, balance=None, **kwargs):
    if balance is None:
        balance = str(closing_balance(archive))
    return service.verify_ledger(
        FakeLedger(splits, balance=balance, **kwargs), settings, archive
    )


def check_named(verdict, name):
    return next(c for c in verdict.checks if c.name == name)


def all_ids(archive):
    return [t.txn_id for s in service.archived_statements(archive) for t in s.transactions]


# --- the incident -----------------------------------------------------------


def test_it_catches_the_2026_08_11_state(archive, settings):
    """21 of 93 rows, with a balance that is internally consistent.

    This is the state the ledger actually sat in. Every check that existed at the
    time passed. Both of the two that matter here must fail, and say by how much.
    """
    ids = all_ids(archive)
    assert len(ids) == 93
    surviving = ids[:21]

    # The balance an interrupted re-push actually leaves: coherent, and wrong.
    stalled = running_balance_after(archive, 21)
    verdict = verdict_for(
        archive,
        settings,
        [row(i) for i in surviving],
        balance=str(stalled)
    )

    assert verdict.ok is False
    rows = check_named(verdict, "rows")
    assert rows.ok is False
    assert "72 archived row(s) MISSING" in rows.detail
    assert surviving[0] not in rows.detail, "the SURVIVORS are not the missing ones"
    assert ids[21] in rows.detail, "the first missing id should be named"

    balance = check_named(verdict, "balance")
    assert balance.ok is False
    # Loud and specific: the drift is stated, signed, not merely "mismatch".
    assert str(stalled) in balance.detail
    assert str(closing_balance(archive)) in balance.detail
    assert "out by" in balance.detail
    assert f"{stalled - closing_balance(archive):+}" in balance.detail


def test_a_healthy_ledger_passes_every_check(archive, settings):
    verdict = verdict_for(
        archive,
        settings,
        [row(i) for i in all_ids(archive)]
    )
    assert verdict.ok is True
    assert verdict.unchecked == []
    # Four, not six. Two of the six were about the store rather than about the
    # ledger — soft-deleted journals waiting to be force-purged, and a purge
    # left half-finished — and neither state exists any more.
    assert verdict.headline == "all 4 checks passed"
    assert all(c.ok is True for c in verdict.checks)


# --- each check, on its own --------------------------------------------------


def test_extra_rows_are_reported_as_well_as_missing_ones(archive, settings):
    """A row in the ledger with no statement behind it is also a defect — it means
    a statement was archived away, or something else pushed into the account."""
    verdict = verdict_for(
        archive,
        settings,
        [row(i) for i in all_ids(archive)] + [row("99999999999999")]
    )
    rows = check_named(verdict, "rows")
    assert rows.ok is False
    assert "1 row(s) in the ledger with no statement" in rows.detail
    assert "99999999999999" in rows.detail


def test_an_account_that_opens_at_zero_is_loud_about_the_consequence(archive, settings):
    """A registered account whose opening balance was never set. Every figure on
    it is short by that amount, and the balance can never equal the bank's — and
    it was caught exactly this way, on an account with no rows in it yet."""
    verdict = verdict_for(
        archive, settings, [row(i) for i in all_ids(archive)], opening="0.00"
    )
    check = check_named(verdict, "opening balance")
    assert check.ok is False
    assert "short by that amount" in check.detail


def test_an_opening_balance_that_is_set_passes_and_says_when(archive, settings):
    """A column, so it cannot be purged, duplicated, or acquire an id — three
    ways this used to be able to go wrong when it was a row."""
    verdict = verdict_for(archive, settings, [row(i) for i in all_ids(archive)])
    check = check_named(verdict, "opening balance")
    assert check.ok is True
    assert "2026-05-06" in check.detail


def test_a_wrong_account_name_fails_before_anything_else(archive, settings):
    verdict = service.verify_ledger(
        FakeLedger([], account="Some Other Account"), settings, archive
    )
    assert verdict.ok is False
    assert verdict.checks[0].name == "account"


def test_an_empty_archive_cannot_be_compared_and_says_so(tmp_path, settings):
    (tmp_path / "archive").mkdir()
    verdict = service.verify_ledger(
        FakeLedger([], balance="0.00"), settings, tmp_path / "archive"
    )
    assert check_named(verdict, "balance").ok is None
    assert check_named(verdict, "rows").ok is None
    assert verdict.ok is True, "nothing to compare is not a failure"
    assert verdict.unchecked, "an empty archive leaves checks unchecked"


def test_the_balance_is_compared_against_the_NEWEST_statement(archive, settings, tmp_path):
    """Two overlapping statements archive together; the ledger's balance can only
    match the newest one's closing figure."""
    import shutil

    shutil.copy(XLS_FIXTURE, archive / "2026-08" / "older.xls")
    verdict = verdict_for(
        archive, settings, [row(i) for i in all_ids(archive)]
    )
    # Both files are the same fixture, so the closing balance agrees either way;
    # what matters is that exactly one statement is named.
    assert check_named(verdict, "balance").detail.count(".xls") == 1


def test_ops_still_cannot_execute_anything_but_rclone():
    """The web container must never gain the ability to shell out to anything
    but rclone: it listens on a port and parses uploads, and the Docker socket
    would turn a web compromise into a host compromise."""
    import ast

    source = (Path(__file__).resolve().parent.parent / "src/passbook/ops.py").read_text()
    executables = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"run", "check_output", "Popen", "call"}:
                first = node.args[0] if node.args else None
                if isinstance(first, ast.List) and first.elts:
                    head = first.elts[0]
                    if isinstance(head, ast.Constant):
                        executables.add(head.value)
    assert executables <= {"rclone"}, f"ops.py can execute {executables}"


# --- the check that could not count -----------------------------------------


def test_a_transaction_stored_twice_fails_the_rows_check(archive, settings):
    """The incident this check missed.

    `rows` compared `set(live) == set(archived)`, so a row written twice was
    invisible to it: the ledger held more rows than the archive had
    transactions and the check said "one per archived transaction", in green.
    The balance check caught it; this one told the operator everything was
    fine, which is the half that decides where they look.

    Non-negotiable 11, literally: a set says which ids are present, only a
    count says how many times.

    The state is now unwritable — `external_id` is the primary key — and the
    check stays, because a check that cannot fail is the cheapest possible
    evidence that the guarantee is still the one being relied on.
    """
    ids = all_ids(archive)
    doubled = ids[:7]
    verdict = verdict_for(
        archive,
        settings,
        [row(i) for i in ids] + [row(i) for i in doubled]
    )

    rows = check_named(verdict, "rows")
    assert rows.ok is False
    assert "7 transaction(s) are in the ledger MORE THAN ONCE" in rows.detail
    assert "7 extra row(s)" in rows.detail
    assert doubled[0] in rows.detail
    # It names what to look at and does not repair it (non-negotiable 12).
    assert "primary key" in rows.detail
    assert verdict.ok is False


def test_the_rows_check_counts_splits_not_identities(archive, settings):
    """The pass message must state what was counted, not what was distinct."""
    ids = all_ids(archive)
    verdict = verdict_for(
        archive, settings, [row(i) for i in ids]
    )
    rows = check_named(verdict, "rows")
    assert rows.ok is True
    assert rows.detail.startswith(f"{len(ids)} rows")


def test_a_row_duplicated_across_the_namespace_migration_is_still_one_row(
    archive, settings
):
    """Bare id plus namespaced id for the same transaction is a duplicate.

    This is the shape a migration run twice would leave, and the tolerant read
    (§21.1) is exactly what lets the check see through it.
    """
    ids = all_ids(archive)
    account = settings if isinstance(settings, Account) else Account(
        slug="canara-1111",
        bank="canara",
        account_number=getattr(settings, "passbook_account_number", "1111"),
        asset_account=getattr(settings, "passbook_asset_account", ""),
    )
    splits = [row(account.external_id(i)) for i in ids] + [row(ids[0])]
    verdict = service.verify_ledger(
        FakeLedger(splits, balance=str(closing_balance(archive))),
        account,
        archive
    )
    rows = check_named(verdict, "rows")
    assert rows.ok is False
    assert "1 transaction(s) are in the ledger MORE THAN ONCE" in rows.detail
