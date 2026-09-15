"""JSON API and auth. SPEC §16.

Covers the paths that can do damage: upload validation, the §6.7 account
assertion, the config-write diff, and every branch of authentication. No
network — the ledger is mocked wherever a route reaches for it.

**All statement data comes from `tests/fixtures/statement.xls`**, which
`scripts/redact.py` produced from a real export (§11). Nothing here is a
hand-typed row. A hand-typed ledger row is how a demo table ended up asserting
a balance chain that did not close — the invariant this project is built on,
contradicted by its own illustration.
"""

import io
import json
import re
import shutil
from pathlib import Path

import pyotp
import pytest

from datetime import date
from decimal import Decimal

from conftest import CSV_FIXTURE, FIXTURE_ACCOUNT, FIXTURE_TXN_COUNT, XLS_FIXTURE, StoreDouble
from passbook import service, webauth
from passbook.web import create_app
from passbook.web.auth import SESSION_KEY, reset_throttle

USER, PASSWORD = "operator", "correct-horse-battery-staple"
SECRET = pyotp.random_base32()


def make_auth(**overrides) -> webauth.WebAuth:
    auth = webauth.WebAuth(
        username=USER,
        password_hash=webauth.hash_password(PASSWORD),
        totp_secret=SECRET,
        totp_enrolled_at="2026-08-09T00:00:00+00:00",
        salt="0123456789abcdef0123456789abcdef",
    )
    for key, value in overrides.items():
        setattr(auth, key, value)
    return auth


@pytest.fixture(autouse=True)
def _clean_throttle():
    reset_throttle()
    yield
    reset_throttle()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inbox").mkdir()
    (tmp_path / "archive").mkdir()
    (tmp_path / "config").mkdir()
    # The fixture statement is for the synthetic account, so make that the
    # configured one — otherwise every upload is (correctly) refused.
    monkeypatch.setenv("PASSBOOK_ACCOUNT_NUMBER", FIXTURE_ACCOUNT)
    monkeypatch.setenv("PASSBOOK_ASSET_ACCOUNT", "Test Account")

    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            # Credentials are normally re-read from config/web-auth.json every
            # request so a change takes effect at once; tests inject instead.
            "WEB_AUTH_FIXED": make_auth(),
            "INBOX": tmp_path / "inbox",
            "ARCHIVE": tmp_path / "archive",
        }
    )


class Api:
    """Test client that behaves like the real one: JSON in, CSRF header set."""

    def __init__(self, client):
        self.client = client
        client.get("/api/session")  # seeds the pb_csrf cookie

    @property
    def _headers(self):
        cookie = self.client.get_cookie("pb_csrf")
        return {"X-Passbook-CSRF": cookie.value if cookie else ""}

    def get(self, path):
        return self.client.get(f"/api{path}")

    def post(self, path, body=None, headers=None):
        return self.client.post(
            f"/api{path}", json=body if body is not None else {},
            headers={**self._headers, **(headers or {})},
        )

    def upload_to(self, path, data: bytes, name: str):
        return self.client.post(
            f"/api{path}",
            data={"statement": (io.BytesIO(data), name)},
            content_type="multipart/form-data",
            headers=self._headers,
        )

    def put(self, path, body=None):
        return self.client.put(
            f"/api{path}", json=body if body is not None else {}, headers=self._headers
        )

    def patch(self, path, body=None):
        return self.client.patch(
            f"/api{path}", json=body if body is not None else {}, headers=self._headers
        )

    def delete(self, path):
        return self.client.delete(f"/api{path}", headers=self._headers)

    def upload(self, data: bytes, name: str = "statement.xls"):
        return self.client.post(
            "/api/statement",
            data={"statement": (io.BytesIO(data), name)},
            content_type="multipart/form-data",
            headers=self._headers,
        )

    def session_is(self, username):
        with self.client.session_transaction() as s:
            s[SESSION_KEY] = username


@pytest.fixture
def api(app):
    return Api(app.test_client())


@pytest.fixture
def signed_in(api):
    api.session_is(USER)
    return api


def code_now() -> str:
    return pyotp.TOTP(SECRET).now()


# --- the door ---------------------------------------------------------------
# Bound to localhost today; Phase 8 is Tailscale, at which point localhost is
# not the boundary. These assert the door is actually shut.


@pytest.mark.parametrize(
    "path",
    [
        "/overview",
        "/payees",
        "/status",
        "/reapply",
        "/categories",
        "/analysis",
        "/reminder",
        "/reminder.ics",
    ],
)
def test_reads_require_a_session(api, path):
    assert api.get(path).status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/statement/confirm",
        "/payees/diff",
        "/payees/apply",
        "/reapply/run",
        "/reapply/sync",
        "/password",
    ],
)
def test_writes_require_a_session(api, path):
    assert api.post(path).status_code == 401


def test_wrong_password_is_rejected(api):
    r = api.post("/session", {"username": USER, "password": "nope"})
    assert r.status_code == 401
    assert r.get_json()["code"] == "bad_credentials"


def test_wrong_username_is_rejected(api):
    r = api.post("/session", {"username": "someone", "password": PASSWORD})
    assert r.status_code == 401


def test_failure_never_says_which_half_was_wrong(api):
    bad_user = api.post("/session", {"username": "x", "password": PASSWORD}).get_json()
    bad_pass = api.post("/session", {"username": USER, "password": "x"}).get_json()
    assert bad_user == bad_pass


def test_password_alone_does_not_sign_you_in(api):
    """The whole point of a second factor."""
    r = api.post("/session", {"username": USER, "password": PASSWORD})
    assert r.status_code == 200
    assert r.get_json()["stage"] == "totp"
    assert api.get("/overview").status_code == 401


def test_password_then_totp_signs_you_in(api):
    api.post("/session", {"username": USER, "password": PASSWORD})
    r = api.post("/session/totp", {"code": code_now()})
    assert r.status_code == 200
    assert r.get_json()["stage"] == "done"
    assert api.get("/session").get_json()["authenticated"] is True


def test_wrong_totp_is_rejected(api):
    api.post("/session", {"username": USER, "password": PASSWORD})
    r = api.post("/session/totp", {"code": "000000"})
    assert r.status_code == 401
    assert r.get_json()["code"] == "bad_code"
    assert api.get("/session").get_json()["authenticated"] is False


def test_totp_without_the_password_step_is_refused(api):
    """A valid code is not a credential on its own."""
    r = api.post("/session/totp", {"code": code_now()})
    assert r.status_code == 401
    assert r.get_json()["code"] == "expired"


def test_a_totp_code_cannot_be_replayed(app, api):
    api.post("/session", {"username": USER, "password": PASSWORD})
    code = code_now()
    assert api.post("/session/totp", {"code": code}).status_code == 200
    api.delete("/session")

    api.post("/session", {"username": USER, "password": PASSWORD})
    again = api.post("/session/totp", {"code": code})
    assert again.status_code == 401, "the same code worked twice"


def test_logout_clears_the_session(signed_in):
    assert signed_in.get("/overview").status_code != 401
    signed_in.delete("/session")
    assert signed_in.get("/overview").status_code == 401


# --- backup codes -----------------------------------------------------------


def test_a_backup_code_works_once_and_only_once(app, api):
    auth = app.config["WEB_AUTH_FIXED"]
    codes = webauth.generate_backup_codes(auth)
    app.config["WEB_AUTH_FIXED"] = auth
    code = codes[0]

    api.post("/session", {"username": USER, "password": PASSWORD})
    assert api.post("/session/totp", {"backupCode": code}).status_code == 200
    api.delete("/session")

    api.post("/session", {"username": USER, "password": PASSWORD})
    r = api.post("/session/totp", {"backupCode": code})
    assert r.status_code == 401, "a backup code was reusable"
    assert app.config["WEB_AUTH_FIXED"].backup_codes_left == len(codes) - 1


def test_backup_codes_are_stored_only_as_digests(tmp_path):
    auth = make_auth()
    codes = webauth.generate_backup_codes(auth)
    path = tmp_path / "web-auth.json"
    webauth.save(auth, path)
    written = path.read_text()
    for code in codes:
        assert code not in written, "a backup code was written in the clear"
    assert len(json.loads(written)["backup_codes"]) == webauth.BACKUP_CODE_COUNT


def test_backup_codes_are_case_and_space_insensitive():
    auth = make_auth()
    code = webauth.generate_backup_codes(auth)[0]
    assert webauth.consume_backup_code(auth, f" {code.lower()} ")


def test_eight_codes_are_issued():
    """Mandatory, not optional: a lost phone must not mean a lost ledger."""
    auth = make_auth()
    assert len(webauth.generate_backup_codes(auth)) == 8


# --- remembered devices -----------------------------------------------------


def test_remember_this_device_skips_the_second_factor_next_time(app, api):
    api.post("/session", {"username": USER, "password": PASSWORD})
    api.post("/session/totp", {"code": code_now(), "remember": True})
    assert api.client.get_cookie("pb_device") is not None
    api.delete("/session")

    r = api.post("/session", {"username": USER, "password": PASSWORD})
    assert r.get_json() == {"stage": "done", "rememberedDevice": True}


def test_a_remembered_device_still_needs_the_password(app, api):
    api.post("/session", {"username": USER, "password": PASSWORD})
    api.post("/session/totp", {"code": code_now(), "remember": True})
    api.delete("/session")

    r = api.post("/session", {"username": USER, "password": "wrong"})
    assert r.status_code == 401
    assert api.get("/session").get_json()["authenticated"] is False


def test_not_remembering_leaves_no_device_cookie(api):
    api.post("/session", {"username": USER, "password": PASSWORD})
    api.post("/session/totp", {"code": code_now()})
    assert api.client.get_cookie("pb_device") is None


def test_an_expired_device_does_not_count():
    auth = make_auth()
    token = webauth.new_device_token()
    webauth.remember_device(auth, token, days=-1)
    assert webauth.device_valid(auth, token) is False


# --- rate limiting ----------------------------------------------------------


def test_repeated_failures_lock_the_account_out(api):
    from passbook.web.auth import MAX_ATTEMPTS

    for _ in range(MAX_ATTEMPTS):
        api.post("/session", {"username": USER, "password": "wrong"})

    blocked = api.post("/session", {"username": USER, "password": PASSWORD})
    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "rate_limited"
    # And the correct password does not get through while locked.
    assert api.get("/session").get_json()["authenticated"] is False


def test_the_second_factor_is_rate_limited_too(api):
    from passbook.web.auth import MAX_ATTEMPTS

    api.post("/session", {"username": USER, "password": PASSWORD})
    for _ in range(MAX_ATTEMPTS):
        api.post("/session/totp", {"code": "000000"})
    r = api.post("/session/totp", {"code": code_now()})
    assert r.status_code == 429


def test_a_success_clears_the_counter(api):
    api.post("/session", {"username": USER, "password": "wrong"})
    api.post("/session", {"username": USER, "password": "wrong"})
    api.post("/session", {"username": USER, "password": PASSWORD})
    api.post("/session/totp", {"code": code_now()})
    api.delete("/session")
    for _ in range(4):
        api.post("/session", {"username": USER, "password": "wrong"})
    # Still under the limit, because the earlier two were forgiven.
    assert api.post("/session", {"username": USER, "password": PASSWORD}).status_code == 200


# --- the timing oracle ------------------------------------------------------


def test_an_unknown_username_still_costs_a_full_hash(monkeypatch):
    """The bug this closes: the unknown-username branch used to return without
    hashing anything, so it answered in microseconds while a known username took
    ~100 ms. That difference is a free account-enumeration oracle."""
    calls = []
    real = webauth.check_password_hash
    monkeypatch.setattr(
        webauth, "check_password_hash", lambda h, p: calls.append(h) or real(h, p)
    )

    webauth.verify_password(make_auth().password_hash, "wrong")
    known = len(calls)
    calls.clear()
    webauth.verify_password(None, "wrong")
    unknown = len(calls)

    assert known == unknown == 1, "the two branches did different amounts of work"


def test_no_credential_configured_reads_as_misconfigured(app, api):
    app.config["WEB_AUTH_FIXED"] = webauth.WebAuth()
    with app.test_request_context():
        from passbook.web.auth import check_password

        ok, reason = check_password(USER, PASSWORD)
    assert ok is False
    assert "no credential configured" in reason


def test_an_unusable_stored_hash_is_not_a_wrong_password():
    """Werkzeug returns False rather than raising on some malformed hashes, so a
    mangled file would otherwise present as a typo."""
    assert webauth.verify_password("not-a-hash", PASSWORD) is False


def test_reasons_never_contain_the_password_or_hash(app):
    from passbook.web.auth import check_password

    with app.test_request_context():
        for username, password in [(USER, "wrong"), ("nobody", PASSWORD)]:
            _, reason = check_password(username, password)
            assert PASSWORD not in reason
            assert "scrypt" not in reason


# --- CSRF -------------------------------------------------------------------


def test_a_post_without_the_csrf_header_is_refused(app):
    client = app.test_client()
    client.get("/api/session")
    r = client.post("/api/session", json={"username": USER, "password": PASSWORD})
    assert r.status_code == 403
    assert r.get_json()["code"] == "csrf"


def test_a_mismatched_csrf_header_is_refused(api):
    r = api.post("/session", {"username": USER}, headers={"X-Passbook-CSRF": "wrong"})
    assert r.status_code == 403


# --- upload validation ------------------------------------------------------


def test_a_pdf_is_accepted_but_a_broken_one_still_fails_cleanly(signed_in, app):
    """PDFs are a supported source since §6.8 — the sniffer routes them to the
    PDF loader instead of refusing. A truncated one still fails, and still
    leaves nothing behind in inbox/."""
    r = signed_in.upload(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", "statement.xls")
    assert r.status_code == 422
    assert "Phase 6" not in r.get_json()["error"]
    assert list((app.config["INBOX"]).iterdir()) == []


def test_rejects_an_empty_file(signed_in):
    r = signed_in.upload(b"")
    assert r.status_code == 422
    assert "empty" in r.get_json()["error"]


def test_a_rejected_upload_leaves_nothing_behind(signed_in, app):
    """Never left in inbox/, where a later `make sync` would find it."""
    signed_in.upload(b"nonsense that is not a statement")
    assert list(app.config["INBOX"].iterdir()) == []


def test_valid_statement_previews_without_pushing(signed_in, app):
    r = signed_in.upload(XLS_FIXTURE.read_bytes())
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] == 93
    assert len(body["transactions"]) == 93
    # Staged, not pushed.
    assert list(app.config["ARCHIVE"].iterdir()) == []


def test_preview_masks_the_account_number(signed_in):
    body = signed_in.upload(XLS_FIXTURE.read_bytes()).get_json()
    assert body["meta"]["account"].endswith(FIXTURE_ACCOUNT[-4:])
    assert FIXTURE_ACCOUNT not in json.dumps(body)


def test_preview_carries_no_category(signed_in):
    """Rules are applied by the ledger at store time, so at preview no category
    exists. Showing one would be a guess (D10) or a lie."""
    body = signed_in.upload(XLS_FIXTURE.read_bytes()).get_json()
    assert "category" not in json.dumps(body["transactions"][0])


def test_preview_rows_are_complete_and_in_sheet_order(signed_in):
    """The table carries a Balance column, so the rows must chain. A filtered
    or reordered subset asserts a continuity that is not there — §6.6."""
    from decimal import Decimal

    body = signed_in.upload(XLS_FIXTURE.read_bytes()).get_json()
    rows = body["transactions"]
    balance = Decimal(body["meta"]["openingBalance"])
    for row in rows:
        balance += Decimal(row["credit"] or 0) - Decimal(row["debit"] or 0)
        assert balance == Decimal(row["balance"]), f"chain broke at {row['id']}"
    assert balance == Decimal(body["meta"]["closingBalance"])


def test_every_row_carries_its_clock_or_an_explicit_null(signed_in):
    """The Day Rail's input. A missing time must be null, never midnight —
    that would invent a nocturnal transaction that did not happen."""
    rows = signed_in.upload(XLS_FIXTURE.read_bytes()).get_json()["transactions"]
    with_clock = [r for r in rows if r["time"] is not None]
    assert len(with_clock) == 85
    assert all(r["time"] is None or len(r["time"]) == 8 for r in rows)


def test_money_is_a_string_never_a_json_number(signed_in):
    """A JSON number is an IEEE double the moment it is parsed. CLAUDE.md's
    first non-negotiable does not stop at the process boundary."""
    raw = signed_in.upload(XLS_FIXTURE.read_bytes()).data.decode()
    body = json.loads(raw)
    assert isinstance(body["transactions"][0]["balance"], str)
    assert isinstance(body["withdrawn"], str)
    assert isinstance(body["meta"]["openingBalance"], str)


def test_confirm_refuses_when_nothing_is_pending(signed_in):
    r = signed_in.post("/statement/confirm")
    assert r.status_code == 404
    assert r.get_json()["code"] == "no_pending"


# --- §6.7 account assertion -------------------------------------------------


def test_statement_from_an_unregistered_account_is_refused(signed_in, app, monkeypatch):
    """§21.2. The refusal got MORE specific, not less: `unknown_account` names the
    masked number and what is registered, so the UI can offer to add it — and the
    staged file is still deleted, so a later `make sync` cannot pick it up.

    An unregistered account must never silently import. That is the whole
    guarantee, and it is inherited: `UnknownAccount` subclasses `AccountMismatch`,
    which this path has refused with a 422 since Phase 7.
    """
    monkeypatch.setenv("PASSBOOK_ACCOUNT_NUMBER", "111100009999")
    r = signed_in.upload(XLS_FIXTURE.read_bytes())
    assert r.status_code == 422
    body = r.get_json()
    assert body["code"] == "unknown_account"
    assert body["account"] == "****1111"
    assert body["known"] == ["canara-9999"]
    assert list(app.config["INBOX"].iterdir()) == []


def test_refusal_message_masks_both_account_numbers(signed_in, monkeypatch):
    other = "111100009999"
    monkeypatch.setenv("PASSBOOK_ACCOUNT_NUMBER", other)
    message = signed_in.upload(XLS_FIXTURE.read_bytes()).get_json()["error"]
    assert FIXTURE_ACCOUNT not in message
    assert other not in message
    assert other[-4:] in message


def test_the_assertion_also_guards_confirm(signed_in, app, monkeypatch):
    """Staged under one setting, pushed under another."""
    signed_in.upload(XLS_FIXTURE.read_bytes())
    monkeypatch.setenv("PASSBOOK_ACCOUNT_NUMBER", "111100009999")
    r = signed_in.post("/statement/confirm")
    assert r.status_code == 422
    assert r.get_json()["code"] in ("account_mismatch", "unknown_account")


# --- payees and the config write --------------------------------------------


@pytest.fixture
def config_files(tmp_path):
    config = tmp_path / "config"
    (config / "payee_aliases.yaml").write_text("aliases:\n  ZEPKV JYX: Canteen\n")
    (config / "rules.yaml").write_text(
        "rule_group:\n  title: passbook\n"
        "rules:\n"
        "  - title: Food\n"
        "    category: Eating out\n"
        "    payees:\n"
        "      - Canteen  # a canteen, confirmed by the operator\n"
    )
    return config


def test_diff_is_shown_and_nothing_is_written(signed_in, config_files):
    before = (config_files / "payee_aliases.yaml").read_text()
    r = signed_in.post("/payees/diff", {"aliases": {"NYXQ RVEXM": "Mother"}, "categories": {}})
    assert r.status_code == 200
    assert "Mother" in r.get_json()["changes"][0]["diff"]
    assert (config_files / "payee_aliases.yaml").read_text() == before


def test_apply_writes_and_preserves_comments(signed_in, config_files):
    signed_in.post(
        "/payees/apply",
        {"aliases": {"NYXQ RVEXM": "Mother"}, "categories": {"NYXQ RVEXM": "Eating out"}},
    )
    aliases = (config_files / "payee_aliases.yaml").read_text()
    rules = (config_files / "rules.yaml").read_text()
    assert "Mother" in aliases
    # rules.yaml's comments are where D10's evidence lives. Losing them to a
    # UI write would destroy the reasoning that stops a token being misread.
    assert "a canteen, confirmed by the operator" in rules


def test_unknown_category_is_refused_rather_than_invented(signed_in, config_files):
    r = signed_in.post(
        "/payees/apply", {"aliases": {}, "categories": {"ZEPKV JYX": "Invented"}}
    )
    assert r.status_code == 422
    assert r.get_json()["code"] == "unknown_category"
    assert "Invented" not in (config_files / "rules.yaml").read_text()


def test_clearing_an_alias_files_the_category_under_the_raw_token(signed_in, config_files):
    """SPEC §23.3. Clearing an alias in the same submission that sets a category.

    `plan_categories` resolves a token to its DISPLAY name, and the merged map it
    resolves through used to keep an alias that `plan_aliases` was about to
    delete. The category was then filed under the deleted alias while the row
    would be pushed under its raw token, so the rule matched nothing — silently,
    on the page that had just reported the write.
    """
    r = signed_in.post(
        "/payees/apply",
        {"aliases": {"ZEPKV JYX": ""}, "categories": {"ZEPKV JYX": "Eating out"}},
    )
    assert r.status_code == 200

    from passbook.rules import load_rules

    aliases = (config_files / "payee_aliases.yaml").read_text()
    rules = (config_files / "rules.yaml").read_text()
    assert "Canteen" not in aliases, "the alias was cleared"
    assert "ZEPKV JYX" in rules, "the category is filed under the token that will be pushed"
    assert "- Canteen" not in rules, "and not under the alias that no longer exists"
    assert service.predict_category("ZEPKV JYX (UPI)", "", load_rules(config_files / "rules.yaml")) == (
        "Eating out"
    )


def test_renaming_an_alias_keeps_the_category_it_already_had(signed_in, config_files):
    """SPEC §23.4. A rename must not silently de-categorise the payee.

    Rules match the DISPLAY name — `description_starts: Canteen` against a row
    pushed as `Canteen (UPI)`. Renaming the alias to `Mess` moved the row out
    from under its own rule and left `payees: [Canteen]` pointing at a name
    nothing would ever be pushed under. Measured before the fix:
    `predict_category('Mess (UPI)')` returned `''`.
    """
    from passbook.rules import load_rules

    r = signed_in.post(
        "/payees/apply", {"aliases": {"ZEPKV JYX": "Mess"}, "categories": {}}
    )
    assert r.status_code == 200

    rules = load_rules(config_files / "rules.yaml")
    assert service.predict_category("Mess (UPI)", "", rules) == "Eating out"
    text = (config_files / "rules.yaml").read_text()
    assert "- Mess" in text
    assert "- Canteen" not in text, "the name nothing will be pushed under is gone"
    # The evidence comment travels with the entry it explains — losing it would
    # discard D10's record of why the token was categorised.
    assert re.search(r"- Mess\s+# a canteen, confirmed by the operator", text)


def test_a_rename_shows_up_in_the_diff_before_it_is_written(signed_in, config_files):
    before = (config_files / "rules.yaml").read_text()
    body = signed_in.post(
        "/payees/diff", {"aliases": {"ZEPKV JYX": "Mess"}, "categories": {}}
    ).get_json()

    diffs = {c["path"]: c["diff"] for c in body["changes"]}
    assert any("rules.yaml" in path for path in diffs), "the rule move is shown, not silent"
    assert (config_files / "rules.yaml").read_text() == before


def test_a_category_chosen_in_the_same_submission_beats_the_rename(signed_in, config_files):
    """Renames are followed first so an explicit choice overwrites, not races."""
    from passbook.rules import load_rules

    (config_files / "rules.yaml").write_text(
        "rule_group:\n  title: passbook\n"
        "rules:\n"
        "  - title: Food\n    category: Eating out\n    payees:\n      - Canteen\n"
        "  - title: Mess\n    category: Mess\n    payees: []\n"
    )
    signed_in.post(
        "/payees/apply",
        {"aliases": {"ZEPKV JYX": "Mess hall"}, "categories": {"ZEPKV JYX": "Mess"}},
    )
    rules = load_rules(config_files / "rules.yaml")
    assert service.predict_category("Mess hall (UPI)", "", rules) == "Mess"


def test_the_diff_says_what_would_change_in_the_ledger_before_the_write(
    signed_in, app, monkeypatch, config_files
):
    """SPEC §23.1. The ledger consequence is knowable first, so it is shown first."""
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )

    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    seen = {}

    def preview(client, settings, archive, **kwargs):
        seen.update(kwargs)
        return [], 7

    monkeypatch.setattr(api_module.service, "reapply_preview", preview)
    monkeypatch.setattr(
        api_base, "open_ledger", lambda *a, **k: FakeLedger([]))

    body = signed_in.post(
        "/payees/diff", {"aliases": {"NYXQ RVEXM": "Mother"}, "categories": {}}
    ).get_json()

    assert body["ledger"]["considered"] == 7
    # The submitted config, not the file it is about to replace. Reading the
    # file would preview the state the operator is leaving behind.
    assert seen["aliases"]["NYXQ RVEXM"] == "Mother"


def test_the_diff_still_works_when_the_ledger_cannot_be_reached(
    signed_in, app, monkeypatch, config_files
):
    """Writing config is what was asked for; it does not depend on the ledger."""
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )

    def boom(*a, **k):
        raise api_module.LedgerError("cannot reach the ledger")

    monkeypatch.setattr(
        api_base, "open_ledger", boom)
    body = signed_in.post(
        "/payees/diff", {"aliases": {"NYXQ RVEXM": "Mother"}, "categories": {}}
    ).get_json()

    assert body["ledger"] is None
    assert body["changes"], "the config diff is unaffected"


def test_applying_a_payee_edit_updates_the_rows_already_in_the_ledger(
    signed_in, app, monkeypatch, config_files
):
    """SPEC §23. The flaw this closes: config was written, the ledger was not.

    Before this, `/payees/apply` wrote the files, synced the rules and stopped.
    The rows already pushed kept their old names until someone ran a purge and a
    full re-push, gated on a dump taken on the host — so in practice they kept
    them for good.
    """
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )

    change = service.ReapplyChange(
        external_id="canara-1111-20260509000001",
        date="2026-05-09",
        amount=Decimal("100.00"),
        old_description="OLD (UPI)",
        new_description="Mother (UPI)",
        old_category="",
        new_category="Eating out"
    )
    previews = iter([([change], 93), ([], 93)])
    monkeypatch.setattr(
        api_module.service, "reapply_preview", lambda *a, **k: next(previews)
    )

    updated = []
    monkeypatch.setattr(
        api_module.service,
        "sync_ledger",
        lambda client, changes: updated.extend(changes)
        or service.SyncResult(updated=len(changes)),
    )
    monkeypatch.setattr(
        api_base, "open_ledger", lambda *a, **k: FakeLedger([]))

    body = signed_in.post(
        "/payees/apply", {"aliases": {"NYXQ RVEXM": "Mother"}, "categories": {}}
    ).get_json()

    assert len(updated) == 1, "the ledger was written, not just config"
    assert body["synced"]["updated"] == 1
    # Re-read afterwards rather than inferred from the count of 200s.
    assert body["synced"]["remaining"] == 0
    assert "1 of 1 row(s) updated in place" in body["summary"]


def test_rows_an_update_could_not_fix_are_reported_not_swallowed(
    signed_in, app, monkeypatch, config_files
):
    """An update cannot create a missing row. Saying so is the difference between
    a report and a green tick for something never checked (non-negotiable 11)."""
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )
    stale = service.ReapplyChange(
        external_id="x", date="2026-05-09", amount=Decimal("1"),
        old_description="a", new_description="b",
        old_category="", new_category=""
    )
    previews = iter([([stale], 93), ([stale], 93)])
    monkeypatch.setattr(
        api_module.service, "reapply_preview", lambda *a, **k: next(previews)
    )
    monkeypatch.setattr(
        api_module.service, "sync_ledger", lambda *a: service.SyncResult(updated=1)
    )
    monkeypatch.setattr(
        api_base, "open_ledger", lambda *a, **k: FakeLedger([]))

    body = signed_in.post("/payees/apply", {"aliases": {}, "categories": {}}).get_json()
    assert body["synced"]["remaining"] == 1
    assert "still differ" in body["summary"]


def test_a_failed_verification_reads_as_unverified_not_as_clean(
    signed_in, app, monkeypatch, config_files
):
    """The rows were written; the CHECK is what failed. §23, non-negotiable 11.

    Reporting nothing because the re-read failed would be the worst of both —
    silent writes and a silent error — and reporting `ok` would be a tick for
    something never looked at.
    """
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )
    stale = service.ReapplyChange(
        external_id="x", date="2026-05-09", amount=Decimal("1"),
        old_description="a", new_description="b",
        old_category="", new_category=""
    )
    calls = {"n": 0}

    def preview(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return [stale], 93
        raise api_module.LedgerError("cannot reach the ledger")

    monkeypatch.setattr(api_module.service, "reapply_preview", preview)
    monkeypatch.setattr(
        api_module.service, "sync_ledger", lambda *a: service.SyncResult(updated=1)
    )
    monkeypatch.setattr(api_reconcile, "_ledger_verdict", lambda *a, **k: {"ok": None})
    monkeypatch.setattr(
        api_base, "open_ledger", lambda *a, **k: FakeLedger([]))

    body = signed_in.post("/reapply/sync").get_json()
    assert body["updated"] == 1, "the write is still reported"
    assert body["remaining"] is None, "unverified, not zero"
    assert body["ok"] is False, "and never rendered as a pass"


def test_the_in_place_sync_needs_no_database_dump(signed_in, app, monkeypatch, config_files):
    """A purge is gated on a dump because it deletes. This deletes nothing, and
    gating it would leave the ledger stale for exactly the reason it already was."""
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )
    monkeypatch.setattr(api_module.service, "reapply_preview", lambda *a, **k: ([], 93))
    monkeypatch.setattr(
        api_module.service, "sync_ledger", lambda *a: service.SyncResult()
    )
    monkeypatch.setattr(api_reconcile, "_ledger_verdict", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        api_base, "open_ledger", lambda *a, **k: FakeLedger([]))

    r = signed_in.post("/reapply/sync")
    assert r.status_code == 200, "no dump exists in backups/ and that is fine here"
    assert r.get_json()["considered"] == 93


# --- encrypted PDFs, prompted for rather than converted online ---------------
# SPEC §30. Most Indian banks hand out password-protected PDFs and the obvious
# move is an online "unlocker", which is privacy-fatal (non-negotiable 7).


def test_an_encrypted_pdf_asks_for_a_password_instead_of_failing(signed_in, app):
    from conftest import FIXTURES

    pdf = (FIXTURES / "statement.pdf").read_bytes()
    r = signed_in.upload(pdf, "statement.pdf")

    assert r.status_code == 422
    body = r.get_json()
    assert body["code"] == "pdf_password", "a distinct code — the remedy is a password"
    assert "encrypted" in body["error"]


def test_the_right_password_unlocks_it_in_process(signed_in, app):
    """No network, no conversion: pikepdf, in this process. §6.8, §30."""
    from conftest import FIXTURES, FIXTURE_ACCOUNT

    pdf = (FIXTURES / "statement.pdf").read_bytes()
    r = signed_in.client.post(
        "/api/statement",
        data={
            "statement": (io.BytesIO(pdf), "statement.pdf"),
            "password": FIXTURE_ACCOUNT[-4:],
        },
        content_type="multipart/form-data",
        headers=signed_in._headers,
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["count"] == FIXTURE_TXN_COUNT


def test_a_wrong_password_says_so_and_does_not_stage_a_half_read_file(signed_in, app):
    from conftest import FIXTURES

    pdf = (FIXTURES / "statement.pdf").read_bytes()
    r = signed_in.client.post(
        "/api/statement",
        data={"statement": (io.BytesIO(pdf), "statement.pdf"), "password": "0000"},
        content_type="multipart/form-data",
        headers=signed_in._headers,
    )
    assert r.status_code == 422
    assert r.get_json()["code"] == "pdf_password_wrong"
    assert "did not open" in r.get_json()["error"]
    assert signed_in.get("/statement/pending").status_code == 404


def _codes_for(signed_in, path: str, staged_name: str = "statement.pdf") -> tuple[str, str]:
    """`(code with no password, code with a wrong one)` from one endpoint."""
    from conftest import FIXTURES

    pdf = (FIXTURES / "statement.pdf").read_bytes()

    def post(data):
        return signed_in.client.post(
            f"/api{path}",
            data=data,
            content_type="multipart/form-data",
            headers=signed_in._headers,
        ).get_json()["code"]

    none = post({"statement": (io.BytesIO(pdf), staged_name)})
    wrong = post({"statement": (io.BytesIO(pdf), staged_name), "password": "0000"})
    return none, wrong


@pytest.mark.parametrize("path", ["/statement", "/accounts/inspect", "/banks/inspect"])
def test_a_wrong_password_is_a_different_code_from_needing_one(signed_in, app, path):
    """The bug that produced "nothing happens after that". SPEC §41.

    Both cases used to answer `pdf_password`, and to a browser that code means
    exactly one thing: **show the password box**. On a page where the box was
    already showing — which is every page where a password can be wrong — a
    rejected password re-rendered an identical screen. No toast, no message, no
    change. The Unlock button looked dead.

    There WAS a test for the wrong-password path and it passed, because it
    asserted the code the bug depended on. So this one asserts the property the
    UI actually needs, which is that the two codes are not the same.
    """
    none, wrong = _codes_for(signed_in, path)
    assert none == "pdf_password"
    assert wrong == "pdf_password_wrong"
    assert none != wrong, "the client cannot show a new state for an identical code"


@pytest.mark.parametrize("path", ["/statement", "/accounts/inspect", "/banks/inspect"])
def test_a_rejected_password_says_what_to_look_at(signed_in, app, path):
    """Not "invalid input". The remedy is a specific one and the message names
    the three things that are actually wrong with a mistyped bank password."""
    from conftest import FIXTURES

    pdf = (FIXTURES / "statement.pdf").read_bytes()
    body = signed_in.client.post(
        f"/api{path}",
        data={"statement": (io.BytesIO(pdf), "statement.pdf"), "password": "0000"},
        content_type="multipart/form-data",
        headers=signed_in._headers,
    ).get_json()

    assert "did not open" in body["error"]
    assert "capital" in body["error"] and "space" in body["error"]
    # And it still does not name a convention. "The password is the Customer
    # ID" cost a day and was false (§6.8.1).
    assert "customer" not in body["error"].lower()


def test_the_password_is_not_a_field_in_any_response(signed_in, app):
    """§11. It reaches pikepdf as an argument and is returned by nothing.

    Note what this canNOT assert: for Canara the PDF password IS the last four
    digits of the account number (§6.8.1), and §11 explicitly permits showing
    those — `****1111`. So "the string never appears" is unsatisfiable here by
    construction, and asserting it would be a test that looks strict and is
    actually just wrong. What is checkable is that no response carries it as a
    value of its own.
    """
    from conftest import FIXTURES, FIXTURE_ACCOUNT

    secret = FIXTURE_ACCOUNT[-4:]
    pdf = (FIXTURES / "statement.pdf").read_bytes()
    r = signed_in.client.post(
        "/api/statement",
        data={"statement": (io.BytesIO(pdf), "statement.pdf"), "password": secret},
        content_type="multipart/form-data",
        headers=signed_in._headers,
    )
    assert r.status_code == 200

    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for item in node:
                yield from keys(item)

    assert not [k for k in keys(r.get_json()) if "password" in k.lower()]

    # And the later reads of the same staged file still work without asking
    # again — the password rides in the signed session, never on disk.
    assert signed_in.get("/statement/pending").status_code == 200


# --- adding a bank from the browser (§34) ------------------------------------


def test_inspect_reads_a_grid_without_staging_anything(signed_in, app):
    """It must be safe on a file passbook cannot parse — that is the only case
    it exists for. So it reads and writes nothing."""
    r = signed_in.upload_to("/banks/inspect", XLS_FIXTURE.read_bytes(), "statement.xls")
    assert r.status_code == 200
    body = r.get_json()
    assert body["container"] == "xls"
    assert body["headerRow"] is not None
    # repr(), so a single space is visibly a space and not ''.
    assert any("' '" in cell for row in body["grid"] for cell in row)
    # Nothing staged, so nothing a later `make sync` could pick up.
    assert signed_in.get("/statement/pending").status_code == 404


def test_inspect_asks_for_a_password_on_an_encrypted_pdf(signed_in, app):
    from conftest import FIXTURES

    r = signed_in.upload_to("/banks/inspect", (FIXTURES / "statement.pdf").read_bytes(), "s.pdf")
    assert r.status_code == 422
    assert r.get_json()["code"] == "pdf_password"


def test_inspect_reads_an_encrypted_pdf_with_its_password(signed_in, app):
    """The whole point: a locked statement can be inspected on this machine,
    so nobody has to send it anywhere to add their bank."""
    from conftest import FIXTURES, FIXTURE_ACCOUNT

    r = signed_in.client.post(
        "/api/banks/inspect",
        data={
            "statement": (io.BytesIO((FIXTURES / "statement.pdf").read_bytes()), "s.pdf"),
            "password": FIXTURE_ACCOUNT[-4:],
        },
        content_type="multipart/form-data",
        headers=signed_in._headers,
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["rows"] > 10


def test_a_profile_is_written_and_loaded_back(signed_in, tmp_path, monkeypatch):
    from passbook.loaders import profiles

    monkeypatch.setattr(profiles, "PROFILES_DIR", tmp_path / "banks")
    body = {
        "bank": "union",
        "columns": {
            "Txn Date": "date", "Ref No": "txn_id", "Withdrawal": "debit",
            "Deposit": "credit", "Balance": "balance", "Description": "narration",
        },
    }
    r = signed_in.post("/banks", body)
    assert r.status_code == 200
    assert "union" in r.get_json()["banks"]
    assert (tmp_path / "banks" / "union.yaml").exists()


def test_an_incomplete_profile_is_refused_with_what_is_missing(signed_in, tmp_path, monkeypatch):
    from passbook.loaders import profiles

    monkeypatch.setattr(profiles, "PROFILES_DIR", tmp_path / "banks")
    r = signed_in.post("/banks", {"bank": "union", "columns": {"Txn Date": "date"}})
    assert r.status_code == 422
    assert "balance" in r.get_json()["error"]
    assert not (tmp_path / "banks").exists() or not list((tmp_path / "banks").glob("*.yaml"))


def test_a_builtin_bank_needs_no_profile(signed_in, tmp_path, monkeypatch):
    from passbook.loaders import profiles

    monkeypatch.setattr(profiles, "PROFILES_DIR", tmp_path / "banks")
    r = signed_in.post("/banks", {"bank": "canara", "columns": {}})
    assert r.status_code == 422
    assert "built in" in r.get_json()["error"]


# --- the date window --------------------------------------------------------
# SPEC §25. A filtered list that does not say it is filtered is how a payee gets
# decided twice, or never.


def test_payees_narrows_to_a_custom_window(signed_in, app, config_files):
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")

    everything = signed_in.get("/payees").get_json()
    assert everything["window"]["range"] == "all"
    assert everything["outsideWindow"] == 0

    narrow = signed_in.get("/payees?from=2026-06-01&to=2026-06-30").get_json()
    assert narrow["window"] == {"range": "custom", "from": "2026-06-01", "to": "2026-06-30"}
    assert narrow["total"] < everything["total"]
    # What the window is hiding is stated, not left to be inferred.
    assert narrow["outsideWindow"] == everything["total"] - narrow["total"]


def test_a_backwards_window_is_ordered_rather_than_refused(signed_in, app, config_files):
    """An empty result would look like no data. Swapping is the kinder read."""
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    body = signed_in.get("/payees?from=2026-06-30&to=2026-06-01").get_json()
    assert body["window"]["from"] == "2026-06-01"
    assert body["window"]["to"] == "2026-06-30"


def test_an_unparseable_date_shows_the_ledger_rather_than_an_error(
    signed_in, app, config_files
):
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    r = signed_in.get("/payees?from=not-a-date")
    assert r.status_code == 200
    assert r.get_json()["window"]["range"] == "all"


def test_named_ranges_are_resolved_by_the_server(signed_in, app, config_files):
    """So the page never decides where a month starts and the two cannot drift."""
    from datetime import date

    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    body = signed_in.get("/payees?range=month").get_json()
    today = date.today()
    assert body["window"]["from"] == today.replace(day=1).isoformat()
    assert body["window"]["to"] == today.isoformat()


def test_an_unknown_range_falls_back_to_everything(signed_in, app, config_files):
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    body = signed_in.get("/payees?range=fortnight").get_json()
    assert body["window"] == {"range": "all", "from": None, "to": None}


def test_the_window_is_applied_after_dedup_not_before(signed_in, app, config_files):
    """Overlapping downloads mean a row appears in several files. Cutting first
    would let a duplicate through as if it were a new row."""
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement-again.xls")
    body = signed_in.get("/payees?range=all").get_json()
    assert body["total"] == 93, "the same statement twice is still 93 rows"


# --- registering makes the ledger account too. SPEC §56.1 -------------------


def _second_account(tmp_path):
    """A registry holding one *other* account, which is the real case.

    Not an empty one: with nothing registered, the **upload** self-registers
    (§21.3, so a single-account operator never meets the feature) and
    `/accounts` then takes the idempotent path without reaching the ledger at all.
    Add-an-account exists for the second account, and that is what these
    exercise.
    """
    from passbook.config import Account, save_accounts

    save_accounts(
        [
            Account(
                slug="other-2222",
                bank="canara",
                account_number="222222222222",
                asset_account="Somebody Else",
            )
        ]
    )


def _register(signed_in, app, monkeypatch, *, existing="Cash wallet", body=None):
    """Stage the statement the way the page does, then register its account.

    `/accounts/inspect`, not `/statement`: the latter refuses an unregistered
    account and deletes the staged file (§21.7), which is right on the normal
    path and exactly wrong on the page for registering one.
    """
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )

    fake = FakeLedger([], account=existing)
    monkeypatch.setattr(
        api_base, "open_ledger", lambda *a, **k: fake)
    staged = signed_in.upload_to("/accounts/inspect", XLS_FIXTURE.read_bytes(), "s.xls")
    assert staged.status_code == 200, staged.get_json()
    return fake, signed_in.post("/accounts", body if body is not None else {})


def test_registering_creates_the_ledger_account_when_it_does_not_exist(
    signed_in, app, monkeypatch, tmp_path
):
    """*"I dont want to create manually in the ledger again as I upload the
    statement in UI."* Registering an account and giving it somewhere to post
    are one intention; splitting them sent the operator off to do half by hand."""
    _second_account(tmp_path)
    fake, response = _register(signed_in, app, monkeypatch)
    body = response.get_json()
    assert response.status_code == 200, body
    assert body["created"] is True
    assert body["assetCreated"] is True

    assert len(fake.stored) == 1, "exactly one asset account, made once"
    name, opening, on, currency = fake.stored[0]
    assert name
    assert currency == "INR"
    assert opening is not None and on is not None, "the opening balance and its date"


def test_the_default_name_is_the_bank_and_the_masked_number(
    signed_in, app, monkeypatch, tmp_path
):
    """Blank means "you decide". The statement already knows the number, and
    `Canara ****1111` beats anything typed at that point in the flow — it is
    also the one name that cannot collide with another account at that bank."""
    _second_account(tmp_path)
    fake, response = _register(signed_in, app, monkeypatch)
    assert response.status_code == 200, response.get_json()
    assert fake.stored[0][0] == f"Canara {FIXTURE_ACCOUNT[-4:].rjust(8, '*')}"


def test_naming_an_account_the_ledger_already_has_attaches_rather_than_duplicating(
    signed_in, app, monkeypatch, tmp_path
):
    """The operator pointing at something they made on purpose. Inventing a
    second one beside it would be the wrong kind of helpful."""
    _second_account(tmp_path)
    fake, response = _register(
        signed_in, app, monkeypatch,
        existing="My Savings", body={"assetAccount": "My Savings"},
    )
    body = response.get_json()
    assert response.status_code == 200, body
    assert body["assetCreated"] is False
    assert fake.stored == [], "it made one anyway"


def test_the_ledger_being_down_stops_before_the_registry_is_touched(
    signed_in, app, monkeypatch, tmp_path
):
    """Half a registration is worse than none: an account in the registry with
    nowhere to post refuses every future upload for a reason nobody can see."""
    from passbook.store import LedgerError
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )

    _second_account(tmp_path)

    class Dead(StoreDouble):
        """Asleep, and it says so when asked rather than when entered. §101."""

        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

        def asset_accounts(self):
            raise LedgerError("connection refused")

    # Stage with a working client, then let the ledger fall over between staging
    # and registering — which is the moment that matters.
    monkeypatch.setattr(
        api_base, "open_ledger", lambda *a, **k: FakeLedger([]))
    staged = signed_in.upload_to("/accounts/inspect", XLS_FIXTURE.read_bytes(), "s.xls")
    assert staged.status_code == 200, staged.get_json()

    monkeypatch.setattr(
        api_base, "open_ledger", Dead)
    response = signed_in.post("/accounts", {})

    assert response.status_code == 502
    from passbook.config import load_accounts

    assert [a.slug for a in load_accounts()] == ["other-2222"], (
        "the new account reached the registry despite the ledger failing"
    )


# --- trying a bank profile before saving it. SPEC §49 ------------------------
#
# This endpoint shipped with no test and broke on the very next refactor: a
# function it imports was renamed, and the operator found out as a 500 on the
# one screen they had been sent to for a diagnosis. An endpoint that exists to
# explain failures cannot itself fail unexplained.


CANARA_TRY = {
    "Date": "date",
    "Particulars": "narration",
    "Withdrawals": "debit",
    "Deposits": "credit",
    "Balance": "balance",
}


def _try_bank(signed_in, **overrides):
    from conftest import FIXTURES

    data = {
        "statement": (io.BytesIO((FIXTURES / "statement.pdf").read_bytes()), "s.pdf"),
        "password": "1111",
        "columns": json.dumps(CANARA_TRY),
        "metadata": json.dumps({"A/c": "account_number"}),
        "deriveTxnId": "true",
    }
    data.update(overrides)
    return signed_in.client.post(
        "/api/banks/try",
        data=data,
        content_type="multipart/form-data",
        headers=signed_in._headers,
    )


def test_try_parses_an_unsaved_profile_and_writes_nothing(signed_in, app, tmp_path):
    from passbook.loaders import profiles

    response = _try_bank(signed_in)
    assert response.status_code == 200, response.get_json()
    body = response.get_json()

    assert body.get("ok") is True, body.get("error")
    assert body["rows"] == FIXTURE_TXN_COUNT
    assert body["account"] == "****1111"
    assert body["opening"] == "10000.00"
    # Nothing was written: no profile, no registry entry, no staged file.
    assert not list(profiles.PROFILES_DIR.glob("*.yaml")) if profiles.PROFILES_DIR.is_dir() else True
    assert signed_in.get("/statement/pending").status_code == 404


def test_try_infers_the_date_format_from_the_operators_own_dates(signed_in, app):
    """§49.1. The next wall after the columns is always `unparseable date`, and
    the fix was a strftime string in a YAML file."""
    body = _try_bank(signed_in).get_json()
    assert body["dateCandidates"] == ["%d-%m-%Y"]
    assert body["dateSample"]


def test_try_reports_where_every_column_was_found(signed_in, app):
    """§51. Both sides — where the values start and where the figures end —
    because a heading is not its column and the operator has to be able to see
    which of the two went wrong."""
    body = _try_bank(signed_in).get_json()
    assert set(body["columns"]) == set(CANARA_TRY.values())
    assert body["edges"]["balance"] == 573.0
    assert body["edges"]["date"] == pytest.approx(23.0, abs=2.0)


def test_try_hands_back_the_rows_when_it_fails(signed_in, app):
    """The whole reason it exists: the operator sees their own statement laid
    out as the mapping reads it, because nobody else may look at it (§46)."""
    wrong = {**CANARA_TRY}
    wrong["Balance"] = "credit"
    del wrong["Deposits"]
    body = _try_bank(signed_in, columns=json.dumps(wrong)).get_json()

    assert body.get("ok") is not True
    assert body["error"]
    assert body["banded"], "no rows to look at, which is the point of the screen"


def test_try_reports_a_wrong_password_as_such_not_as_a_crash(signed_in, app):
    response = _try_bank(signed_in, password="0000")
    assert response.status_code == 422
    assert response.get_json()["code"] == "pdf_password_wrong"


def test_try_shares_shapes_and_never_content(signed_in, app):
    """§50. The dump is offered to be pasted to someone who must not see the
    statement, so it must carry no statement."""
    body = _try_bank(signed_in).get_json()
    blob = "\n".join(body["shapes"])

    assert blob
    assert "999900001111" not in blob
    assert "Particulars" not in blob and "Withdrawals" not in blob
    assert "UPI" not in blob
    # It does carry the geometry, which is the part that debugs a banding bug.
    assert "@" in blob and "92-92-94" in blob


def test_try_refuses_an_empty_mapping_rather_than_guessing(signed_in, app):
    response = _try_bank(signed_in, columns="{}")
    assert response.status_code == 422
    assert "Name the columns" in response.get_json()["error"]


# --- renaming an account. SPEC §40 ------------------------------------------


def test_an_unnamed_account_is_shown_as_bank_plus_last_four(signed_in, three_accounts):
    """Not the asset account's name, which is a string chosen in another
    app: it can be anything and it can be the same for two accounts."""
    labels = [a["label"] for a in signed_in.get("/accounts").get_json()["accounts"]]
    assert labels == ["Canara ****1111", "Canara ****2222", "Canara ****3333"]


def test_renaming_changes_the_display_and_nothing_else(signed_in, three_accounts):
    from passbook.config import find_account, load_accounts

    body = signed_in.patch("/accounts/a-1", {"label": "Salary"}).get_json()
    assert body["account"]["label"] == "Salary"
    assert body["account"]["renamed"] is True

    after = find_account(load_accounts(), "a-1")
    assert after.label == "Salary"
    # The slug namespaces external_id. Moving it would orphan every pushed row.
    assert after.slug == "a-1"
    assert after.account_number == "111111111111"
    assert after.asset_account == "A"
    assert after.external_id("20260509000001") == "a-1-20260509000001"


def test_clearing_the_name_is_a_reset_not_an_error(signed_in, three_accounts):
    signed_in.patch("/accounts/a-1", {"label": "Salary"})
    body = signed_in.patch("/accounts/a-1", {"label": "  "}).get_json()
    assert body["account"]["label"] == "Canara ****1111"
    assert body["account"]["renamed"] is False


def test_two_accounts_cannot_share_a_name(signed_in, three_accounts):
    """The name exists to tell them apart, so a duplicate defeats the feature —
    in the switcher, the masthead and every 'which account is this' caption."""
    signed_in.patch("/accounts/a-1", {"label": "Salary"})
    response = signed_in.patch("/accounts/b-2", {"label": "salary"})
    assert response.status_code == 400
    assert response.get_json()["code"] == "duplicate_label"

    # ...including against a DEFAULT name, which is a name someone can see.
    clash = signed_in.patch("/accounts/b-2", {"label": "Canara ****3333"})
    assert clash.get_json()["code"] == "duplicate_label"


def test_an_account_can_keep_its_own_name(signed_in, three_accounts):
    """Renaming to what it already is must not collide with itself."""
    signed_in.patch("/accounts/a-1", {"label": "Salary"})
    assert signed_in.patch("/accounts/a-1", {"label": "Salary"}).status_code == 200


def test_an_overlong_name_is_refused_with_the_limit_in_the_message(
    signed_in, three_accounts
):
    response = signed_in.patch("/accounts/a-1", {"label": "x" * 41})
    assert response.status_code == 400
    assert response.get_json()["code"] == "too_long"


def test_renaming_an_unknown_account_is_404(signed_in, three_accounts):
    assert signed_in.patch("/accounts/nope", {"label": "x"}).status_code == 404


def test_a_rename_leaves_the_ledger_alone(signed_in, three_accounts):
    """§23.4 in reverse. A *payee* rename moves the row out from under its own
    categorisation rule because rules match the display name. An *account* name
    is matched by nothing — it is never pushed and the ledger never sees it."""
    import json

    from passbook.rules import load_rules

    before = json.dumps(load_rules(), sort_keys=True)
    signed_in.patch("/accounts/a-1", {"label": "Salary"})
    assert json.dumps(load_rules(), sort_keys=True) == before


# --- removing an account. SPEC §38 ------------------------------------------


@pytest.fixture
def three_accounts(app, monkeypatch, tmp_path):
    from passbook.config import Account, save_accounts

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir(exist_ok=True)
    save_accounts(
        [
            Account(slug="a-1", bank="canara", account_number="111111111111", asset_account="A"),
            Account(slug="b-2", bank="canara", account_number="222222222222", asset_account="B"),
            Account(slug="c-3", bank="canara", account_number="333333333333", asset_account="C"),
        ]
    )
    return tmp_path


def test_removal_says_what_it_would_leave_behind(signed_in, three_accounts, monkeypatch):
    """The count is the warning. "Are you sure?" is not one."""
    body = signed_in.get("/accounts/a-1/removal").get_json()

    assert body["account"]["slug"] == "a-1"
    assert body["archiveFiles"] == 0
    assert body["last"] is False


def test_removal_of_an_unknown_account_is_404_not_a_silent_success(signed_in, three_accounts):
    response = signed_in.get("/accounts/nope/removal")
    assert response.status_code == 404
    assert signed_in.delete("/accounts/nope").status_code == 404


def test_removing_an_account_leaves_the_others_alone(signed_in, three_accounts):
    from passbook.config import load_accounts

    body = signed_in.delete("/accounts/b-2").get_json()
    assert body["removed"] == "b-2"
    assert [a["slug"] for a in body["remaining"]] == ["a-1", "c-3"]
    assert [a.slug for a in load_accounts()] == ["a-1", "c-3"]


def test_removing_the_last_account_removes_the_file_rather_than_writing_an_empty_list(
    signed_in, app, monkeypatch, tmp_path
):
    """`load_accounts` reads an empty registry as "never configured" and falls
    back to the single-account settings path (§21.3). A written `accounts: []`
    is a different state from an absent file to anything reading the YAML, so
    the file goes."""
    from passbook.config import ACCOUNTS_FILE, Account, save_accounts

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir(exist_ok=True)
    save_accounts(
        [Account(slug="only-1", bank="canara", account_number="111111111111", asset_account="A")]
    )
    assert (tmp_path / ACCOUNTS_FILE).exists()

    assert signed_in.get("/accounts/only-1/removal").get_json()["last"] is True
    assert signed_in.delete("/accounts/only-1").get_json()["remaining"] == []
    assert not (tmp_path / ACCOUNTS_FILE).exists()


def test_a_browser_still_scoped_to_a_removed_account_falls_back(
    signed_in, app, three_accounts
):
    """A stale selection in someone's localStorage must not break the page it
    is stored for."""
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )

    signed_in.delete("/accounts/b-2")
    with app.test_request_context("/api/payees?account=b-2"):
        scope, selected = api_module._account_scope()
    assert [a.slug for a in scope] == ["a-1"]
    assert selected == "a-1"


def test_removal_survives_the_ledger_being_asleep(signed_in, three_accounts, monkeypatch):
    """Refusing to show the screen because the stack is down would make this the
    one management action that needs the stack up to undo a ledger mistake."""
    from passbook.store import LedgerError

    class Dead(StoreDouble):
        """Asleep. It fails on the CALL, not on the `with`. §101.

        Since the client became request-scoped there is no `__enter__` to fail
        in — `_client` builds one and hands it out, so a store that is down
        announces itself at the first request. Which is where the call sites
        catch it, and always was.
        """

        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

        def asset_accounts(self):
            raise LedgerError("connection refused")

        def account_transactions(self, _account):
            raise LedgerError("connection refused")

        def identities(self, _account):
            raise LedgerError("connection refused")

    monkeypatch.setattr("passbook.web.api._base.open_ledger", Dead)
    body = signed_in.get("/accounts/a-1/removal").get_json()
    assert body["ledgerRows"] is None
    assert "connection refused" in body["countReason"]


# --- combining accounts -----------------------------------------------------


def test_a_subset_of_accounts_can_be_scoped(signed_in, app, monkeypatch, tmp_path):
    """SPEC §21.9, §25. With four accounts, "these two together" is a real
    question that neither "one" nor "all" answers."""
    from passbook.config import Account, save_accounts
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir(exist_ok=True)
    save_accounts(
        [
            Account(slug="a-1", bank="canara", account_number="111111111111", asset_account="A"),
            Account(slug="b-2", bank="canara", account_number="222222222222", asset_account="B"),
            Account(slug="c-3", bank="canara", account_number="333333333333", asset_account="C"),
        ]
    )

    with app.test_request_context("/api/payees?account=a-1,c-3"):
        scope, selected = api_module._account_scope()
    assert [a.slug for a in scope] == ["a-1", "c-3"]
    assert selected == "a-1,c-3"

    # Registry order, so two URLs naming the same accounts share a cache key.
    with app.test_request_context("/api/payees?account=c-3,a-1"):
        scope, selected = api_module._account_scope()
    assert selected == "a-1,c-3"

    # Naming every account is `all` by another route.
    with app.test_request_context("/api/payees?account=a-1,b-2,c-3"):
        _scope, selected = api_module._account_scope()
    assert selected == "all"

    # A stale slug is dropped, not 404'd.
    with app.test_request_context("/api/payees?account=a-1,gone"):
        scope, selected = api_module._account_scope()
    assert [a.slug for a in scope] == ["a-1"]


# --- emailed recovery, at the HTTP boundary ----------------------------------


def test_a_recovery_code_cannot_be_requested_before_the_password(api):
    """It is a second factor, not a way around the first. No pending sign-in,
    no code — otherwise anyone who can reach the port can post mail to the
    operator's inbox, and worse, mint a live credential."""
    r = api.post("/session/recover")
    assert r.status_code == 401
    assert r.get_json()["code"] == "expired"


def test_recovery_is_refused_when_no_address_is_set(api, monkeypatch):
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )

    auth = make_auth()
    auth.recovery_email = None
    monkeypatch.setitem(api.client.application.config, "WEB_AUTH_FIXED", auth)

    api.post("/session", {"username": USER, "password": PASSWORD})
    r = api.post("/session/recover")
    assert r.status_code == 409
    assert r.get_json()["code"] == "no_recovery_email"
    assert "backup code" in r.get_json()["error"], "says what to do instead"
    assert api_module is not None


def test_a_code_that_could_not_be_sent_is_burnt(api, monkeypatch):
    """A code that never reached the inbox must not sit live for ten minutes."""
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )

    auth = make_auth()
    auth.recovery_email = "me@example.com"
    monkeypatch.setitem(api.client.application.config, "WEB_AUTH_FIXED", auth)

    def boom(*a, **k):
        raise api_ops.reminders.SendFailed("no mail server")

    monkeypatch.setattr(api_ops.reminders, "send_text", boom)

    api.post("/session", {"username": USER, "password": PASSWORD})
    r = api.post("/session/recover")
    assert r.status_code == 502
    assert api.client.application.config["WEB_AUTH_FIXED"].recovery is None


def test_the_emailed_code_signs_in_and_is_then_spent(api, monkeypatch):
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )

    auth = make_auth()
    auth.recovery_email = "me@example.com"
    monkeypatch.setitem(api.client.application.config, "WEB_AUTH_FIXED", auth)

    sent: list[str] = []
    monkeypatch.setattr(
        api_ops.reminders, "send_text", lambda subject, body, **k: sent.append(body) or "to"
    )

    api.post("/session", {"username": USER, "password": PASSWORD})
    r = api.post("/session/recover")
    assert r.status_code == 200
    assert r.get_json()["to"] == "m***@example.com", "masked, never the address"

    # The code is in the email and nowhere in the response.
    body = sent[0]
    code = next(line.strip() for line in body.splitlines() if line.startswith("    "))
    assert code not in r.get_data(as_text=True)

    done = api.post("/session/totp", {"recoveryCode": code})
    assert done.status_code == 200
    assert done.get_json()["stage"] == "done"

    # Spent: signing in again with it must not work.
    api.client.delete("/api/session", headers=api._headers)
    api.post("/session", {"username": USER, "password": PASSWORD})
    assert api.post("/session/totp", {"recoveryCode": code}).status_code == 401


def test_a_wrong_recovery_code_does_not_sign_in(api, monkeypatch):
    auth = make_auth()
    auth.recovery_email = "me@example.com"
    monkeypatch.setitem(api.client.application.config, "WEB_AUTH_FIXED", auth)
    api.post("/session", {"username": USER, "password": PASSWORD})
    r = api.post("/session/totp", {"recoveryCode": "AAAAAAAA"})
    assert r.status_code == 401


def test_the_recovery_address_is_only_ever_returned_masked(signed_in, monkeypatch):
    r = signed_in.put("/recovery-email", {"email": "Someone@Example.COM"})
    assert r.status_code == 200
    assert r.get_json()["recoveryEmail"] == "S***@example.com"

    body = signed_in.get("/session").get_json()
    assert body["totp"]["recoveryEmail"] == "S***@example.com"
    assert "Someone@Example.COM" not in signed_in.get("/session").get_data(as_text=True)


def test_a_bad_recovery_address_is_refused(signed_in):
    r = signed_in.put("/recovery-email", {"email": "not-an-address"})
    assert r.status_code == 422
    assert r.get_json()["code"] == "invalid"


# --- the reminder -----------------------------------------------------------


def test_reminder_reads_and_writes_a_schedule(signed_in, config_files):
    body = signed_in.get("/reminder").get_json()
    assert body["frequency"] == "weekly"
    assert len(body["upcoming"]) == 5
    assert body["uid"] is None, "the calendar UID never reaches a page"

    saved = signed_in.put(
        "/reminder", {"frequency": "monthly", "day_of_month": 5, "hour": 8, "minute": 30}
    ).get_json()
    assert saved["label"] == "monthly on day 5 at 08:30"
    assert all(w.endswith("T08:30") for w in saved["upcoming"])
    assert (config_files / "reminder.yaml").exists()


def test_an_out_of_range_reminder_is_refused_with_the_bounds(signed_in, config_files):
    """Clamping silently would set a reminder for a time nobody chose."""
    r = signed_in.put("/reminder", {"hour": 99})
    assert r.status_code == 422
    assert r.get_json()["code"] == "invalid"
    assert "between 0 and 23" in r.get_json()["error"]


def test_the_calendar_file_downloads_and_parses(signed_in, config_files):
    icalendar = pytest.importorskip("icalendar")

    signed_in.put("/reminder", {"frequency": "weekly", "weekday": 2, "hour": 20})
    r = signed_in.client.get("/api/reminder.ics")

    assert r.status_code == 200
    assert r.mimetype == "text/calendar"
    assert "attachment" in r.headers["Content-Disposition"]
    assert ".ics" in r.headers["Content-Disposition"]
    # Generated per request from config; a cached copy would serve the schedule
    # the operator just changed away from.
    assert r.headers["Cache-Control"] == "no-store"

    event = next(
        p for p in icalendar.Calendar.from_ical(r.get_data(as_text=True)).walk()
        if p.name == "VEVENT"
    )
    assert event["RRULE"].to_ical().decode() == "FREQ=WEEKLY;BYDAY=WE"


def test_a_reminder_edit_bumps_the_sequence_so_a_re_import_is_not_ignored(
    signed_in, config_files
):
    icalendar = pytest.importorskip("icalendar")

    def sequence():
        text = signed_in.client.get("/api/reminder.ics").get_data(as_text=True)
        event = next(
            p for p in icalendar.Calendar.from_ical(text).walk() if p.name == "VEVENT"
        )
        return int(event["SEQUENCE"]), str(event["UID"])

    signed_in.put("/reminder", {"hour": 7})
    first, uid = sequence()
    signed_in.put("/reminder", {"hour": 8})
    second, same_uid = sequence()

    assert second > first, "a calendar ignores a re-import whose SEQUENCE has not moved"
    assert same_uid == uid, "the same event, updated — not a duplicate"


def test_the_diff_warns_when_a_change_empties_a_category(signed_in, config_files):
    """SPEC §33. Moving a payee out of its last category leaves a rule that can
    never match again — it still exists in the dropdown and in the ledger, so a
    report on it is permanently empty. A YAML diff of payee lists does not show
    that, which is why it went unnoticed for weeks."""
    (config_files / "rules.yaml").write_text(
        "rules:\n"
        "  - title: Day canteen\n    category: Day Canteen\n    tag: food\n"
        "    payees: [Canteen]\n"
        "  - title: College\n    category: College Expense\n    payees: []\n"
    )
    body = signed_in.post(
        "/payees/diff",
        {"aliases": {}, "categories": {"ZEPKV JYX": "College Expense"}},
    ).get_json()
    assert body["emptied"] == ["Day Canteen"]


def test_the_diff_warns_when_a_change_would_drop_a_tag(signed_in, app, monkeypatch, config_files):
    """The other half of §33, and the expensive one: `College Expense` carries
    no `tag:`, so 29 rows lost `food` and the roll-up under-reported the total
    while still looking like a plausible number."""
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )
    change = service.ReapplyChange(
        external_id="x", date="2026-08-01", amount=Decimal("40"),
        old_description="Day Canteen (UPI)", new_description="Day Canteen (UPI)",
        old_category="Day Canteen", new_category="College Expense",
        old_tags=("food",), new_tags=()
    )
    monkeypatch.setattr(api_module.service, "reapply_preview", lambda *a, **k: ([change], 93))
    monkeypatch.setattr(
        api_base, "open_ledger", lambda *a, **k: FakeLedger([]))

    body = signed_in.post(
        "/payees/diff", {"aliases": {}, "categories": {"ZEPKV JYX": "Eating out"}}
    ).get_json()
    assert body["ledger"]["tagsLost"] == [{"tag": "food", "rows": 1}]


def test_a_change_that_loses_nothing_warns_about_nothing(signed_in, config_files):
    body = signed_in.post(
        "/payees/diff", {"aliases": {"NYXQ RVEXM": "Mother"}, "categories": {}}
    ).get_json()
    assert body["emptied"] == []


def test_apply_can_carry_the_tag_across_in_the_same_write(signed_in, config_files):
    """SPEC §33.3. The remedy, not just the warning — and in the SAME plan as
    the categorisation, so the tag cannot be lost in the window between two
    writes."""
    from passbook.rules import load_rules

    (config_files / "rules.yaml").write_text(
        "rules:\n"
        "  - title: Day canteen\n    category: Day Canteen\n    tag: food\n"
        "    payees: [Canteen]\n"
        "  - title: College\n    category: College Expense\n    payees: []\n"
    )
    r = signed_in.post(
        "/payees/apply",
        {
            "aliases": {},
            "categories": {"ZEPKV JYX": "College Expense"},
            "keepTags": {"College Expense": "food"},
        },
    )
    assert r.status_code == 200

    rules = load_rules(config_files / "rules.yaml")
    college = next(x for x in rules["rules"] if x["category"] == "College Expense")
    assert college.get("tag") == "food", "the roll-up keeps counting"
    assert "food" in service.predict_tags("Canteen (UPI)", "", "withdrawal", rules)


def test_apply_can_remove_the_category_it_empties(signed_in, config_files):
    """After the categorisation, not before — beforehand the category still
    holds the payees and the removal would refuse, correctly and confusingly."""
    from passbook.configwrite import known_categories

    (config_files / "rules.yaml").write_text(
        "rules:\n"
        "  - title: Day canteen\n    category: Day Canteen\n    payees: [Canteen]\n"
        "  - title: College\n    category: College Expense\n    payees: []\n"
    )
    r = signed_in.post(
        "/payees/apply",
        {
            "aliases": {},
            "categories": {"ZEPKV JYX": "College Expense"},
            "removeEmptied": ["Day Canteen"],
        },
    )
    assert r.status_code == 200
    assert "Day Canteen" not in known_categories(config_files / "rules.yaml")


def test_neither_remedy_happens_unless_asked(signed_in, config_files):
    """They are defaults in the UI, not behaviour in the server. A write that
    does not mention them changes neither."""
    from passbook.rules import load_rules

    (config_files / "rules.yaml").write_text(
        "rules:\n"
        "  - title: Day canteen\n    category: Day Canteen\n    tag: food\n"
        "    payees: [Canteen]\n"
        "  - title: College\n    category: College Expense\n    payees: []\n"
    )
    signed_in.post(
        "/payees/apply", {"aliases": {}, "categories": {"ZEPKV JYX": "College Expense"}}
    )
    rules = load_rules(config_files / "rules.yaml")
    assert next(x for x in rules["rules"] if x["category"] == "College Expense").get("tag") is None
    assert any(x["category"] == "Day Canteen" for x in rules["rules"])


def test_an_empty_category_can_be_removed(signed_in, config_files):
    """SPEC §32. Create-without-remove made a typo permanent — and left three
    probe rules in the operator's own config before anyone noticed."""
    signed_in.post("/categories", {"name": "Typo"})
    assert "Typo" in signed_in.get("/categories").get_json()["categories"]

    r = signed_in.delete("/categories/Typo")
    assert r.status_code == 200
    assert "Typo" not in r.get_json()["categories"]


def test_a_category_with_payees_is_not_removed(signed_in, config_files):
    """Deleting the rule does not delete the payees, it STRANDS them: the rows
    fall to uncategorised on the next push and nothing says so."""
    r = signed_in.delete("/categories/Eating out")
    assert r.status_code == 409
    assert r.get_json()["code"] == "in_use"
    assert "Canteen" in r.get_json()["error"], "names what is in the way"
    assert "Eating out" in signed_in.get("/categories").get_json()["categories"]


def test_removing_an_unknown_category_is_a_404(signed_in, config_files):
    assert signed_in.delete("/categories/Nope").status_code == 404


def test_a_category_the_charts_depend_on_is_not_removed(signed_in, config_files):
    """A name in `not_spend` that no longer exists excludes NOTHING — silently,
    and §8's whole point is that a silent exclusion failure looks plausible."""
    (config_files / "rules.yaml").write_text(
        "rules:\n  - title: Moves\n    category: Moves\n    payees: []\n"
        "not_spend: [Moves]\n"
    )
    r = signed_in.delete("/categories/Moves")
    assert r.status_code == 409
    assert "not_spend" in r.get_json()["error"]


def test_categories_endpoint_offers_only_existing_rules(signed_in, config_files):
    assert signed_in.get("/categories").get_json()["categories"] == ["Eating out"]


def test_payees_reports_an_hour_histogram_per_token(signed_in, app, config_files):
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    rows = signed_in.get("/payees").get_json()["rows"]
    assert rows, "no payees returned"
    for row in rows:
        assert len(row["hours"]) == 24
    # 85 of 93 rows carry a clock; the histogram must account for exactly those.
    assert sum(sum(r["hours"]) for r in rows) == 85


def test_undecided_tokens_come_first(signed_in, app, config_files):
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    rows = signed_in.get("/payees").get_json()["rows"]
    flags = [r["needsDecision"] for r in rows]
    assert flags == sorted(flags, reverse=True), "decided rows are mixed in above undecided ones"


# --- change password --------------------------------------------------------


def test_wrong_current_password_is_refused(signed_in):
    r = signed_in.post("/password", {"current": "wrong", "new": "x" * 12, "confirm": "x" * 12})
    assert r.status_code == 401


def test_mismatched_new_passwords_are_refused(signed_in):
    r = signed_in.post(
        "/password", {"current": PASSWORD, "new": "x" * 12, "confirm": "y" * 12}
    )
    assert r.status_code == 400


def test_short_new_password_is_refused(signed_in):
    r = signed_in.post("/password", {"current": PASSWORD, "new": "short", "confirm": "short"})
    assert r.status_code == 400


def test_a_valid_change_signs_you_out_and_keeps_totp(app, signed_in):
    new = "a-much-longer-password"
    r = signed_in.post("/password", {"current": PASSWORD, "new": new, "confirm": new})
    assert r.status_code == 200
    assert signed_in.get("/overview").status_code == 401

    auth = app.config["WEB_AUTH_FIXED"]
    assert webauth.verify_password(auth.password_hash, new)
    assert auth.totp_secret == SECRET, "changing the password destroyed the second factor"


# --- enrolment --------------------------------------------------------------


def test_enrolment_is_required_when_no_secret_exists(app, api):
    app.config["WEB_AUTH_FIXED"] = make_auth(totp_secret=None, totp_enrolled_at=None)
    r = api.post("/session", {"username": USER, "password": PASSWORD})
    assert r.get_json()["stage"] == "enroll"
    assert api.get("/overview").status_code == 401, "enrolment was skippable"


def test_enrolment_issues_eight_codes_and_signs_in(app, api):
    app.config["WEB_AUTH_FIXED"] = make_auth(totp_secret=None, totp_enrolled_at=None)
    api.post("/session", {"username": USER, "password": PASSWORD})

    started = api.post("/totp/enroll/start").get_json()
    assert started["qr"].lstrip().startswith("<svg")
    assert "otpauth://totp/" in started["uri"]

    code = pyotp.TOTP(started["secret"]).now()
    confirmed = api.post("/totp/enroll/confirm", {"code": code})
    assert confirmed.status_code == 200
    assert len(confirmed.get_json()["backupCodes"]) == 8
    assert api.get("/overview").status_code != 401


def test_a_bad_enrolment_code_stores_nothing(app, api):
    app.config["WEB_AUTH_FIXED"] = make_auth(totp_secret=None, totp_enrolled_at=None)
    api.post("/session", {"username": USER, "password": PASSWORD})
    api.post("/totp/enroll/start")
    r = api.post("/totp/enroll/confirm", {"code": "000000"})
    assert r.status_code == 400
    assert app.config["WEB_AUTH_FIXED"].totp_secret is None


def test_regenerating_backup_codes_needs_the_password(signed_in):
    assert signed_in.post("/totp/backup-codes", {"password": "wrong"}).status_code == 401
    r = signed_in.post("/totp/backup-codes", {"password": PASSWORD})
    assert len(r.get_json()["backupCodes"]) == 8


# --- secrets stay on the server ---------------------------------------------


def test_the_session_endpoint_never_returns_the_totp_secret(signed_in):
    body = signed_in.get("/session").data.decode()
    assert SECRET not in body


def test_status_never_returns_a_credential(signed_in, monkeypatch):
    """There is no API token any more, and the database password must not take
    its place: `/status` reports whether the ledger *answers*, never how it is
    reached."""
    secret = "super-secret-database-password"
    monkeypatch.setenv("PASSBOOK_DATABASE_URL", f"postgresql://u:{secret}@db:5432/x")
    body = signed_in.get("/status").data.decode()
    assert secret not in body
    assert set(json.loads(body)["store"]) == {"accounts", "error"}


# --- the bundle -------------------------------------------------------------


def test_an_unknown_api_path_is_json_not_the_spa(api):
    r = api.client.get("/api/nope")
    assert r.status_code == 404
    assert r.get_json()["code"] == "not_found"


def test_a_deep_link_serves_the_app_not_a_404(app):
    """Routing lives in the client, so /payees must return index.html."""
    from passbook.web.app import DIST

    response = app.test_client().get("/payees")
    if (DIST / "index.html").is_file():
        assert response.status_code == 200
    else:
        # Source checkout with no bundle built: says so rather than 404ing.
        assert response.status_code == 503
        assert b"has not been built" in response.data


# --- the CSV loader still round-trips (unchanged by this phase) --------------


def test_csv_fixture_still_parses(signed_in):
    r = signed_in.upload(CSV_FIXTURE.read_bytes(), "statement.csv")
    assert r.status_code == 200
    assert r.get_json()["count"] == 93


# --- ops stays unprivileged --------------------------------------------------


def test_ops_only_ever_executes_rclone():
    """AST-level, not a string search: the web container must not gain the
    ability to shell out to docker, gpg, or anything else. §15.3."""
    import ast

    source = Path("/home/shubh/projects/Bank-Spend/src/passbook/ops.py").read_text()
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


# --- the enrolment QR -------------------------------------------------------
# It rendered ~70 px inside a 300 px box and phone cameras could not lock on.
# segno emits width/height and NO viewBox unless told otherwise, and an SVG
# with intrinsic pixel dimensions and no viewBox does not scale: `width:100%`
# grows the canvas and leaves the drawing in the top-left corner. These assert
# the rendered geometry, not merely that an <svg> came back.

import re as _re


def _qr_root(svg: str) -> str:
    return svg[: svg.index(">") + 1]


def test_the_qr_carries_a_viewbox_matching_its_module_count():
    uri = webauth.totp_uri(SECRET, USER)
    root = _qr_root(webauth.totp_qr_svg(uri))
    match = _re.search(r'viewBox="0 0 (\d+) (\d+)"', root)
    assert match, f"no viewBox, so the QR cannot scale: {root}"
    width, height = int(match.group(1)), int(match.group(2))
    assert width == height, "QR must be square"
    assert width == webauth.totp_qr_modules(uri)


def test_the_qr_has_no_fixed_pixel_size_to_override():
    """width/height attributes would pin it at ~45 px however wide the box is."""
    root = _qr_root(webauth.totp_qr_svg(webauth.totp_uri(SECRET, USER)))
    assert not _re.search(r'\swidth="\d', root), f"fixed width pins the QR: {root}"
    assert not _re.search(r'\sheight="\d', root), f"fixed height pins the QR: {root}"


def test_the_qr_scales_proportionally_and_stays_crisp():
    root = _qr_root(webauth.totp_qr_svg(webauth.totp_uri(SECRET, USER)))
    assert 'preserveAspectRatio="xMidYMid meet"' in root
    assert 'shape-rendering="crispEdges"' in root


def test_the_qr_keeps_a_four_module_quiet_zone():
    """The spec's quiet zone. Without it a camera hunts for the code."""
    uri = webauth.totp_uri(SECRET, USER)
    svg = webauth.totp_qr_svg(uri)
    total = webauth.totp_qr_modules(uri)

    # segno emits ONE absolute `M` to open the path and relative `m` moves
    # thereafter, so only the absolute one describes a position. Matching `m`
    # too compares a 1-module relative hop against the border and always fails.
    origin = _re.search(r'd="M(\d+(?:\.\d+)?) (\d+(?:\.\d+)?)', svg)
    assert origin, "no absolute move in the QR path"
    x, y = float(origin.group(1)), float(origin.group(2))
    assert x >= webauth.QR_BORDER, f"first module at x={x}, inside the quiet zone"
    assert y >= webauth.QR_BORDER, f"first module at y={y}, inside the quiet zone"
    # And the modules stop short of the far edge by the same margin.
    assert total >= 2 * webauth.QR_BORDER + 21  # smallest QR is 21 modules


def test_the_enrolment_endpoint_returns_a_scalable_qr(app, api):
    app.config["WEB_AUTH_FIXED"] = make_auth(totp_secret=None, totp_enrolled_at=None)
    api.post("/session", {"username": USER, "password": PASSWORD})
    qr = api.post("/totp/enroll/start").get_json()["qr"]
    assert "viewBox" in qr
    assert not _re.search(r'\swidth="\d', _qr_root(qr))


def test_the_qr_is_dark_on_light_and_not_theme_dependent():
    """A decoder needs dark modules on a light field. Inverting the QR in dark
    mode produces a code many phone cameras will not read at all, so the
    backing must not be a theme token."""
    css = Path("/home/shubh/projects/Bank-Spend/frontend/src/theme.css").read_text()
    block = css[css.index(".qr {") : css.index(".qr svg {")]
    assert "background: #fff" in block, "the QR backing must be literally white"
    assert "var(--" not in block.split("border:")[0], "QR backing must not follow the theme"
    # And the modules are a stroked path, not filled rects.
    assert ".qr svg path {\n  stroke:" in css


# --- rotating a compromised secret ------------------------------------------


def test_reset_invalidates_the_old_secret_codes_and_devices(tmp_path, monkeypatch):
    """`make web-totp RESET=yes`. Everything minted from the old secret dies."""
    from typer.testing import CliRunner

    from passbook.cli import app as cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()

    old_secret = pyotp.random_base32()
    auth = webauth.WebAuth(
        username=USER, password_hash=webauth.hash_password(PASSWORD),
        totp_secret=old_secret, salt="f" * 32,
    )
    old_codes = webauth.generate_backup_codes(auth)
    device = webauth.new_device_token()
    webauth.remember_device(auth, device)
    webauth.save(auth)

    result = CliRunner().invoke(cli, ["web-totp", "--reset"])
    assert result.exit_code == 0, result.output

    after = webauth.load()
    assert after.totp_secret is None, "the old secret survived the reset"
    assert after.backup_codes == [], "old backup codes survived the reset"
    assert webauth.device_valid(after, device) is False, "a remembered device survived"
    # A code from the old secret is now unverifiable against anything.
    assert webauth.verify_totp(after, pyotp.TOTP(old_secret).now()) is False
    assert webauth.consume_backup_code(after, old_codes[0]) is False
    # The password is untouched — resetting the second factor must not lock
    # you out of the first.
    assert webauth.verify_password(after.password_hash, PASSWORD)


def test_a_reset_forces_enrolment_and_new_codes_differ(app, api):
    old_codes = webauth.generate_backup_codes(app.config["WEB_AUTH_FIXED"])
    app.config["WEB_AUTH_FIXED"] = make_auth(totp_secret=None, totp_enrolled_at=None)

    r = api.post("/session", {"username": USER, "password": PASSWORD})
    assert r.get_json()["stage"] == "enroll"

    started = api.post("/totp/enroll/start").get_json()
    assert started["secret"] != SECRET, "enrolment reissued the same secret"
    new_codes = api.post(
        "/totp/enroll/confirm", {"code": pyotp.TOTP(started["secret"]).now()}
    ).get_json()["backupCodes"]
    assert set(new_codes).isdisjoint(old_codes)


# --- histogram denominators -------------------------------------------------
# The bars count CLOCKED transactions; the row count is ALL of them, and 8 of
# 93 rows carry no clock. Labelling the chart with the wrong one told a
# screen-reader user that `Bank Charges` had 0 transactions when the row beside
# it said 2.


def test_payees_reports_clocked_separately_from_count(signed_in, app, config_files):
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    body = signed_in.get("/payees").get_json()
    rows = body["rows"]
    for row in rows:
        assert sum(row["hours"]) == row["clocked"], row["token"]
        assert row["clocked"] <= row["count"], row["token"]
    assert sum(r["count"] for r in rows) == body["total"] == 93
    assert sum(r["clocked"] for r in rows) == body["totalClocked"] == 85


def test_rows_with_no_clock_are_visible_as_such(signed_in, app, config_files):
    """The rows the old label silently reported as empty."""
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    rows = signed_in.get("/payees").get_json()["rows"]
    partial = [r for r in rows if r["clocked"] != r["count"]]
    assert partial, "expected rows whose clocked count differs from their total"
    assert any(r["clocked"] == 0 and r["count"] > 0 for r in partial)


def test_histograms_are_keyed_per_row_not_per_token(signed_in, app, config_files):
    """Rows group by (token, channel). Keying the histogram on token alone
    would hand two rows the same chart while their counts differed."""
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    rows = signed_in.get("/payees").get_json()["rows"]
    seen = {}
    for row in rows:
        seen.setdefault(row["token"], []).append(row)
    for token, group in seen.items():
        if len(group) > 1:
            totals = {sum(r["hours"]) for r in group}
            assert len(totals) == len(group) or all(
                sum(r["hours"]) == r["clocked"] for r in group
            ), f"{token} rows share a histogram"


# --- /analysis: the charts, and what they must never show --------------------
# SPEC §18. The figures on the Ledger page are the ones §8 and §8.1 define, not
# The ledger's by-type totals. On the real ledger those differ by a factor of three
# in both directions, so a chart drawn on the wrong one is not approximately
# right — and it looks fine.


class FakeLedger(StoreDouble):
    """Just enough ledger to answer /analysis. No network."""

    def __init__(self, splits, *, account="Test Account"):
        self.splits = splits
        self.account = account
        self.stored: list[tuple] = []

    def asset_accounts(self):
        return [
            {
                "name": self.account,
                "current_balance": "1.00",
                "opening_balance": "12612.64",
                "opening_on": date(2026, 5, 6),
                "currency": "INR",
            }
        ]

    def account_transactions(self, account):
        assert account == self.account
        return self.splits

    def identities(self, account):
        return {s["external_id"] for s in self.splits if s.get("external_id")}

    def store_transaction(self, split):
        self.splits.append(split)

    def update_transaction(self, external_id, fields):
        for split in self.splits:
            if split.get("external_id") == external_id:
                split.update(fields)

    def store_account(self, name, opening, on, currency):
        """Records what was asked for, so a test can assert the shape."""
        self.stored.append((name, opening, on, currency))


def _fake_ledger(monkeypatch, splits, **kwargs):
    from passbook.web.api import _base as api_base

    monkeypatch.setattr(
        api_base, "open_ledger", lambda *a, **k: FakeLedger(splits, **kwargs)
    )


def _split(kind, amount, *, category=None, tags=(), when="2026-06-10", external_id=None):
    return {
        "kind": kind,
        "amount": Decimal(str(amount)),
        "category": category,
        "tags": list(tags),
        "external_id": external_id,
        "txn_date": date.fromisoformat(when),
        "description": "",
        "counterparty": "",
    }


def test_analysis_excludes_movement_from_spend_and_returns_both_figures(
    signed_in, app, monkeypatch, config_files
):
    (config_files / "rules.yaml").write_text(
        "rules:\n"
        "  - title: Eating\n    category: Eating out\n    tag: food\n"
        "not_spend: [Investments]\n"
    )
    _fake_ledger(
        monkeypatch,
        [
            _split("withdrawal", "100", category="Eating out", tags=("food",)),
            _split("withdrawal", "30000", category="Investments"),
            _split("deposit", "20000", category="Salary"),
            _split("deposit", "5000", tags=("not-earnings",)),
            _split("opening balance", "12612"),
        ],
    )
    body = signed_in.get("/analysis").get_json()

    assert body["spend"] == "100.00" and body["grossSpend"] == "30100.00"
    assert body["income"] == "20000.00" and body["grossIncome"] == "25000.00"
    assert body["excludedSpendTotal"] == "30000.00"
    assert body["excludedIncome"]["amount"] == "5000.00"
    assert body["notSpend"] == ["Investments"]
    assert body["rollups"][0]["tag"] == "food"


def test_every_amount_in_the_analysis_is_a_string_never_a_json_number(
    signed_in, monkeypatch, config_files
):
    """§16.1. A JSON number is an IEEE double the moment `JSON.parse` sees it."""
    _fake_ledger(monkeypatch, [_split("withdrawal", "65", category="Shopping")])
    raw = json.loads(signed_in.get("/analysis").data)

    for key in ("spend", "grossSpend", "income", "grossIncome", "excludedSpendTotal"):
        assert isinstance(raw[key], str), key
    for slice_ in raw["categories"] + raw["excludedSpend"]:
        assert isinstance(slice_["amount"], str)
    for month in raw["months"]:
        assert isinstance(month["spend"], str) and isinstance(month["income"], str)
    # Counts are the one thing that IS a number, and must stay one.
    assert isinstance(raw["counted"], int)


def test_analysis_joins_the_clock_from_the_archive(
    signed_in, app, monkeypatch, config_files
):
    """The clock exists only in the statement (§6.5) — the ledger is never told it.
    So the hours come from the archive, joined on the bank's transaction id."""
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    from passbook import service

    transactions = service.archived_statements(app.config["ARCHIVE"])[0].transactions
    clocked = [t for t in transactions if t.txn_time][:3]
    _fake_ledger(
        monkeypatch,
        [
            _split(
                "withdrawal", "10", category="Shopping",
                when=t.txn_date.isoformat(), external_id=t.txn_id,
            )
            for t in clocked
        ],
    )
    body = signed_in.get("/analysis").get_json()

    assert sum(body["hours"]) == body["clocked"] == len(clocked)
    assert body["counted"] == len(clocked)
    assert body["coverage"]["from"] == "2026-05-07"


def test_analysis_says_which_months_are_partial(signed_in, app, monkeypatch, config_files):
    """The fixture covers 07-May to 07-Aug, so May and August are stubs. This is
    what the page shows instead of drawing a trend line through four points."""
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    _fake_ledger(
        monkeypatch,
        [
            _split("withdrawal", "10", category="Shopping", when="2026-05-20"),
            _split("withdrawal", "20", category="Shopping", when="2026-06-20"),
            _split("withdrawal", "30", category="Shopping", when="2026-08-02"),
        ],
    )
    months = signed_in.get("/analysis").get_json()["months"]
    assert [(m["month"], m["partial"]) for m in months] == [
        ("2026-05", True),
        ("2026-06", False),
        ("2026-08", True),
    ]


def test_analysis_with_no_registered_account_says_so(signed_in, monkeypatch):
    """It used to be gated on a credential being present in `.env`, which said
    whether a string existed rather than whether anything answered. What is
    left to be unconfigured is the registry."""
    from passbook.web.api import _scope as api_scope

    monkeypatch.setattr(api_scope, "load_accounts", lambda *a, **k: [])
    r = signed_in.get("/analysis")
    assert r.status_code == 503
    assert r.get_json()["code"] == "unconfigured"


def test_an_unreachable_ledger_is_a_502_not_a_500(signed_in, monkeypatch):
    """The charts are drawn from the ledger, so one that cannot be reached
    means no charts — and the client shows that instead of an empty page."""
    from passbook.store import LedgerError
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )

    def explode(*a, **k):
        raise LedgerError("connection refused")

    monkeypatch.setattr(
        api_base, "open_ledger", explode)
    r = signed_in.get("/analysis")
    assert r.status_code == 502
    assert r.get_json()["code"] == "ledger"


def test_analysis_never_leaks_the_account_number_or_the_token(
    signed_in, app, monkeypatch, config_files
):
    """§11. The charts are aggregates; nothing here needs an account number."""
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    _fake_ledger(monkeypatch, [_split("withdrawal", "65", category="Shopping")])
    body = signed_in.get("/analysis").data.decode()

    assert FIXTURE_ACCOUNT not in body
    assert "a.b.c" not in body


# --- the state a restore guarantees -----------------------------------------
# config/web-auth.json is deliberately excluded from the backup (§16.9), so a
# recovered install has no credentials at all. `make dr-drill` walks this; these
# pin the behaviour it now depends on.


@pytest.fixture
def unconfigured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    for var in ("PASSBOOK_WEB_USER", "PASSBOOK_WEB_PASSWORD_HASH",
                "PASSBOOK_WEB_PASSWORD_HASH_B64"):
        monkeypatch.delenv(var, raising=False)
    app = create_app({"TESTING": True, "SECRET_KEY": "t", "WEB_AUTH_FIXED": webauth.WebAuth()})
    return Api(app.test_client())


def test_a_restored_install_starts_and_reports_it_is_unconfigured(unconfigured):
    body = unconfigured.get("/session").get_json()
    assert body["configured"] is False
    assert body["authenticated"] is False


def test_signing_in_with_no_credentials_names_the_fix(unconfigured):
    """The old behaviour returned a bare "Sign-in failed." — indistinguishable
    from a typo, with the reason only in the container log. After a recovery
    that is the guaranteed state, so it says so: there is no account here to
    enumerate, and nothing to be vague about."""
    r = unconfigured.post("/session", {"username": "me", "password": "whatever"})
    assert r.status_code == 503
    body = r.get_json()
    assert body["code"] == "not_configured"
    assert "make web-password" in body["error"]


def test_an_unconfigured_install_writes_no_credential_file(unconfigured, tmp_path):
    unconfigured.post("/session", {"username": "me", "password": "whatever"})
    assert not (tmp_path / "config" / "web-auth.json").exists()


def test_web_password_then_enrolment_is_the_whole_recovery(tmp_path, monkeypatch):
    """Runbook step 7, end to end, without Docker."""
    from typer.testing import CliRunner

    from passbook.cli import app as cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    monkeypatch.setenv("PASSBOOK_ASSET_ACCOUNT", "Test Account")

    result = CliRunner().invoke(
        cli, ["web-password", "--username", "restored", "--password", "a-restored-password"]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "config" / "web-auth.json").exists()

    app = create_app({"TESTING": True, "SECRET_KEY": "t"})
    api = Api(app.test_client())
    assert api.get("/session").get_json()["configured"] is True

    r = api.post("/session", {"username": "restored", "password": "a-restored-password"})
    assert r.get_json()["stage"] == "enroll", "enrolment must be mandatory on a fresh credential"

    started = api.post("/totp/enroll/start").get_json()
    confirmed = api.post(
        "/totp/enroll/confirm", {"code": pyotp.TOTP(started["secret"]).now()}
    ).get_json()
    assert len(confirmed["backupCodes"]) == 8
    assert api.get("/session").get_json()["authenticated"] is True


# --- the manifest, as actually served ---------------------------------------
# Static shape is checked in test_assets.py. This is the other half: Flask
# guesses application/octet-stream for an unknown extension, and Chromium
# ignores a manifest under the wrong type WITHOUT any error — the only symptom
# is that "Install" never appears. Exactly the failure that needs a test rather
# than a memory.


@pytest.mark.skipif(
    not (Path("/home/shubh/projects/Bank-Spend/src/passbook/web/dist")
         / "manifest.webmanifest").is_file(),
    reason="bundle not built; run `make web-build`",
)
def test_the_manifest_is_served_as_manifest_json(app):
    response = app.test_client().get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.mimetype == "application/manifest+json", (
        f"served as {response.mimetype!r}; Chromium silently ignores anything else "
        "and Install never appears"
    )
    assert json.loads(response.data)["display"] == "standalone"


def test_the_manifest_type_does_not_depend_on_the_interpreter(app, monkeypatch):
    """Pins the app, not CPython.

    Written first as a plain request, which passed even with the app's explicit
    `mimetype` line deleted — because CPython has known `.webmanifest` since
    3.11, so `mimetypes` was quietly covering for it. A regression test that
    cannot fail is not a regression test. This removes the interpreter's
    knowledge and asserts the app still gets it right.
    """
    import mimetypes

    real = mimetypes.guess_type

    def blind(url, *args, **kwargs):
        if str(url).endswith(".webmanifest"):
            return (None, None)
        return real(url, *args, **kwargs)

    monkeypatch.setattr(mimetypes, "guess_type", blind)
    response = app.test_client().get("/manifest.webmanifest")
    assert response.mimetype == "application/manifest+json", (
        "with mimetypes blind to .webmanifest the app fell back to "
        f"{response.mimetype!r} — the explicit mimetype is the only thing "
        "standing between this and a manifest Chromium silently ignores"
    )


@pytest.mark.skipif(
    not (Path("/home/shubh/projects/Bank-Spend/src/passbook/web/dist") / "icon-512.png").is_file(),
    reason="bundle not built; run `make web-build`",
)
def test_the_icons_the_manifest_names_are_served(app):
    client = app.test_client()
    manifest = json.loads(client.get("/manifest.webmanifest").data)
    for icon in manifest["icons"]:
        response = client.get(icon["src"])
        assert response.status_code == 200, f"{icon['src']} is 404 at runtime"
        assert response.mimetype == "image/png"


def test_the_manifest_does_not_need_a_session(app):
    """It is fetched before sign-in; behind auth, install would never offer."""
    response = app.test_client().get("/manifest.webmanifest")
    assert response.status_code in (200, 503)  # 503 only when the bundle is absent


# --- the destructive action's precondition -----------------------------------
# SPEC §18.7. The button used to read "Back up, then purge and re-push" directly
# above a note explaining that this container cannot take a database dump. It
# promised the one thing the page had just said it could not do, on the only
# destructive action in the app.


def _dump(tmp_path, minutes_old: int) -> Path:
    import os
    import time

    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    dump = backups / "the ledger-2026-08-11.sql.gz"
    dump.write_bytes(b"not a real dump")
    when = time.time() - minutes_old * 60
    os.utime(dump, (when, when))
    return dump


def test_reapply_reports_whether_a_recent_dump_exists(signed_in, tmp_path, monkeypatch):
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )
    monkeypatch.setattr(api_module.service, "reapply_preview", lambda *a, **k: ([], 0))
    monkeypatch.setattr(
        api_base, "open_ledger", lambda *a, **k: FakeLedger([]))

    body = signed_in.get("/reapply").get_json()
    assert body["dump"]["fresh"] is False
    assert body["dump"]["ageMinutes"] is None

    _dump(tmp_path, minutes_old=5)
    body = signed_in.get("/reapply").get_json()
    assert body["dump"]["fresh"] is True
    assert body["dump"]["ageMinutes"] == 5
    assert body["dump"]["name"] == "the ledger-2026-08-11.sql.gz"


def test_a_purge_is_refused_without_a_recent_dump(signed_in, tmp_path, monkeypatch):
    """Refused on the SERVER. A disabled button is a courtesy; the thing standing
    between a purge and an unrecoverable ledger cannot live in the client."""
    shutil.copy(XLS_FIXTURE, tmp_path / "archive" / "statement.xls")

    r = signed_in.post("/reapply/run")
    assert r.status_code == 409
    body = r.get_json()
    assert body["code"] == "stale_backup"
    assert "make backup" in body["error"]

    _dump(tmp_path, minutes_old=180)
    r = signed_in.post("/reapply/run")
    assert r.status_code == 409
    assert "180 minutes old" in r.get_json()["error"]


def test_the_refusal_happens_before_anything_is_copied_or_deleted(
    signed_in, tmp_path, monkeypatch
):
    """The order is the whole point: no config tarball, no purge call, nothing."""
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )
    called = []
    monkeypatch.setattr(
        api_reconcile, "_run_config_backup", lambda: called.append("config") or "x"
    )
    monkeypatch.setattr(
        api_base, "open_ledger", lambda *a, **k: called.append("the ledger") or FakeLedger([])
    )

    assert signed_in.post("/reapply/run").status_code == 409
    assert called == [], f"work happened before the refusal: {called}"


def test_a_dump_that_is_exactly_at_the_limit_still_counts(signed_in, tmp_path, monkeypatch):
    from passbook import ops
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )
    monkeypatch.setattr(api_module.service, "reapply_preview", lambda *a, **k: ([], 0))
    monkeypatch.setattr(
        api_base, "open_ledger", lambda *a, **k: FakeLedger([]))

    _dump(tmp_path, minutes_old=ops.REAPPLY_DUMP_MAX_AGE_MINUTES)
    assert signed_in.get("/reapply").get_json()["dump"]["fresh"] is True


# --- backup codes warn before zero, not at zero ------------------------------


def test_the_low_backup_code_warning_starts_above_zero(app):
    """Codes are single-use, so the count only falls. At zero, a lost phone means
    `make web-totp RESET=yes` on the host is the only door left — which is too
    late to be told about it."""
    from passbook import webauth
    from passbook.web.auth import totp_status

    assert webauth.LOW_BACKUP_CODES >= 1
    for left, low in ((8, False), (webauth.LOW_BACKUP_CODES + 1, False),
                      (webauth.LOW_BACKUP_CODES, True), (0, True)):
        auth = make_auth(backup_codes=["x" * 64] * left)
        status = totp_status(auth)
        assert status["backupCodesLeft"] == left
        assert status["backupCodesLow"] is low, left


def test_remembered_devices_are_not_counted_as_backup_codes(app):
    """A device token's digest is the same shape as a backup code's — 64 hex
    characters — and `make check` was counting both, reporting 10 when there were
    8. It over-counted by exactly the number of remembered devices, in the
    direction that suppresses the warning."""
    from passbook.web.auth import totp_status

    auth = make_auth(
        backup_codes=["a" * 64, "b" * 64],
        devices=[
            {"token": "c" * 64, "expires": "2099-01-01T00:00:00+00:00"},
            {"token": "d" * 64, "expires": "2099-01-01T00:00:00+00:00"},
        ],
    )
    status = totp_status(auth)
    assert status["backupCodesLeft"] == 2
    assert status["backupCodesLow"] is True
    assert status["rememberedDevices"] == 2


def test_make_check_counts_codes_out_of_the_array(tmp_path):
    """The Makefile now reads the JSON instead of grepping for 64-hex strings.
    Asserted here because the shell branch that warns is not otherwise covered,
    and the bug it had was silent by construction."""
    import json
    import subprocess

    auth = tmp_path / "web-auth.json"
    auth.write_text(
        json.dumps(
            {
                "username": "operator",
                "backup_codes": ["a" * 64, "b" * 64],
                "devices": [{"token": "c" * 64}, {"token": "d" * 64}],
            }
        )
    )
    grepped = subprocess.run(
        ["grep", "-o", r'"[A-Fa-f0-9]\{64\}"', str(auth)], capture_output=True, text=True
    ).stdout.splitlines()
    assert len(grepped) == 4, "the old grep counted devices too"

    counted = subprocess.run(
        [
            "python3",
            "-c",
            'import json,sys; print(len(json.load(open(sys.argv[1])).get("backup_codes") or []))',
            str(auth),
        ],
        capture_output=True,
        text=True,
    )
    assert counted.stdout.strip() == "2"


# --- §61 the ledger browser --------------------------------------------------
#
# §52's lesson: `/banks/try` shipped untested and had two bugs that cost an
# evening, one of them a 500 nothing touched. A new endpoint gets tests with it.


def test_transactions_lists_the_ledger_newest_first(signed_in, app, monkeypatch, config_files):
    (config_files / "rules.yaml").write_text("rules: []\nnot_spend: []\n")
    _fake_ledger(
        monkeypatch,
        [
            _split("withdrawal", "100", category="Eating out", when="2026-06-01"),
            _split("deposit", "900", category="Salary", when="2026-06-09"),
        ],
    )
    body = signed_in.get("/transactions?range=all").get_json()
    assert [r["date"] for r in body["rows"]] == ["2026-06-09", "2026-06-01"]
    assert body["matched"] == 2
    assert [r["kind"] for r in body["rows"]] == ["deposit", "withdrawal"]


def test_transactions_never_returns_a_running_balance(signed_in, app, monkeypatch, config_files):
    """§16.4. A balance column over a filterable, reorderable view asserts a
    continuity that is not there, and §6.6 is the spine of this project. The
    statement sheet keeps its balance; this must never grow one."""
    (config_files / "rules.yaml").write_text("rules: []\nnot_spend: []\n")
    _fake_ledger(monkeypatch, [_split("withdrawal", "100", category="Eating out")])
    row = signed_in.get("/transactions?range=all").get_json()["rows"][0]
    assert "balance" not in row


def test_an_opening_balance_cannot_appear_as_a_row(
    signed_in, app, monkeypatch, config_files
):
    """It is a column on the account now, and it used to be a row.

    As a row it had no payee, no external_id and no Out or In value, so the
    page listed it as a blank line and counted one more than `verify-ledger`
    did — which reads as a phantom transaction. The ledger cannot hold one:
    `kind` is `withdrawal` or `deposit`, checked by the database.
    """
    (config_files / "rules.yaml").write_text("rules: []\nnot_spend: []\n")
    _fake_ledger(monkeypatch, [_split("withdrawal", "100", category="Eating out")])
    body = signed_in.get("/transactions?range=all").get_json()
    assert body["matched"] == 1
    assert [r["kind"] for r in body["rows"]] == ["withdrawal"]

    from passbook.store import LedgerError
    from passbook.store.memory import MemoryLedger

    store = MemoryLedger()
    store.store_account("Test Account", Decimal("12612.64"), date(2026, 5, 6), "INR")
    with pytest.raises(LedgerError):
        store.store_transaction(
            {**_split("opening balance", "12612", external_id="x"),
             "account": "Test Account", "notes": ""}
        )


def test_transactions_search_matches_the_raw_narration_too(
    signed_in, app, monkeypatch, config_files
):
    """The display name is no help when you are looking for a UTR, and that is
    the case this page exists to replace the ledger's search for."""
    (config_files / "rules.yaml").write_text("rules: []\nnot_spend: []\n")
    _fake_ledger(monkeypatch, [_split("withdrawal", "100", category="Eating out")])
    hit = signed_in.get("/transactions?range=all&q=eating").get_json()
    assert hit["matched"] == 1
    miss = signed_in.get("/transactions?range=all&q=zzzznothing").get_json()
    assert miss["matched"] == 0
    # `total` stays the unfiltered count, so the page can say "n of m".
    assert miss["total"] == 1


def test_transactions_filters_by_category_and_direction(
    signed_in, app, monkeypatch, config_files
):
    (config_files / "rules.yaml").write_text("rules: []\nnot_spend: []\n")
    _fake_ledger(
        monkeypatch,
        [
            _split("withdrawal", "100", category="Eating out"),
            _split("withdrawal", "40", category="Travel"),
            _split("deposit", "900", category="Salary"),
        ],
    )
    only_travel = signed_in.get("/transactions?range=all&category=Travel").get_json()
    assert only_travel["matched"] == 1
    only_in = signed_in.get("/transactions?range=all&direction=in").get_json()
    assert [r["kind"] for r in only_in["rows"]] == ["deposit"]
    only_out = signed_in.get("/transactions?range=all&direction=out").get_json()
    assert only_out["matched"] == 2


def test_transactions_filters_by_amount_band(signed_in, app, monkeypatch, config_files):
    """§83. The commonest analyst question the page could not express:
    "everything over ten thousand"."""
    (config_files / "rules.yaml").write_text("rules: []\nnot_spend: []\n")
    _fake_ledger(
        monkeypatch,
        [
            _split("withdrawal", "50", category="Eating out"),
            _split("withdrawal", "5000", category="Rent"),
            _split("withdrawal", "20000", category="Rent"),
        ],
    )
    big = signed_in.get("/transactions?range=all&min=1000").get_json()
    assert big["matched"] == 2
    band = signed_in.get("/transactions?range=all&min=1000&max=10000").get_json()
    assert band["matched"] == 1
    # A hand-edited URL shows the ledger, not an error page — as `_as_date` does.
    junk = signed_in.get("/transactions?range=all&min=abc").get_json()
    assert junk["matched"] == 3


def test_transactions_sorts_amounts_as_numbers_not_strings(
    signed_in, app, monkeypatch, config_files
):
    """`"9.00"` sorts above `"10000.00"` lexically, which is the kind of wrong
    that looks fine until the biggest row is missing from the top."""
    (config_files / "rules.yaml").write_text("rules: []\nnot_spend: []\n")
    _fake_ledger(
        monkeypatch,
        [
            _split("withdrawal", "9", category="Eating out", when="2026-06-01"),
            _split("withdrawal", "10000", category="Rent", when="2026-06-02"),
            _split("withdrawal", "500", category="Travel", when="2026-06-03"),
        ],
    )
    biggest = signed_in.get("/transactions?range=all&sort=amount").get_json()
    assert [r["amount"] for r in biggest["rows"]] == ["10000.00", "500.00", "9.00"]
    smallest = signed_in.get("/transactions?range=all&sort=amount-asc").get_json()
    assert [r["amount"] for r in smallest["rows"]] == ["9.00", "500.00", "10000.00"]
    oldest = signed_in.get("/transactions?range=all&sort=oldest").get_json()
    assert [r["date"] for r in oldest["rows"]] == ["2026-06-01", "2026-06-02", "2026-06-03"]


def test_transactions_offers_the_tags_that_are_actually_present(
    signed_in, app, monkeypatch, config_files
):
    """A tag filter you have to already know the value of is not a filter."""
    (config_files / "rules.yaml").write_text("rules: []\nnot_spend: []\n")
    _fake_ledger(
        monkeypatch,
        [
            _split("withdrawal", "50", category="Eating out", tags=("food",)),
            _split("withdrawal", "60", category="Mother", tags=("family", "food")),
        ],
    )
    body = signed_in.get("/transactions?range=all").get_json()
    assert body["tags"] == ["family", "food"]


def test_registering_an_account_sets_its_opening_balance(
    signed_in, app, monkeypatch, tmp_path
):
    """§95. Without it the ledger starts the account at zero and every figure on
    it is short by the opening amount forever — the account balances against
    nothing, and §20's balance check fails on a ledger that is otherwise
    perfectly correct.

    Caught the only way it could be: `verify-ledger` reported `opening balance
    MISSING` on a freshly registered account with no rows in it at all, before
    a single transaction had been pushed.
    """
    from passbook.web import api as api_module
    from passbook.web.api import (
        _base as api_base,
        _reconcile as api_reconcile,
        ops as api_ops,
        payees as api_payees,
    )

    # Seed another account, or the UPLOAD self-registers (§21.3) and /accounts
    # takes the idempotent path without ever reaching the ledger.
    _second_account(tmp_path)

    fake = FakeLedger([], account="Other")
    monkeypatch.setattr(
        api_base, "open_ledger", lambda *a, **k: fake)

    # Stage via /accounts/inspect: /statement refuses an unregistered account
    # and deletes the file (§21.7), which is right there and wrong here.
    staged = signed_in.upload_to("/accounts/inspect", XLS_FIXTURE.read_bytes(), "s.xls")
    assert staged.status_code == 200, staged.get_json()
    response = signed_in.post("/accounts", {"name": "Brand New"})
    assert response.status_code == 200, response.get_json()

    assert fake.stored, "no asset account was created"
    # The last one is the registration's: `/accounts` names it, and the upload
    # that staged the statement may have registered its own first.
    name, opening, on, currency = fake.stored[-1]
    assert opening == Decimal("10000.00"), name
    # The day BEFORE the period starts: dated on the first day it would sit
    # alongside that day's transactions and be ordered arbitrarily among them.
    assert on == date(2026, 5, 6)
    assert currency == "INR"


# --- the banner for a statement that is gone. SPEC §100.4 ---------------------


def test_the_pending_banner_goes_when_the_staged_file_does(signed_in, tmp_path):
    """`/overview` reported `pending` from a session key; `/statement/pending`
    checked the file that key names still exists. A container restart or a tmp
    sweep took the file and left the key, so the Ledger showed "review and push
    it" for the life of the session, linking to a Preview that answered
    "Nothing pending — upload a statement first"."""
    staged = tmp_path / "gone.xls"
    staged.write_bytes(b"x")
    with signed_in.client.session_transaction() as session:
        session["pending"] = str(staged)
        session["pending_password"] = "secret"
    assert signed_in.get("/overview").get_json()["pending"] is True

    staged.unlink()
    assert signed_in.get("/overview").get_json()["pending"] is False


def test_the_password_goes_with_it(signed_in, tmp_path):
    """§30. A password left behind is a credential kept for a file that no
    longer exists — the same three keys `discard_pending` drops."""
    staged = tmp_path / "gone.xls"
    staged.write_bytes(b"x")
    with signed_in.client.session_transaction() as session:
        session["pending"] = str(staged)
        session["pending_password"] = "secret"
        session["pending_encrypted"] = str(tmp_path / "enc.pdf")

    staged.unlink()
    signed_in.get("/overview")
    with signed_in.client.session_transaction() as session:
        assert "pending" not in session
        assert "pending_password" not in session
        assert "pending_encrypted" not in session

# --- §103 the credit-card split ----------------------------------------------
#
# `Attribution.keep` has existed since §73 and there was no way to fill it in:
# it is keyed on a namespaced `external_id`, and setting one meant opening a
# YAML file and pasting an id into it. That is the terminal this app exists to
# replace.
#
#   "Credit Card I say put in last month but give option to split some amount
#    if any in this month"
#   "I want split option to be in dropdown category in Payees in another
#    dropdown in just Credit Card"


def test_the_picker_offers_credit_card_before_anything_is_configured(signed_in):
    """The chicken and egg. The list of splittable rows is filtered by
    `Attribution.categories`, which is empty until `config/attribution.yaml`
    exists — and that file is written by splitting a row. Without a default the
    feature could never be switched on from the UI at all."""
    body = signed_in.get("/attribution").get_json()
    assert body["categories"] == sorted(service.DEFAULT_SETTLEMENT_CATEGORIES)
    assert body["configured"] is False


def test_a_bare_transaction_id_is_refused(signed_in):
    """Non-negotiable 10. The bank sequences `txn_id` per account, so a bare id
    would split the wrong account's bill the day a second account is
    registered — silently, and only in the month buckets."""
    response = signed_in.put("/attribution", {"externalId": "20260805000001", "keep": "5000"})
    assert response.status_code == 422
    assert "namespaced" in response.get_json()["error"]


def test_a_negative_amount_is_refused(signed_in):
    response = signed_in.put(
        "/attribution", {"externalId": "canara-1111-20260805000001", "keep": "-1"}
    )
    assert response.status_code == 422


def test_nonsense_is_refused(signed_in):
    response = signed_in.put(
        "/attribution", {"externalId": "canara-1111-20260805000001", "keep": "five thousand"}
    )
    assert response.status_code == 422


def test_saving_a_split_does_not_switch_the_shift_on(signed_in, tmp_path, monkeypatch):
    """§103.3. It used to, on the reasoning that a `keep` in a config with no
    `categories` records an exception to a rule that does not exist.

    The cure was worse: typing 500 into one box moved **three** month buckets by
    tens of thousands, because turning the policy on shifts every qualifying
    bill and not only the one being edited. The operator called it broken and
    was describing the surprise rather than a fault.
    """
    from passbook import config, configwrite

    target = tmp_path / "attribution.yaml"
    monkeypatch.setattr(config, "ATTRIBUTION_FILE", target)
    monkeypatch.setattr(configwrite, "ATTRIBUTION_FILE", target)

    response = signed_in.put(
        "/attribution", {"externalId": "canara-1111-20260805000001", "keep": "5000.00"}
    )
    assert response.status_code == 200, response.get_json()

    loaded = config.load_attribution(target)
    assert loaded.keep["canara-1111-20260805000001"] == Decimal("5000.00")
    assert loaded.categories == frozenset(), "saving a split moved a month"
    # Money, as a string. A float in a config file is a float in a ledger.
    assert "5000.00" in target.read_text(encoding="utf-8")


def test_the_settlement_policy_is_its_own_control(signed_in, config_files, tmp_path, monkeypatch):
    """Two decisions, two controls. This is the one that moves whole months."""
    from passbook import config, configwrite

    target = tmp_path / "attribution.yaml"
    monkeypatch.setattr(config, "ATTRIBUTION_FILE", target)
    monkeypatch.setattr(configwrite, "ATTRIBUTION_FILE", target)

    # Whatever the fixture's rules file actually names — the point is the
    # policy, not which category it is about.
    category = signed_in.get("/categories").get_json()["categories"][0]

    on = signed_in.put("/attribution/settlement", {"category": category, "settles": True})
    assert on.status_code == 200, on.get_json()
    assert config.load_attribution(target).categories == frozenset({category})

    off = signed_in.put("/attribution/settlement", {"category": category, "settles": False})
    assert off.status_code == 200
    # Off is the ABSENCE of the category, not a flag saying false — an empty
    # list shifts nothing, which is the behaviour before the feature existed.
    assert config.load_attribution(target).categories == frozenset()


def test_a_settlement_policy_for_an_unknown_category_is_refused(signed_in, config_files):
    """A policy naming a category no rule can assign is a rule that can never
    fire — the same refusal `/categories` makes, for the same reason."""
    response = signed_in.put(
        "/attribution/settlement", {"category": "Nonsense", "settles": True}
    )
    assert response.status_code == 422
    assert response.get_json()["code"] == "unknown_category"


def test_clearing_a_split_removes_the_entry(signed_in, tmp_path, monkeypatch):
    """"Nothing is kept" is the absence of a rule, not a rule saying zero — so
    the key goes, rather than being left behind as `0.00` for a later reader to
    wonder about."""
    from passbook import config, configwrite

    target = tmp_path / "attribution.yaml"
    monkeypatch.setattr(config, "ATTRIBUTION_FILE", target)
    monkeypatch.setattr(configwrite, "ATTRIBUTION_FILE", target)

    external = "canara-1111-20260805000001"
    signed_in.put("/attribution", {"externalId": external, "keep": "5000"})
    signed_in.put("/attribution", {"externalId": external, "keep": ""})

    assert external not in config.load_attribution(target).keep


@pytest.mark.parametrize("amount", ["5000", "5000.00", " 5000.5 "])
def test_the_shapes_an_operator_actually_types(signed_in, tmp_path, monkeypatch, amount):
    from passbook import config, configwrite

    target = tmp_path / "attribution.yaml"
    monkeypatch.setattr(config, "ATTRIBUTION_FILE", target)
    monkeypatch.setattr(configwrite, "ATTRIBUTION_FILE", target)

    response = signed_in.put(
        "/attribution", {"externalId": "canara-1111-20260805000001", "keep": amount}
    )
    assert response.status_code == 200, response.get_json()


def test_a_split_moves_no_total(signed_in, tmp_path, monkeypatch):
    """Non-negotiable 19, asserted through the API rather than in the unit that
    already covers it: a reporting shift that changed a figure would be
    inventing money."""
    from passbook import config, configwrite

    target = tmp_path / "attribution.yaml"
    monkeypatch.setattr(config, "ATTRIBUTION_FILE", target)
    monkeypatch.setattr(configwrite, "ATTRIBUTION_FILE", target)

    before = signed_in.get("/analysis").get_json()
    rows = signed_in.get("/attribution").get_json()["rows"]
    if not rows:
        pytest.skip("no settlement rows in the fixture window")
    signed_in.put("/attribution", {"externalId": rows[0]["externalId"], "keep": "1.00"})
    after = signed_in.get("/analysis").get_json()

    for figure in ("spend", "grossSpend", "income", "grossIncome", "net"):
        assert before[figure] == after[figure], figure


# --- §104 what belongs to the account on screen -------------------------------


def _two_accounts(tmp_path):
    """A second registered account with nothing pushed to it — which is the
    state that made the scoping visible in the first place."""
    from passbook.config import Account, load_accounts, save_accounts

    existing = load_accounts()
    if any(a.slug == "other-9999" for a in existing):
        return
    save_accounts([
        *existing,
        Account(
            slug="other-9999",
            bank="union",
            account_number="9999999999",
            asset_account="Other ****9999",
            label="Other ****9999",
        ),
    ])


def test_the_sync_warning_belongs_to_the_bank_it_is_about(signed_in, app, tmp_path):
    """*"this shouldnt be seen on ledger when union is selected"*.

    `sync_status` answered for the whole archive, so a Canara download warning —
    which is about one bank's download window and nothing else — appeared on
    every account's page. An account nobody has pushed to has never synced, and
    that is a different sentence with a different remedy.
    """
    _two_accounts(tmp_path)
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    mine = signed_in.get("/overview?account=canara-1111").get_json()
    theirs = signed_in.get("/overview?account=other-9999").get_json()

    assert mine["sync"]["state"] != "never"
    assert theirs["sync"]["state"] == "never"
    assert "Other ****9999" in theirs["sync"]["headline"]


def test_recently_archived_lists_only_this_accounts_statements(signed_in, app, tmp_path):
    """The same mistake one column over."""
    _two_accounts(tmp_path)
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    mine = signed_in.get("/overview?account=canara-1111").get_json()["history"]
    theirs = signed_in.get("/overview?account=other-9999").get_json()["history"]

    assert mine, "the account with statements lists none"
    assert theirs == []


def test_combining_accounts_shows_both_histories(signed_in, app, tmp_path):
    """Scoping must not become filtering-to-one. `all` is a real scope."""
    _two_accounts(tmp_path)
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    combined = signed_in.get("/overview?account=all").get_json()["history"]
    mine = signed_in.get("/overview?account=canara-1111").get_json()["history"]
    assert len(combined) >= len(mine)


def test_the_staged_banner_shows_only_on_the_account_it_is_for(signed_in, tmp_path):
    """A staged statement was routed by what the FILE says (§21.9), so it
    belongs to one account. Shown on every tab it reads as "push this" on a page
    where pushing it would change nothing."""
    _two_accounts(tmp_path)
    staged = tmp_path / "staged.xls"
    staged.write_bytes(b"x")
    with signed_in.client.session_transaction() as session:
        session["pending"] = str(staged)
        session["pending_slug"] = "canara-1111"

    assert signed_in.get("/overview?account=canara-1111").get_json()["pending"] is True
    assert signed_in.get("/overview?account=other-9999").get_json()["pending"] is False
    # And on a combined scope it shows, because one of the accounts is its own.
    assert signed_in.get("/overview?account=all").get_json()["pending"] is True


def test_a_statement_staged_before_any_account_exists_shows_everywhere(signed_in, tmp_path):
    """The inspect path runs before an account is registered, so there is no
    slug to scope by — and that statement is precisely the thing standing
    between the operator and a registered account."""
    _two_accounts(tmp_path)
    staged = tmp_path / "unrouted.xls"
    staged.write_bytes(b"x")
    with signed_in.client.session_transaction() as session:
        session["pending"] = str(staged)
        session.pop("pending_slug", None)

    assert signed_in.get("/overview?account=other-9999").get_json()["pending"] is True


def test_a_combined_scope_reports_the_worst_account_not_the_newest_file(signed_in, app, tmp_path):
    """§18, reintroduced by §104 and caught the same day. Scoping made
    `sync_status` take the newest file across the scope — so a three-week-old
    gap on one account disappears behind a statement pushed this morning on
    another. One fresh account must not answer for a stale one.
    """
    import os
    import time

    _two_accounts(tmp_path)
    archive = app.config["ARCHIVE"]
    stale = archive / "canara-1111" / "old.xls"
    stale.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(XLS_FIXTURE, stale)
    old = time.time() - 60 * 60 * 24 * 40
    os.utime(stale, (old, old))

    combined = signed_in.get("/overview?account=all").get_json()["sync"]
    just_this_one = signed_in.get("/overview?account=canara-1111").get_json()["sync"]
    assert combined["state"] == just_this_one["state"] == "stale"

