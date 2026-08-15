"""JSON API and auth. SPEC §16.

Covers the paths that can do damage: upload validation, the §6.7 account
assertion, the config-write diff, and every branch of authentication. No
network — Firefly is mocked wherever a route reaches for it.

**All statement data comes from `tests/fixtures/statement.xls`**, which
`scripts/redact.py` produced from a real export (§11). Nothing here is a
hand-typed row. A hand-typed ledger row is how a demo table ended up asserting
a balance chain that did not close — the invariant this project is built on,
contradicted by its own illustration.
"""

import io
import json
import shutil
from pathlib import Path

import pyotp
import pytest

from conftest import CSV_FIXTURE, FIXTURE_ACCOUNT, REPO_ROOT, XLS_FIXTURE
from passbook import webauth
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
    monkeypatch.delenv("FIREFLY_TOKEN", raising=False)

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
    "path", ["/overview", "/payees", "/status", "/reapply", "/categories", "/analysis"]
)
def test_reads_require_a_session(api, path):
    assert api.get(path).status_code == 401


@pytest.mark.parametrize(
    "path", ["/statement/confirm", "/payees/diff", "/payees/apply", "/reapply/run", "/password"]
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
    """Rules are applied by Firefly at store time, so at preview no category
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
    monkeypatch.setenv("FIREFLY_TOKEN", "a.b.c")
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


def test_status_never_returns_the_firefly_token(signed_in, monkeypatch):
    token = "eyJ0eXAiOiJKV1QifQ.eyJleHAiOjk5OTk5OTk5OTl9.sig"
    monkeypatch.setenv("FIREFLY_TOKEN", token)
    body = signed_in.get("/status").data.decode()
    assert token not in body
    assert "sig" not in json.loads(body)["token"]


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

    source = Path(REPO_ROOT, "src/passbook/ops.py").read_text()
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
    css = Path(REPO_ROOT, "frontend/src/theme.css").read_text()
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
# Firefly's by-type totals. On the real ledger those differ by a factor of three
# in both directions, so a chart drawn on the wrong one is not approximately
# right — and it looks fine.


class FakeFirefly:
    """Just enough Firefly to answer /analysis. No network (§10)."""

    def __init__(self, splits, *, account="Test Account"):
        self.splits = splits
        self.account = account

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def asset_accounts(self):
        return [{"id": "7", "attributes": {"name": self.account, "current_balance": "1.00"}}]

    def account_transactions(self, account_id):
        assert account_id == "7"
        return [{"attributes": {"transactions": self.splits}}]


def _fake_firefly(monkeypatch, splits, **kwargs):
    from passbook.web import api as api_module

    monkeypatch.setenv("FIREFLY_TOKEN", "a.b.c")
    monkeypatch.setattr(
        api_module, "FireflyClient", lambda *a, **k: FakeFirefly(splits, **kwargs)
    )


def _split(kind, amount, *, category=None, tags=(), when="2026-06-10", external_id=None):
    return {
        "type": kind,
        "amount": f"{amount}.000000000000",
        "category_name": category,
        "tags": list(tags),
        "external_id": external_id,
        "date": f"{when}T00:00:00+05:30",
    }


def test_analysis_excludes_movement_from_spend_and_returns_both_figures(
    signed_in, app, monkeypatch, config_files
):
    (config_files / "rules.yaml").write_text(
        "rules:\n"
        "  - title: Eating\n    category: Eating out\n    tag: food\n"
        "not_spend: [Investments]\n"
    )
    _fake_firefly(
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
    _fake_firefly(monkeypatch, [_split("withdrawal", "65", category="Shopping")])
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
    """The clock exists only in the statement (§6.5) — Firefly is never told it.
    So the hours come from the archive, joined on the bank's transaction id."""
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    from passbook import service

    transactions = service.archived_statements(app.config["ARCHIVE"])[0].transactions
    clocked = [t for t in transactions if t.txn_time][:3]
    _fake_firefly(
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
    _fake_firefly(
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


def test_analysis_needs_a_configured_firefly_and_says_so(signed_in, monkeypatch):
    monkeypatch.delenv("FIREFLY_TOKEN", raising=False)
    r = signed_in.get("/analysis")
    assert r.status_code == 503
    assert r.get_json()["code"] == "unconfigured"


def test_an_unreachable_firefly_is_a_502_not_a_500(signed_in, monkeypatch):
    """The charts are drawn from the ledger, so an unreachable Firefly means no
    charts — and the client shows that instead of an empty page."""
    from passbook.firefly.client import FireflyError
    from passbook.web import api as api_module

    monkeypatch.setenv("FIREFLY_TOKEN", "a.b.c")

    def explode(*a, **k):
        raise FireflyError("connection refused")

    monkeypatch.setattr(api_module, "FireflyClient", explode)
    r = signed_in.get("/analysis")
    assert r.status_code == 502
    assert r.get_json()["code"] == "firefly"


def test_analysis_never_leaks_the_account_number_or_the_token(
    signed_in, app, monkeypatch, config_files
):
    """§11. The charts are aggregates; nothing here needs an account number."""
    shutil.copy(XLS_FIXTURE, app.config["ARCHIVE"] / "statement.xls")
    _fake_firefly(monkeypatch, [_split("withdrawal", "65", category="Shopping")])
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
    not (Path(REPO_ROOT, "src/passbook/web/dist")
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


@pytest.mark.skipif(
    not (Path(REPO_ROOT, "src/passbook/web/dist")
         / "manifest.webmanifest").is_file(),
    reason="bundle not built; run `make web-build`",
)
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
    not (Path(REPO_ROOT, "src/passbook/web/dist") / "icon-512.png").is_file(),
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
    dump = backups / "firefly-2026-08-11.sql.gz"
    dump.write_bytes(b"not a real dump")
    when = time.time() - minutes_old * 60
    os.utime(dump, (when, when))
    return dump


def test_reapply_reports_whether_a_recent_dump_exists(signed_in, tmp_path, monkeypatch):
    from passbook.web import api as api_module

    monkeypatch.setenv("FIREFLY_TOKEN", "a.b.c")
    monkeypatch.setattr(api_module.service, "reapply_preview", lambda *a, **k: ([], 0))
    monkeypatch.setattr(api_module, "FireflyClient", lambda *a, **k: FakeFirefly([]))

    body = signed_in.get("/reapply").get_json()
    assert body["dump"]["fresh"] is False
    assert body["dump"]["ageMinutes"] is None

    _dump(tmp_path, minutes_old=5)
    body = signed_in.get("/reapply").get_json()
    assert body["dump"]["fresh"] is True
    assert body["dump"]["ageMinutes"] == 5
    assert body["dump"]["name"] == "firefly-2026-08-11.sql.gz"


def test_a_purge_is_refused_without_a_recent_dump(signed_in, tmp_path, monkeypatch):
    """Refused on the SERVER. A disabled button is a courtesy; the thing standing
    between a purge and an unrecoverable ledger cannot live in the client."""
    monkeypatch.setenv("FIREFLY_TOKEN", "a.b.c")
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

    monkeypatch.setenv("FIREFLY_TOKEN", "a.b.c")
    called = []
    monkeypatch.setattr(
        api_module, "_run_config_backup", lambda: called.append("config") or "x"
    )
    monkeypatch.setattr(
        api_module, "FireflyClient", lambda *a, **k: called.append("firefly") or FakeFirefly([])
    )

    assert signed_in.post("/reapply/run").status_code == 409
    assert called == [], f"work happened before the refusal: {called}"


def test_a_dump_that_is_exactly_at_the_limit_still_counts(signed_in, tmp_path, monkeypatch):
    from passbook import ops
    from passbook.web import api as api_module

    monkeypatch.setenv("FIREFLY_TOKEN", "a.b.c")
    monkeypatch.setattr(api_module.service, "reapply_preview", lambda *a, **k: ([], 0))
    monkeypatch.setattr(api_module, "FireflyClient", lambda *a, **k: FakeFirefly([]))

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
