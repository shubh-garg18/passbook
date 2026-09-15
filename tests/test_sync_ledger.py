"""In-place reconciliation of the ledger with config. SPEC §23.

The regression that made this phase necessary is the first test in the file:
`reapply_preview` keyed the live rows on the namespaced `external_id` and
looked them up with the bank's bare `txn_id`, so it matched **nothing**. On the
reference ledger that is 0 of 113 rows compared, reported as "All 0
transactions already match. Nothing to do." — a green answer to a question that
was never asked (non-negotiable 10 and 11 in one).
"""

import shutil
from decimal import Decimal

import pytest

from passbook import service
from passbook.config import Account, Settings
from passbook.push import build_split
from passbook.store import LedgerError

from conftest import FIXTURE_ACCOUNT, FIXTURE_TXN_COUNT, XLS_FIXTURE

ASSET = "Canara Savings"
ACCOUNT = Account(
    slug="canara-1111", bank="canara", account_number=FIXTURE_ACCOUNT, asset_account=ASSET
)


@pytest.fixture
def archive(tmp_path):
    folder = tmp_path / "archive" / ACCOUNT.slug / "2026-08"
    folder.mkdir(parents=True)
    shutil.copy(XLS_FIXTURE, folder / "statement.xls")
    return tmp_path / "archive"


@pytest.fixture
def settings():
    return Settings(
        passbook_account_number=FIXTURE_ACCOUNT,
        passbook_asset_account=ASSET,
    )


class FakeLedger:
    """One asset account whose rows are built from the fixture, then dirtied."""

    def __init__(self, *, namespaced=True, dirty=None, account=ASSET):
        self.account = account
        self.updates: list[tuple[str, dict]] = []
        self.fail_on: set[str] = set()
        parsed = service.parse_statement(XLS_FIXTURE)
        self.rows = []
        for txn in parsed.transactions:
            split = build_split(txn, ACCOUNT if namespaced else ASSET)
            split["category"] = None
            split["tags"] = []
            if dirty:
                dirty(split)
            self.rows.append(split)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        return None

    def asset_accounts(self):
        return [{"name": self.account, "current_balance": Decimal("0.00"), "currency": "INR"}]

    def account_transactions(self, account):
        assert account == self.account
        return self.rows

    def identities(self, account):
        return {r["external_id"] for r in self.rows}

    def update_transaction(self, external_id, fields):
        if str(external_id) in self.fail_on:
            raise LedgerError("the ledger refused the write")
        self.updates.append((str(external_id), fields))


# --- the join ---------------------------------------------------------------


@pytest.mark.parametrize("namespaced", [False, True])
def test_the_preview_matches_rows_in_either_external_id_form(
    namespaced, archive, settings, monkeypatch
):
    """§21.1. Every write since Phase 14 is namespaced; both forms must match.

    Parametrised rather than written once, because the bug was invisible in the
    only form the old test data used.
    """
    monkeypatch.chdir(archive.parent)
    client = FakeLedger(namespaced=namespaced)

    changes, considered = service.reapply_preview(
        client, settings, archive, aliases={}, rules={}, accounts=[ACCOUNT]
    )

    assert considered == FIXTURE_TXN_COUNT, "rows were compared, not skipped"
    assert changes == [], "nothing in config, so nothing should differ"


def test_a_bare_id_ledger_is_still_matched_after_the_namespace_migration(
    archive, settings, monkeypatch
):
    """A ledger holding pre-migration rows keeps working. §21.1."""
    monkeypatch.chdir(archive.parent)
    client = FakeLedger(namespaced=False)

    _, considered = service.reapply_preview(
        client, settings, archive, aliases={}, rules={}, accounts=[ACCOUNT]
    )
    assert considered == FIXTURE_TXN_COUNT


def test_a_row_absent_from_the_ledger_is_not_reported_as_a_change(
    archive, settings, monkeypatch
):
    monkeypatch.chdir(archive.parent)
    client = FakeLedger()
    client.rows = client.rows[:10]

    changes, considered = service.reapply_preview(
        client, settings, archive, aliases={}, rules={}, accounts=[ACCOUNT]
    )
    assert considered == 10
    assert all(c.group_id for c in changes)


# --- what counts as drift ---------------------------------------------------


def _rename_all(split):
    split["description"] = "OLD NAME (UPI)"
    split["counterparty"] = "OLD NAME"


def test_a_stale_description_category_and_counterparty_are_all_reported(
    archive, settings, monkeypatch
):
    monkeypatch.chdir(archive.parent)
    client = FakeLedger(dirty=_rename_all)

    changes, considered = service.reapply_preview(
        client, settings, archive, aliases={}, rules={}, accounts=[ACCOUNT]
    )

    assert considered == FIXTURE_TXN_COUNT
    assert len(changes) == FIXTURE_TXN_COUNT
    one = changes[0]
    assert one.name_changed and one.counterparty_changed
    assert one.old_description == "OLD NAME (UPI)"
    assert one.old_counterparty == "OLD NAME"
    assert one.external_id


def test_the_counterparty_is_a_column_rather_than_a_side(
    archive, settings, monkeypatch
):
    """It used to be whichever of source/destination the direction did not use,
    and reading the wrong one reported every deposit as renaming the asset
    account itself. A deposit's payee is now just its `counterparty`."""
    monkeypatch.chdir(archive.parent)
    client = FakeLedger()

    deposits = [r for r in client.rows if r["kind"] == "deposit"]
    assert deposits, "fixture must contain a deposit for this to mean anything"
    for split in deposits:
        assert service._counterparty(split) == split["counterparty"]
        assert service._counterparty(split) != ASSET


# --- tags -------------------------------------------------------------------

RULES = {
    "rules": [
        {"title": "Salary", "category": "Salary", "payees": ["Salary", "Freelance"]},
        {"title": "Mother", "category": "Mother", "tag": "family", "payees": ["Mother"]},
    ],
    "not_earnings": {
        "title": "Not earnings",
        "tag": "not-earnings",
        "earnings_only": ["Salary"],
    },
}


def test_managed_tags_are_only_the_ones_config_can_move():
    assert service.managed_tags(RULES) == {"family", "not-earnings"}
    # `reversal` is the pusher's and `large-oneoff` is the rules engine's.
    assert "reversal" not in service.managed_tags(RULES)
    assert "large-oneoff" not in service.managed_tags(RULES)


@pytest.mark.parametrize(
    "description,kind,expected",
    [
        ("Salary (IMPS)", "deposit", set()),
        ("Freelance (NEFT)", "deposit", {"not-earnings"}),
        ("Mother (UPI)", "deposit", {"family", "not-earnings"}),
        ("Mother (UPI)", "withdrawal", {"family"}),
    ],
)
def test_predicted_tags_mirror_the_rules(description, kind, expected):
    """`not_earnings` is inverted and can never land on a withdrawal (§8.1)."""
    assert service.predict_tags(description, "", kind, RULES) == expected


def test_an_unmanaged_tag_survives_a_sync(archive, settings, monkeypatch):
    """`reversal` is a parser-derived fact. A sync must carry it through."""
    monkeypatch.chdir(archive.parent)

    def dirty(split):
        split["description"] = "OLD NAME (UPI)"
        split["tags"] = ["reversal", "large-oneoff"]

    client = FakeLedger(dirty=dirty)
    changes, _ = service.reapply_preview(
        client, settings, archive, aliases={}, rules=RULES, accounts=[ACCOUNT]
    )
    assert changes
    for change in changes:
        assert "reversal" in change.new_tags
        assert "large-oneoff" in change.new_tags


# --- the write --------------------------------------------------------------


def _change(**kw):
    base = dict(
        external_id="canara-1111-20260509000001",
        date="2026-05-09",
        amount=Decimal("100.00"),
        old_description="OLD (UPI)",
        new_description="New (UPI)",
        old_category="",
        new_category="",
        old_counterparty="OLD",
        new_counterparty="OLD",
        old_tags=(),
        new_tags=(),
        kind="withdrawal",
    )
    return service.ReapplyChange(**{**base, **kw})


def test_the_update_is_sparse_and_never_carries_money_or_a_date():
    """The reason a rename cannot corrupt a ledger: those fields are not sent.

    It used to be safe because the other store's update happened to be sparse.
    The store now refuses the money-carrying fields outright, and a test that
    only checked they were absent would no longer be checking the guarantee —
    so `test_store.py` asserts the refusal and this asserts the request.
    """
    client = FakeLedger()
    result = service.sync_ledger(client, [_change()])

    assert result.updated == 1 and result.ok
    (external_id, fields), = client.updates
    assert external_id == "canara-1111-20260509000001"
    assert fields == {"description": "New (UPI)"}
    for forbidden in ("amount", "txn_date", "kind", "external_id", "notes", "account"):
        assert forbidden not in fields


def test_a_category_a_counterparty_and_tags_are_sent_only_when_they_move():
    client = FakeLedger()
    service.sync_ledger(
        client,
        [
            _change(
                new_category="Salary",
                new_counterparty="New",
                old_tags=("reversal",),
                new_tags=("reversal", "family"),
            )
        ],
    )
    fields = client.updates[0][1]
    assert fields["category"] == "Salary"
    assert fields["counterparty"] == "New"
    # The whole list: tags are replaced rather than appended, so an omitted tag
    # is a removed tag.
    assert fields["tags"] == ["reversal", "family"]


def test_a_deposit_writes_the_same_counterparty_field_as_a_withdrawal():
    """One column, so the direction no longer decides which key to send — which
    is one whole class of bug that cannot be written any more."""
    client = FakeLedger()
    service.sync_ledger(client, [_change(kind="deposit", new_counterparty="New")])
    assert client.updates[0][1]["counterparty"] == "New"


def test_clearing_a_category_sends_an_empty_string_not_a_missing_key():
    """An empty category clears it rather than leaving the old one in place. No
    category named "" is invented — nothing creates a category from a row."""
    client = FakeLedger()
    service.sync_ledger(client, [_change(old_category="Salary", new_category="")])
    assert client.updates[0][1]["category"] == ""


def test_a_change_that_never_matched_a_row_is_refused_not_guessed():
    """The alternative is a write to an address nobody looked up."""
    client = FakeLedger()
    result = service.sync_ledger(client, [_change(external_id="")])

    assert client.updates == []
    assert result.updated == 0 and result.failed == 1 and not result.ok
    assert "not matched" in result.failures[0][1]


def test_a_failed_row_is_counted_and_named_and_the_rest_still_run():
    client = FakeLedger()
    client.fail_on = {"canara-1111-20260509000001"}
    result = service.sync_ledger(
        client, [_change(), _change(external_id="canara-1111-20260509000002")]
    )

    assert result.updated == 1 and result.failed == 1
    assert result.failures[0][0] == "canara-1111-20260509000001"
    assert not result.ok


def test_syncing_twice_is_the_same_as_syncing_once(archive, settings, monkeypatch):
    """Idempotent, which is what lets it run without a dump gate."""
    monkeypatch.chdir(archive.parent)
    client = FakeLedger(dirty=_rename_all)

    changes, _ = service.reapply_preview(
        client, settings, archive, aliases={}, rules={}, accounts=[ACCOUNT]
    )
    first = service.sync_ledger(client, changes)

    # Apply what the sync wrote back onto the fake ledger, then re-preview.
    by_id = {r["external_id"]: r for r in client.rows}
    for external_id, fields in client.updates:
        by_id[external_id].update(fields)
    client.updates.clear()

    again, _ = service.reapply_preview(
        client, settings, archive, aliases={}, rules={}, accounts=[ACCOUNT]
    )
    assert first.updated == FIXTURE_TXN_COUNT
    assert again == [], "a second run has nothing left to do"


# --- zero compared is not zero differing ------------------------------------


def test_an_asset_account_the_ledger_does_not_have_compares_nothing(
    archive, settings, monkeypatch
):
    """And the callers must not render that as a pass. §23.1.

    The service reports 0; the guard lives in every reader — the page, the
    summary line and the CLI all treat `considered == 0` as an unanswered
    question rather than a clean bill of health.
    """
    monkeypatch.chdir(archive.parent)
    client = FakeLedger(account="Some Other Account")

    changes, considered = service.reapply_preview(
        client, settings, archive, aliases={}, rules={}, accounts=[ACCOUNT]
    )
    assert (changes, considered) == ([], 0)
