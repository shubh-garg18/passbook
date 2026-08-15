"""Ledger integrity, and the purge intent that makes an interruption visible.

SPEC §19, §20. On 2026-08-11 a purge completed and the re-push stopped after 21
of 93 rows. The result was a ledger with a **self-consistent balance and no error
anywhere**, and it stayed that way for seven hours while 349 tests, `doctor`,
`make check` and the status strip all passed.

The first test here is the one that matters: it reconstructs that exact state and
asserts the check catches it, naming what is missing and by how much. Everything
else guards a way of getting it wrong.

No network: `verify_ledger` takes a client, and a fake one is enough.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import FIXTURE_ACCOUNT, XLS_FIXTURE
from passbook import ops, service
from passbook.config import Account

ACCOUNT = "Test Account"


class FakeFirefly:
    """Just enough Firefly. `splits` are what the account holds."""

    def __init__(self, splits, *, balance="5068.09", account=ACCOUNT):
        self.splits = splits
        self.balance = balance
        self.account = account

    def asset_accounts(self):
        return [
            {"id": "7", "attributes": {"name": self.account, "current_balance": self.balance}}
        ]

    def account_transactions(self, account_id):
        assert account_id == "7"
        return [{"attributes": {"transactions": self.splits}}]


def opening(amount="10000.00"):
    return {"type": "opening balance", "amount": amount, "external_id": None}


def row(external_id, amount="10.00", *, slug="canara-1111"):
    """A live split. Ids are namespaced (§21.1); `bare=` covers the pre-migration
    form, which reads must still tolerate."""
    return {
        "type": "withdrawal",
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
    `tests/fixtures/statement.xls`. An early version of this file hardcoded a
    live ledger's closing figure, which the fixture does not close at.
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
        FakeFirefly(splits, balance=balance), settings, archive, **kwargs
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
        [opening()] + [row(i) for i in surviving],
        balance=str(stalled),
        trashed=0,
        intents=[],
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
        [opening()] + [row(i) for i in all_ids(archive)],
        trashed=0,
        intents=[],
    )
    assert verdict.ok is True
    assert verdict.unchecked == []
    assert verdict.headline == "all 6 checks passed"
    assert all(c.ok is True for c in verdict.checks)


# --- each check, on its own --------------------------------------------------


def test_extra_rows_are_reported_as_well_as_missing_ones(archive, settings):
    """A row in Firefly with no statement behind it is also a defect — it means
    a statement was archived away, or something else pushed into the account."""
    verdict = verdict_for(
        archive,
        settings,
        [opening()] + [row(i) for i in all_ids(archive)] + [row("99999999999999")],
        trashed=0,
        intents=[],
    )
    rows = check_named(verdict, "rows")
    assert rows.ok is False
    assert "1 row(s) in Firefly with no statement" in rows.detail
    assert "99999999999999" in rows.detail


def test_tombstones_fail_the_check_and_name_the_remedy(archive, settings):
    verdict = verdict_for(
        archive, settings, [opening()] + [row(i) for i in all_ids(archive)],
        trashed=72, intents=[],
    )
    trashed = check_named(verdict, "trashed")
    assert trashed.ok is False
    assert "72 soft-deleted" in trashed.detail
    assert "purge --confirm --yes" in trashed.detail


def test_an_unavailable_tombstone_count_is_UNCHECKED_never_a_pass(archive, settings):
    """The web container has no database credentials and Firefly's API cannot
    list trashed journals — verified against the pinned tag, `routes/api.php`
    exposes only `data/destroy` and `data/purge`. Reporting a tick for that would
    be the §19 failure in miniature: a green light for something never looked at.
    """
    verdict = verdict_for(
        archive, settings, [opening()] + [row(i) for i in all_ids(archive)], intents=[]
    )
    trashed = check_named(verdict, "trashed")
    assert trashed.ok is None
    assert trashed not in verdict.failed
    assert trashed in verdict.unchecked
    # Unchecked does not make the verdict false, and it does not hide either.
    assert verdict.ok is True
    assert verdict.headline == "5 of 6 checks passed"


def test_an_unfinished_purge_fails_the_check(archive, settings):
    verdict = verdict_for(
        archive, settings, [opening()] + [row(i) for i in all_ids(archive)],
        trashed=0, intents=["purge-intent-20260811-035417.json"],
    )
    intent = check_named(verdict, "purge intent")
    assert intent.ok is False
    assert "purge --resume" in intent.detail


def test_a_missing_opening_balance_is_loud_about_the_consequence(archive, settings):
    verdict = verdict_for(
        archive, settings, [row(i) for i in all_ids(archive)], trashed=0, intents=[]
    )
    check = check_named(verdict, "opening balance")
    assert check.ok is False
    assert "MISSING" in check.detail


def test_an_opening_balance_with_an_external_id_fails(archive, settings):
    """It would then be a purge candidate, and `purge`'s exclusion is structural
    precisely because the opening balance carries no external_id (§7.3)."""
    tainted = dict(opening())
    tainted["external_id"] = "20260507000000"
    verdict = verdict_for(
        archive, settings, [tainted] + [row(i) for i in all_ids(archive)],
        trashed=0, intents=[],
    )
    check = check_named(verdict, "opening balance")
    assert check.ok is False
    assert "external_id" in check.detail


def test_a_wrong_account_name_fails_before_anything_else(archive, settings):
    verdict = service.verify_ledger(
        FakeFirefly([], account="Some Other Account"), settings, archive
    )
    assert verdict.ok is False
    assert verdict.checks[0].name == "account"


def test_an_empty_archive_cannot_be_compared_and_says_so(tmp_path, settings):
    (tmp_path / "archive").mkdir()
    verdict = service.verify_ledger(
        FakeFirefly([opening()], balance="0.00"), settings, tmp_path / "archive",
        trashed=0, intents=[],
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
        archive, settings, [opening()] + [row(i) for i in all_ids(archive)],
        trashed=0, intents=[],
    )
    # Both files are the same fixture, so the closing balance agrees either way;
    # what matters is that exactly one statement is named.
    assert check_named(verdict, "balance").detail.count(".xls") == 1


# --- purge intent -----------------------------------------------------------


def test_intent_is_written_before_deleting_and_says_what_to_restore(tmp_path):
    path = ops.write_purge_intent(
        ACCOUNT, ["1", "2", "3"], ["archive/2026-08/statement.xls"], backups=tmp_path
    )
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["stage"] == "purging"
    assert data["expected_rows"] == 3
    assert data["statements"] == ["archive/2026-08/statement.xls"]
    assert data["account"] == ACCOUNT
    # 600: it names an account and a set of transaction ids.
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_an_unfinished_intent_is_outstanding_and_a_done_one_is_not(tmp_path):
    path = ops.write_purge_intent(ACCOUNT, ["1"], [], backups=tmp_path)
    assert ops.outstanding_purge_intents(tmp_path) == [path]

    ops.update_purge_intent(path, stage="purged")
    assert ops.outstanding_purge_intents(tmp_path) == [path], "purged is not finished"

    ops.update_purge_intent(path, stage="done")
    assert ops.outstanding_purge_intents(tmp_path) == []


def test_an_unreadable_intent_counts_as_outstanding(tmp_path):
    """It was being written when something stopped — the one interpretation that
    must never be "probably fine"."""
    broken = tmp_path / "purge-intent-20260811-035417.json"
    broken.write_text("{ truncated")
    assert ops.outstanding_purge_intents(tmp_path) == [broken]


def test_clearing_removes_the_file(tmp_path):
    path = ops.write_purge_intent(ACCOUNT, ["1"], [], backups=tmp_path)
    ops.clear_purge_intent(path)
    assert not path.exists()
    assert ops.outstanding_purge_intents(tmp_path) == []


def test_purge_records_intent_even_when_the_caller_forgets(tmp_path, monkeypatch):
    """`purge()` writes its own intent if none was passed. The guarantee has to
    be in the function that deletes, not in the discipline of its callers."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "backups").mkdir()

    from passbook.firefly import purge as purge_module

    class Client:
        def delete_transaction(self, group_id):
            return None

        def purge_trashed(self):
            return None

    candidates = [
        purge_module.Candidate(
            group_id="1", external_id="20260509000001", date="2026-05-09",
            description="x", amount=Decimal("1.00"),
        )
    ]
    result = purge_module.purge(Client(), candidates, account=ACCOUNT)
    assert result.intent is not None and result.intent.exists()
    data = json.loads(result.intent.read_text())
    assert data["stage"] == "purged", "advanced only after the force-delete ran"
    assert data["external_ids"] == ["20260509000001"]
    assert result.hard_purged is True


def test_the_intent_stays_at_purging_if_the_force_delete_never_ran(tmp_path, monkeypatch):
    """No deletes, no force-purge, so nothing is claimed. An intent still reading
    `purging` is what tells the next re-push that tombstones may remain."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "backups").mkdir()
    from passbook.firefly import purge as purge_module

    class Client:
        def purge_trashed(self):  # pragma: no cover - must not be called
            raise AssertionError("force-purge ran with nothing deleted")

    result = purge_module.purge(Client(), [], account=ACCOUNT)
    assert result.hard_purged is False
    assert json.loads(result.intent.read_text())["stage"] == "purging"


def test_ops_still_cannot_execute_anything_but_rclone():
    """The intent file is deliberately plain file I/O. Counting tombstones needs
    the database and therefore `docker compose exec`, which lives in the CLI —
    the web container must never gain that ability (§15.1)."""
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
