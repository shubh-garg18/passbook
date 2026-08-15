"""The account registry, and the id collision it exists to survive. SPEC §21.

**The case that must not lose data.** Canara's transaction id is `YYYYMMDD` plus
a per-date ordinal, and the bank sequences it **per account** — so a second
Canara account emits the *same ids*. `tests/fixtures/statement-second.xls` is
built from the same source as `statement.xls` with a different synthetic account
number that deliberately **shares its last four digits**, so it collides two ways
at once:

* all 93 transaction ids are identical;
* the masked account (`****1111`) is identical.

Every test here is about one of those two collisions not turning into silence.
"""

from __future__ import annotations

import shutil

import pytest

from conftest import FIXTURE_ACCOUNT, SECOND_FIXTURE, XLS_FIXTURE
from passbook import service
from passbook.config import (
    Account,
    RegistryError,
    default_slug,
    load_accounts,
    parse_accounts,
    save_accounts,
)
from passbook.loaders import xls
from passbook.validate import UnknownAccount

SECOND_ACCOUNT = "888800001111"


def account(slug="canara-1111", number=FIXTURE_ACCOUNT, asset="First Asset", bank="canara"):
    return Account(slug=slug, bank=bank, account_number=number, asset_account=asset)


@pytest.fixture
def two_accounts():
    return [
        account(),
        account(slug="canara-1111-2", number=SECOND_ACCOUNT, asset="Second Asset"),
    ]


@pytest.fixture
def archive(tmp_path):
    """Both statements, in one archive, as a real two-account install would."""
    folder = tmp_path / "archive" / "2026-08"
    folder.mkdir(parents=True)
    shutil.copy(XLS_FIXTURE, folder / "first.xls")
    shutil.copy(SECOND_FIXTURE, folder / "second.xls")
    return tmp_path / "archive"


# --- the collision itself ----------------------------------------------------


def test_the_two_fixtures_really_do_collide():
    """If this ever stops being true the rest of the file proves nothing."""
    first_meta, first = xls.load(XLS_FIXTURE)
    second_meta, second = xls.load(SECOND_FIXTURE)

    assert first_meta.account_number != second_meta.account_number
    assert first_meta.masked_account == second_meta.masked_account == "****1111"
    ids_first = {t.txn_id for t in first}
    ids_second = {t.txn_id for t in second}
    assert ids_first == ids_second, "the ids must collide completely"
    assert len(ids_first) == 93


def test_namespacing_makes_colliding_ids_unique(two_accounts):
    first, second = two_accounts
    _, transactions = xls.load(XLS_FIXTURE)
    ids = {first.external_id(t.txn_id) for t in transactions} | {
        second.external_id(t.txn_id) for t in transactions
    }
    assert len(ids) == 186, "93 rows per account, none colliding"
    assert first.external_id("20260509000001") == "canara-1111-20260509000001"
    assert second.external_id("20260509000001") == "canara-1111-2-20260509000001"


def test_a_naive_merge_on_txn_id_loses_an_entire_account(archive, two_accounts):
    """The failure this phase exists to prevent, measured.

    Deduping across accounts on the bank's id keeps 93 of 186 rows and reports
    success — no error, no warning, half the ledger gone.
    """
    statements = service.archived_statements(archive)
    naive = {t.txn_id: t for s in statements for t in s.transactions}
    assert len(naive) == 93, "the naive merge silently halves it"

    kept = sum(len(service.account_transactions(a, archive)) for a in two_accounts)
    assert kept == 186, "account-scoped, nothing is lost"


def test_each_account_gets_only_its_own_statements(archive, two_accounts):
    first, second = two_accounts
    statements = service.archived_statements(archive)
    assert len(service.statements_for(first, statements)) == 1
    assert len(service.statements_for(second, statements)) == 1
    # Filtered on what the STATEMENT says, not on where the file sits: a file
    # moved by hand into the wrong folder must not change whose ledger it joins.
    assert service.statements_for(first, statements)[0].meta.account_number == FIXTURE_ACCOUNT


def test_the_clock_is_never_joined_across_accounts(archive, two_accounts):
    """`transaction_times` is keyed on the bank's id, so mixing accounts would
    attach one account's clock to the other's transaction."""
    first, second = two_accounts
    statements = service.archived_statements(archive)
    times_first = service.transaction_times(service.statements_for(first, statements))
    times_second = service.transaction_times(service.statements_for(second, statements))
    assert len(times_first) == len(times_second) == 93
    # Same keys (they collide!) but each map came from one account only.
    assert set(times_first) == set(times_second)


# --- routing -----------------------------------------------------------------


def test_routing_matches_on_the_full_number_not_the_mask(two_accounts):
    """Both accounts mask to ****1111. Matching on the mask would route half the
    statements into the wrong ledger — silently, and irreversibly once pushed."""
    first_meta, _ = xls.load(XLS_FIXTURE)
    second_meta, _ = xls.load(SECOND_FIXTURE)

    assert service.route_statement(first_meta, two_accounts).slug == "canara-1111"
    assert service.route_statement(second_meta, two_accounts).slug == "canara-1111-2"


def test_an_unregistered_account_is_refused_and_says_what_is_known(two_accounts):
    meta, _ = xls.load(XLS_FIXTURE)
    others = [a for a in two_accounts if a.account_number != FIXTURE_ACCOUNT]
    with pytest.raises(UnknownAccount) as caught:
        service.route_statement(meta, others)
    assert caught.value.masked == "****1111"
    assert caught.value.known == ["canara-1111-2"]
    # §11: the message carries the mask, never the full number.
    assert FIXTURE_ACCOUNT not in str(caught.value)


def test_refusing_is_the_default_when_the_registry_is_not_empty(tmp_path, monkeypatch):
    """Auto-registration is for the FIRST account only (§21.3). Account two is an
    explicit act, because it also needs a Firefly asset account chosen — and
    `doctor` has refused to guess between several since §7.2."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    save_accounts([account(number=SECOND_ACCOUNT, slug="canara-1111-2")])
    from passbook.config import Settings

    meta, _ = xls.load(XLS_FIXTURE)
    with pytest.raises(UnknownAccount):
        service.resolve_account(meta, Settings(passbook_asset_account="A"))


def test_the_first_statement_registers_itself(tmp_path, monkeypatch):
    """Zero config: someone with one account never learns this feature exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    from passbook.config import Settings

    meta, _ = xls.load(XLS_FIXTURE)
    resolved = service.resolve_account(
        meta, Settings(passbook_asset_account="Canara savings")
    )
    assert resolved.slug == default_slug("canara", FIXTURE_ACCOUNT) == "canara-1111"
    assert resolved.bank == "canara"
    assert resolved.asset_account == "Canara savings"
    # ...and it persisted, so the second upload routes instead of re-registering.
    assert [a.slug for a in load_accounts()] == ["canara-1111"]
    assert service.resolve_account(meta, Settings(passbook_asset_account="Canara savings")).slug == "canara-1111"


def test_a_colliding_default_slug_is_disambiguated(tmp_path, monkeypatch):
    """Both fixtures default to `canara-1111`. The slug is part of every
    external_id the account will push, so it is unique by construction."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    first_meta, _ = xls.load(XLS_FIXTURE)
    second_meta, _ = xls.load(SECOND_FIXTURE)

    one = service.register_from_statement(first_meta, "First Asset", [])
    two = service.register_from_statement(second_meta, "Second Asset")
    assert one.slug == "canara-1111"
    assert two.slug == "canara-1111-2"
    assert one.external_id("20260509000001") != two.external_id("20260509000001")


# --- the id, read tolerantly and written strictly ---------------------------


@pytest.mark.parametrize(
    "external_id,txn_id,slug",
    [
        ("canara-1111-20260509000001", "20260509000001", "canara-1111"),
        ("canara-1111-2-20260509000001", "20260509000001", "canara-1111-2"),
        ("20260509000001", "20260509000001", None),
        ("", "", None),
        ("nonsense", "nonsense", None),
    ],
)
def test_ids_are_read_tolerantly(external_id, txn_id, slug):
    """Pre-migration rows carry the bank's bare id. Reads accept both forms so
    the migration (§21.2) can be run when it suits, not when a version forces it."""
    assert service.txn_id_of(external_id) == txn_id
    assert service.slug_of(external_id) == slug
    assert service.is_namespaced(external_id) is (slug is not None)


def test_pushes_are_namespaced_but_a_bare_asset_account_still_works():
    from passbook.firefly.push import build_payload

    _, transactions = xls.load(XLS_FIXTURE)
    txn = transactions[0]
    assert build_payload(txn, account())["transactions"][0]["external_id"] == (
        f"canara-1111-{txn.txn_id}"
    )
    # The DR drill passes two env vars into a recovered container and nothing
    # else; that path must keep working.
    legacy = build_payload(txn, "Legacy Asset")["transactions"][0]
    assert legacy["external_id"] == txn.txn_id
    assert legacy["source_name"] == "Legacy Asset"


# --- the registry file -------------------------------------------------------


def test_a_duplicate_slug_is_refused_because_it_would_merge_two_ledgers():
    with pytest.raises(RegistryError, match="share slug"):
        parse_accounts(
            {"accounts": [account().to_dict(), account(number=SECOND_ACCOUNT).to_dict()]}
        )


def test_a_duplicate_account_number_is_refused_and_masked_in_the_message():
    with pytest.raises(RegistryError) as caught:
        parse_accounts(
            {
                "accounts": [
                    account().to_dict(),
                    account(slug="other", asset="Other Asset").to_dict(),
                ]
            }
        )
    assert FIXTURE_ACCOUNT not in str(caught.value), "§11 holds in error paths"
    assert "****1111" in str(caught.value)


def test_two_accounts_cannot_share_one_firefly_asset_account():
    """They would merge in Firefly no matter what the registry said."""
    with pytest.raises(RegistryError, match="share asset_account"):
        parse_accounts(
            {
                "accounts": [
                    account().to_dict(),
                    account(slug="second", number=SECOND_ACCOUNT).to_dict(),
                ]
            }
        )


def test_an_unsupported_bank_is_refused_with_the_reason():
    with pytest.raises(RegistryError, match="not supported"):
        parse_accounts({"accounts": [account(bank="hdfc").to_dict()]})


@pytest.mark.parametrize("slug", ["Canara-1111", "canara_1111", "-canara", "canara 1111", ""])
def test_a_slug_that_cannot_live_in_an_external_id_is_refused(slug):
    with pytest.raises(RegistryError):
        parse_accounts({"accounts": [account(slug=slug).to_dict()]})


def test_the_registry_round_trips_and_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    written = save_accounts([account(), account(slug="s2", number=SECOND_ACCOUNT, asset="A2")])
    assert oct(written.stat().st_mode)[-3:] == "600", "it names real account numbers"
    assert [a.slug for a in load_accounts()] == ["canara-1111", "s2"]
    # The comment block is what stops someone splitting the shared config later.
    text = written.read_text()
    assert "SHARED across" in text and "IMMUTABLE" in text


def test_an_absent_registry_falls_back_to_the_two_env_vars(tmp_path, monkeypatch):
    """The pre-registry install, `make dr-drill`, and every test written before
    this phase. Nothing asks the operator to migrate a file they never had."""
    monkeypatch.chdir(tmp_path)
    from passbook.config import Settings

    accounts = load_accounts(
        settings=Settings(
            passbook_account_number=FIXTURE_ACCOUNT,
            passbook_asset_account="Canara savings",
        )
    )
    assert len(accounts) == 1
    assert accounts[0].slug == "canara-1111"
    assert accounts[0].account_number == FIXTURE_ACCOUNT


def test_no_env_vars_and_no_file_means_no_accounts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from passbook.config import Settings

    assert load_accounts(settings=Settings()) == []


# --- the API, scoped ---------------------------------------------------------
# SPEC §21.9. These use the web test client; Firefly is faked, as everywhere else.


@pytest.fixture
def two_account_app(tmp_path, monkeypatch):
    """An install with both colliding accounts registered and both archived."""
    import shutil as _shutil

    from passbook import webauth
    from passbook.web import create_app
    from passbook.web.auth import SESSION_KEY

    monkeypatch.chdir(tmp_path)
    for name in ("inbox", "archive", "config", "backups"):
        (tmp_path / name).mkdir()
    monkeypatch.delenv("PASSBOOK_ACCOUNT_NUMBER", raising=False)
    monkeypatch.delenv("PASSBOOK_ASSET_ACCOUNT", raising=False)
    monkeypatch.setenv("FIREFLY_TOKEN", "a.b.c")

    save_accounts(
        [
            Account(slug="canara-1111", bank="canara", account_number=FIXTURE_ACCOUNT,
                    asset_account="First Asset", label="First"),
            Account(slug="canara-1111-2", bank="canara", account_number=SECOND_ACCOUNT,
                    asset_account="Second Asset", label="Second"),
        ]
    )
    (tmp_path / "archive" / "canara-1111" / "2026-08").mkdir(parents=True)
    (tmp_path / "archive" / "canara-1111-2" / "2026-08").mkdir(parents=True)
    _shutil.copy(XLS_FIXTURE, tmp_path / "archive" / "canara-1111" / "2026-08" / "s.xls")
    _shutil.copy(SECOND_FIXTURE, tmp_path / "archive" / "canara-1111-2" / "2026-08" / "s.xls")

    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "WEB_AUTH_FIXED": webauth.WebAuth(
                username="operator", password_hash=webauth.hash_password("x" * 16),
                totp_secret="A" * 16, totp_enrolled_at="2026-08-09T00:00:00+00:00",
                salt="0" * 32,
            ),
            "INBOX": tmp_path / "inbox",
            "ARCHIVE": tmp_path / "archive",
        }
    )
    client = app.test_client()
    client.get("/api/session")
    with client.session_transaction() as s:
        s[SESSION_KEY] = "operator"
    return app, client


def _fake_firefly_two(monkeypatch, per_account):
    """Firefly holding `per_account[asset_account] -> [splits]`."""
    from passbook.web import api as api_module

    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def asset_accounts(self):
            return [
                {"id": str(i + 1), "attributes": {"name": name, "current_balance": bal}}
                for i, (name, (bal, _)) in enumerate(per_account.items())
            ]

        def account_transactions(self, account_id):
            name = list(per_account)[int(account_id) - 1]
            return [{"attributes": {"transactions": per_account[name][1]}}]

    monkeypatch.setattr(api_module, "FireflyClient", lambda *a, **k: Fake())


def _split(external_id, amount="10", kind="withdrawal", category="Shopping"):
    return {
        "type": kind,
        "amount": f"{amount}.000000000000",
        "category_name": category,
        "tags": [],
        "external_id": external_id,
        "date": "2026-06-10T00:00:00+05:30",
    }


def test_accounts_endpoint_says_when_the_switcher_should_exist(two_account_app, monkeypatch):
    _, client = two_account_app
    body = client.get("/api/accounts").get_json()
    assert body["multiple"] is True
    assert [a["slug"] for a in body["accounts"]] == ["canara-1111", "canara-1111-2"]
    # Masked, never full (§11).
    assert body["accounts"][0]["account"] == "****1111"
    assert FIXTURE_ACCOUNT not in client.get("/api/accounts").data.decode()


@pytest.fixture
def one_account_client(two_account_app):
    """The same install with the second account removed — the shape almost every
    user has, and the one that must show no switcher at all."""
    _, client = two_account_app
    save_accounts(
        [
            Account(slug="canara-1111", bank="canara", account_number=FIXTURE_ACCOUNT,
                    asset_account="First Asset", label="First"),
        ]
    )
    return client


def test_a_single_account_install_is_told_there_is_no_switcher(one_account_client):
    """The whole of §21.3: someone with one account never learns this exists."""
    body = one_account_client.get("/api/accounts").get_json()
    assert body["multiple"] is False
    assert len(body["accounts"]) == 1


def test_the_analysis_is_scoped_and_all_accounts_combines(two_account_app, monkeypatch):
    _, client = two_account_app
    _fake_firefly_two(
        monkeypatch,
        {
            "First Asset": ("100.00", [_split("canara-1111-20260509000001", "40")]),
            "Second Asset": ("50.00", [_split("canara-1111-2-20260509000001", "25")]),
        },
    )
    first = client.get("/api/analysis?account=canara-1111").get_json()
    second = client.get("/api/analysis?account=canara-1111-2").get_json()
    both = client.get("/api/analysis?account=all").get_json()

    assert first["spend"] == "40.00" and first["accounts"] == ["canara-1111"]
    assert second["spend"] == "25.00"
    # Additive over transactions, so combining cannot change what it means.
    assert both["spend"] == "65.00"
    assert both["selected"] == "all"
    assert both["accounts"] == ["canara-1111", "canara-1111-2"]


def test_the_balance_sums_across_accounts_and_shows_the_parts(two_account_app, monkeypatch):
    """The one figure that does not combine cleanly. It is summed, labelled a sum
    by the client, and the parts travel with it — a sum reconciles against no
    statement, unlike every other balance this app shows."""
    _, client = two_account_app
    _fake_firefly_two(
        monkeypatch,
        {"First Asset": ("100.00", []), "Second Asset": ("50.50", [])},
    )
    one = client.get("/api/overview?account=canara-1111").get_json()
    assert one["balance"] == "100.00" and one["parts"] == []

    both = client.get("/api/overview?account=all").get_json()
    assert both["balance"] == "150.50"
    assert [p["balance"] for p in both["parts"]] == ["100.00", "50.50"]
    assert [p["label"] for p in both["parts"]] == ["First", "Second"]


def test_payees_are_scoped_and_never_deduped_across_accounts(two_account_app):
    """Both statements carry all 93 of the same transaction ids. Scoped, each
    account reports 93; combined, 186 — not 93 with half silently dropped."""
    _, client = two_account_app
    first = client.get("/api/payees?account=canara-1111").get_json()
    second = client.get("/api/payees?account=canara-1111-2").get_json()
    both = client.get("/api/payees?account=all").get_json()
    assert first["total"] == second["total"] == 93
    assert both["total"] == 186


def test_an_unknown_slug_falls_back_rather_than_erroring(two_account_app, monkeypatch):
    """A selection left in a browser after an account is removed must show data,
    not a broken page."""
    _, client = two_account_app
    _fake_firefly_two(monkeypatch, {"First Asset": ("1.00", []), "Second Asset": ("2.00", [])})
    body = client.get("/api/overview?account=nope").get_json()
    assert body["selected"] == "canara-1111"


def test_all_is_not_a_scope_when_only_one_account_exists(one_account_client):
    """Nothing to combine, so `all` resolves to the single account — the UI has no
    switcher to have asked for it in the first place."""
    body = one_account_client.get("/api/accounts?account=all").get_json()
    assert body["selected"] == "canara-1111"
