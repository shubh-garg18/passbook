"""JSON API. SPEC §16.1.

Every route delegates to `passbook.service` — the same code `passbook sync`
runs. There is no parsing here, no push logic, no categorisation. The Phase 7
rule survives the rewrite intact: **this is a front end over service.py, never a
second implementation.**

Three constraints shape every response:

* **D5/D10 — never guess a category.** Unknown tokens are surfaced and asked
  about. `/api/categories` returns only categories that already have a rule;
  writing an unlisted one is refused with the list of known ones.
* **§6.7 — the account assertion applies here.** A statement from another
  account is refused at upload, before anything can be pushed, and the staged
  file is deleted rather than left where `make sync` would find it.
* **§11 — no secrets cross this boundary.** Account numbers are masked to last
  4 by `StatementMeta.masked_account`. The Firefly token, the DB password, the
  customer ID and the TOTP secret never appear in a response body or a log
  line. The one exception is the TOTP secret at the moment of enrolment, which
  is the entire point of that request and is returned exactly once.

**Money is serialised as a decimal string, never a JSON number.** A JSON number
is an IEEE double the moment it is parsed, and the first non-negotiable
does not stop at the process boundary. The client formats the string without
ever converting it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml
from flask import Blueprint, current_app, jsonify, request, session
from werkzeug.utils import secure_filename

from .. import ops, service, webauth
from ..config import (
    ACCOUNTS_FILE,
    BUILTIN_BANKS,
    SUPPORTED_BANKS,
    RegistryError,
    alias_drift,
    find_account,
    load_accounts,
    load_attribution,
    load_payee_aliases,
    load_settings,
    save_accounts,
    token_expiry,
)
from ..configwrite import known_categories, plan_aliases, plan_categories
from ..firefly.bootstrap import bootstrap as bootstrap_rules
from ..firefly.bootstrap import load_rules
from ..firefly.push import CURRENCY
from ..firefly.client import FireflyClient, FireflyError, ValidationFailed
from ..firefly.purge import find_candidates
from ..firefly.purge import purge as purge_transactions
from ..loaders import UnsupportedFormat, profiles, read_grid, sniff
from ..loaders._table import CORE_COLS, REQUIRED_COLS, ParseError, _all_aliases
from ..loaders._table import norm as COL_ALIASES_NORM
from ..loaders.pdf import PdfPasswordRequired, PdfPasswordWrong
from ..models import StatementMeta, mask_account
from ..validate import AccountMismatch, BalanceBreak, IntegrityError, UnknownAccount
from . import auth as A

log = logging.getLogger(__name__)

api = Blueprint("api", __name__, url_prefix="/api")

# A Canara three-month export is ~30 KB. Ten megabytes is already absurd, and
# refusing early keeps a mistake from becoming a disk problem.
#: How long an account's own name may be. Long enough for "Canara joint —
#: household", short enough that the switcher stays a strip rather than a wall.
ACCOUNT_LABEL_MAX = 40

MAX_UPLOAD_BYTES = 10 * 1024 * 1024

#: How many banded rows the "try it" preview hands back. Enough to see the
#: header, a sentinel and several transactions; not the whole statement.
BANDED_PREVIEW = 14

#: How many lines the shareable shape dump covers. Enough to reach the
#: transaction header past a details table, which is what needs looking at.
SHAPE_LINES = 40

# §6.2 dispatches on magic bytes, never on the extension.
ACCEPTED_SNIFF = {"xls", "xlsx", "html_table", "delimited", "pdf"}

MIN_PASSWORD_LENGTH = 12


# --- serialisation --------------------------------------------------------


def _money(value) -> str | None:
    """Decimal -> exact decimal string. Never a float, never a JSON number."""
    if value is None:
        return None
    return f"{Decimal(value):.2f}"


def _txn(t) -> dict:
    return {
        "id": t.txn_id,
        "date": t.txn_date.isoformat(),
        # The Day Rail's whole input. None for the rows whose narration carries
        # no clock — rendered as an explicit absence, never as midnight.
        "time": t.txn_time.strftime("%H:%M:%S") if t.txn_time else None,
        "channel": t.channel,
        "payee": t.payee,
        "alias": t.payee_alias,
        "display": t.payee_alias or t.payee,
        "debit": _money(t.debit),
        "credit": _money(t.credit),
        "balance": _money(t.balance),
        "reversal": t.is_reversal,
    }


def _parsed(parsed, *, filename: str | None = None) -> dict:
    """A statement, ready to render.

    Rows are emitted in sheet order and complete — the Preview table shows a
    Balance column, and a Balance column over a filtered or reordered subset
    asserts a continuity that is not there. §6.6 is the spine of this project;
    a view that appears to break it teaches the operator to distrust the check.
    """
    meta = parsed.meta
    return {
        "filename": filename or parsed.path.name,
        "meta": {
            # masked_account is last-4 only. The full number never leaves here.
            "account": meta.masked_account,
            "periodFrom": meta.period_from.isoformat(),
            "periodTo": meta.period_to.isoformat(),
            "openingBalance": _money(meta.opening_balance),
            "closingBalance": _money(meta.closing_balance),
        },
        "count": len(parsed.transactions),
        "withdrawn": _money(parsed.debits),
        "deposited": _money(parsed.credits),
        "warnings": parsed.warnings,
        "transactions": [_txn(t) for t in parsed.transactions],
    }


def _payee_row(r) -> dict:
    return {
        "token": r.token,
        "alias": r.alias,
        "category": r.category,
        "channel": r.channel,
        "count": r.count,
        "withdrawn": _money(r.withdrawn),
        "deposited": _money(r.deposited),
        "total": _money(r.total),
        "first": r.first,
        "last": r.last,
        "needsDecision": r.needs_decision,
    }


def _artefact(a) -> dict:
    return {
        "name": a.name,
        "size": a.size,
        "humanSize": a.human_size,
        "modified": a.modified,
        "ageDays": a.age_days,
    }


def _sync(status) -> dict:
    return {
        "state": status.state,
        "age": status.age,
        "filename": status.filename,
        "headline": status.headline,
        "detail": status.detail,
    }


def _fail(message: str, code: str = "error", status: int = 400):
    return jsonify({"error": message, "code": code}), status


# --- session --------------------------------------------------------------


@api.get("/session")
def get_session():
    auth = A.current_auth()
    stage = "anonymous"
    if A.is_authenticated():
        stage = "done"
    elif A.pending_username():
        stage = "enroll" if not auth.totp_enrolled else "totp"

    return jsonify(
        {
            "authenticated": A.is_authenticated(),
            "username": session.get(A.SESSION_KEY),
            "stage": stage,
            "configured": auth.configured,
            "totp": A.totp_status(auth),
        }
    )


@api.post("/session")
def login():
    """Step one: username and password."""
    body = request.get_json(silent=True) or {}
    username = str(body.get("username") or "")
    password = str(body.get("password") or "")

    locked, seconds = A.throttle_state(username)
    if locked:
        return _fail(
            f"Too many attempts. Try again in {seconds // 60 + 1} minute(s).",
            "rate_limited",
            429,
        )

    # An install with NO credentials at all says so, plainly.
    #
    # Everywhere else this endpoint is deliberately vague — which half was
    # wrong is free information to an attacker. Here there is nothing to be
    # vague about: no account exists, so there is nothing to enumerate, and
    # the generic "Sign-in failed." is indistinguishable from a wrong
    # password. That is the guaranteed state after any real recovery (the
    # credential file is deliberately not in the backup, §16.9), and the DR
    # drill found an operator would meet a login box that rejects everything
    # with the actual reason visible only in `docker compose logs web`.
    if not A.current_auth().configured:
        webauth.verify_password(None, password)  # keep the work constant regardless
        log.warning("sign-in attempted, but no web credentials are configured")
        return _fail(
            "No web credentials are set on this install. On the host, run "
            "`make web-password`, then sign in and enrol an authenticator. "
            "This is expected after a restore — the credential file is "
            "deliberately not carried in the backup.",
            "not_configured",
            503,
        )

    ok, reason = A.check_password(username, password)
    if not ok:
        A.record_failure(username)
        # The client learns only that it failed. The log says which half, so a
        # misconfiguration is distinguishable from a typo without the page
        # leaking which usernames exist. Never the password, never the hash.
        log.warning("login failed: %s (submitted username=%r)", reason, username[:64])
        return _fail("Sign-in failed.", "bad_credentials", 401)

    A.record_success(username)
    auth = A.current_auth()

    # Enrolment is mandatory, so an un-enrolled operator cannot slip past it by
    # simply not visiting the page.
    if not auth.totp_enrolled:
        A.begin_pending(username)
        return jsonify({"stage": "enroll"})

    # A remembered device skips the second factor, never the first.
    if webauth.device_valid(auth, request.cookies.get(A.DEVICE_COOKIE)):
        A.begin_session(username)
        log.info("signed in with a remembered device")
        return jsonify({"stage": "done", "rememberedDevice": True})

    A.begin_pending(username)
    return jsonify({"stage": "totp"})


@api.post("/session/totp")
def login_totp():
    """Step two: a TOTP code, or a single-use backup code."""
    username = A.pending_username()
    if not username:
        return _fail("Start again — the sign-in attempt expired.", "expired", 401)

    locked, seconds = A.throttle_state(username)
    if locked:
        return _fail(
            f"Too many attempts. Try again in {seconds // 60 + 1} minute(s).",
            "rate_limited",
            429,
        )

    body = request.get_json(silent=True) or {}
    auth = A.current_auth()
    ok, reason = A.check_second_factor(
        auth, str(body.get("code") or ""), str(body.get("backupCode") or "")
    )
    if not ok:
        A.record_failure(username)
        log.warning("second factor failed: %s", reason)
        return _fail("That code did not work.", "bad_code", 401)

    A.record_success(username)
    A.begin_session(username)

    response = jsonify(
        {"stage": "done", "backupCodesLeft": auth.backup_codes_left}
    )
    if body.get("remember"):
        token = webauth.new_device_token()
        webauth.remember_device(auth, token)
        A.store_auth(auth)
        response.set_cookie(
            A.DEVICE_COOKIE,
            token,
            max_age=webauth.DEVICE_REMEMBER_DAYS * 86400,
            httponly=True,
            samesite="Strict",
            secure=current_app.config.get("SECURE_COOKIES", False),
            path="/",
        )
    return response


@api.delete("/session")
def logout():
    session.clear()
    return jsonify({"ok": True})


# --- TOTP enrolment -------------------------------------------------------


@api.post("/totp/enroll/start")
def totp_enroll_start():
    """Mint a candidate secret and return it once, with its QR.

    Not stored yet: an interrupted enrolment must not leave a secret the
    operator never scanned, which would lock them out on the next sign-in.
    """
    username = A.pending_username() or session.get(A.SESSION_KEY)
    if not username:
        return _fail("Not signed in.", "unauthenticated", 401)
    auth = A.current_auth()
    if auth.totp_enrolled and not A.is_authenticated():
        return _fail("Already enrolled.", "already_enrolled", 409)

    secret = webauth.new_totp_secret()
    session["totp_candidate"] = secret
    uri = webauth.totp_uri(secret, username)
    return jsonify(
        {
            "secret": secret,
            "secretPretty": webauth.b32_pretty(secret),
            "uri": uri,
            "qr": webauth.totp_qr_svg(uri),
        }
    )


@api.post("/totp/enroll/confirm")
def totp_enroll_confirm():
    """Prove the authenticator works, then issue the backup codes once."""
    username = A.pending_username() or session.get(A.SESSION_KEY)
    if not username:
        return _fail("Not signed in.", "unauthenticated", 401)
    candidate = session.get("totp_candidate")
    if not candidate:
        return _fail("Start enrolment again.", "expired", 409)

    body = request.get_json(silent=True) or {}
    auth = A.current_auth()

    probe = webauth.WebAuth(totp_secret=candidate)
    if not webauth.verify_totp(probe, str(body.get("code") or "")):
        log.warning("TOTP enrolment code rejected")
        return _fail("That code did not match. Check the clock on your phone.", "bad_code", 400)

    auth.totp_secret = candidate
    auth.totp_enrolled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    auth.totp_last_counter = probe.totp_last_counter
    codes = webauth.generate_backup_codes(auth)
    A.store_auth(auth)
    session.pop("totp_candidate", None)
    A.begin_session(username)
    log.info("TOTP enrolled; %d backup codes issued", len(codes))

    # The only time these are ever readable. Stored as salted digests.
    return jsonify({"stage": "done", "backupCodes": codes})


@api.post("/totp/backup-codes")
@A.login_required
def regenerate_backup_codes():
    """Re-issue all eight. Re-authenticates first — this invalidates the old
    set, so a borrowed session must not be able to do it."""
    body = request.get_json(silent=True) or {}
    ok, reason = A.check_password(
        session.get(A.SESSION_KEY) or "", str(body.get("password") or "")
    )
    if not ok:
        log.warning("backup-code regeneration refused: %s", reason)
        return _fail("Password is incorrect.", "bad_credentials", 401)
    auth = A.current_auth()
    codes = webauth.generate_backup_codes(auth)
    A.store_auth(auth)
    log.info("backup codes regenerated")
    return jsonify({"backupCodes": codes})


@api.post("/devices/forget")
@A.login_required
def forget_devices():
    auth = A.current_auth()
    count = webauth.forget_devices(auth)
    A.store_auth(auth)
    return jsonify({"forgotten": count})


@api.post("/password")
@A.login_required
def change_password():
    body = request.get_json(silent=True) or {}
    username = session.get(A.SESSION_KEY) or ""
    ok, reason = A.check_password(username, str(body.get("current") or ""))
    if not ok:
        log.warning("password change refused: %s", reason)
        return _fail("Current password is incorrect.", "bad_credentials", 401)

    new = str(body.get("new") or "")
    if len(new) < MIN_PASSWORD_LENGTH:
        return _fail(f"New password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if new != str(body.get("confirm") or ""):
        return _fail("New passwords do not match.")

    auth = A.current_auth()
    auth.password_hash = webauth.hash_password(new)
    A.store_auth(auth)
    log.info("web password changed")
    session.clear()
    return jsonify({"ok": True})


# --- account scope. SPEC §21.9 -----------------------------------------------
# Every read endpoint takes `?account=<slug>` or `?account=all`. The default is
# the first registered account, so a single-account install behaves exactly as it
# did before this phase and never sees a switcher (§21.3).

ALL_ACCOUNTS = "all"


def _account_scope(default_to_first: bool = True):
    """Return `(accounts_in_scope, selected)` for this request.

    `selected` is a slug, `"all"`, or None when nothing is registered. A slug the
    registry does not know falls back to the first account rather than 404ing: a
    stale selection in someone's browser must not break the page it is stored for.
    """
    registry = load_accounts()
    wanted = (request.args.get("account") or "").strip()
    if not registry:
        return [], None
    if wanted == ALL_ACCOUNTS and len(registry) > 1:
        return registry, ALL_ACCOUNTS
    chosen = next((a for a in registry if a.slug == wanted), None)
    if chosen is None:
        chosen = registry[0] if default_to_first else None
    return ([chosen] if chosen else []), (chosen.slug if chosen else None)


# -- the window --------------------------------------------------------------
# SPEC §26. Every figure on every page answers "over what period", and until
# this existed the answer was always "everything ever archived" — which is the
# one window nobody asks about. A named range or an explicit from/to, resolved
# server-side and echoed back, so the client renders the scope the server used
# rather than the one it asked for.

RANGES = ("month", "last-month", "3m", "6m", "year", "all")
DEFAULT_RANGE = "all"

def _range_bounds(name: str, today: date) -> tuple[date | None, date | None]:
    """`(from, to)` inclusive, or `(None, None)` for everything."""
    first = today.replace(day=1)
    if name == "month":
        return first, today
    if name == "last-month":
        end = first - timedelta(days=1)
        return end.replace(day=1), end
    if name == "3m":
        return _months_back(first, 2), today
    if name == "6m":
        return _months_back(first, 5), today
    if name == "year":
        return today.replace(month=1, day=1), today
    return None, None

def _months_back(first_of_month: date, months: int) -> date:
    """N whole months before this one, staying on the 1st.

    Arithmetic on the 1st only, so there is no end-of-month case to get wrong —
    the bug that would otherwise show up once a year, in March.
    """
    month = first_of_month.month - months
    year = first_of_month.year
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)

def _date_scope() -> tuple[date | None, date | None, dict]:
    """The requested window, plus what to echo back to the client.

    An explicit `from`/`to` beats a named range, so a custom window survives a
    reload. An unparseable date is ignored rather than 400ing: a hand-edited URL
    should show the ledger, not an error page.
    """
    name = (request.args.get("range") or "").strip().lower()
    raw_from = (request.args.get("from") or "").strip()
    raw_to = (request.args.get("to") or "").strip()

    start = _as_date(raw_from)
    end = _as_date(raw_to)
    if start or end:
        # Swapped by hand or by a date picker that allows it. Ordering them is
        # kinder than refusing, and an empty result would look like no data.
        if start and end and start > end:
            start, end = end, start
        return start, end, {"range": "custom", "from": _iso(start), "to": _iso(end)}

    if name not in RANGES:
        name = DEFAULT_RANGE
    start, end = _range_bounds(name, date.today())
    return start, end, {"range": name, "from": _iso(start), "to": _iso(end)}

def _as_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None

def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None

def _splits_within(splits: list[dict], start: date | None, end: date | None) -> list[dict]:
    """The same window, over Firefly splits rather than parsed transactions.

    A split's `date` is an ISO-8601 *datetime* with an offset
    (`2026-08-24T00:00:00+05:30`), so it is cut at the `T` rather than parsed:
    the window is a range of calendar days in `Asia/Kolkata`, and converting to
    a datetime only to drop the time again invites a timezone shift that would
    silently move a midnight transaction into the previous day.
    """
    if start is None and end is None:
        return splits
    out = []
    for split in splits:
        day = _as_date(str(split.get("date") or "")[:10])
        if day is None:
            continue
        if (start is None or day >= start) and (end is None or day <= end):
            out.append(split)
    return out

def _within(transactions, start: date | None, end: date | None):
    if start is None and end is None:
        return list(transactions)
    return [
        t
        for t in transactions
        if (start is None or t.txn_date >= start) and (end is None or t.txn_date <= end)
    ]

def _as_amount(raw: str | None) -> "Decimal | None":
    """A money bound from the query string, or None. Decimal, never float."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        # A hand-edited URL should show the ledger, not an error page — the
        # same call `_as_date` makes for a malformed window.
        return None


def _account_summary(account, selected: str | None) -> dict:
    return {
        "slug": account.slug,
        "bank": account.bank,
        "account": account.masked,
        "assetAccount": account.asset_account,
        "label": account.display,
        "selected": account.slug == selected,
    }


@api.get("/accounts")
@A.login_required
def accounts():
    """The registry, masked. Drives the switcher — which the client hides
    entirely when this returns fewer than two accounts (§21.9)."""
    registry = load_accounts()
    _, selected = _account_scope()
    return jsonify(
        {
            "accounts": [_account_summary(a, selected) for a in registry],
            "selected": selected,
            # Stated by the server so the client never has to decide when the
            # feature exists. One account means one account, everywhere.
            "multiple": len(registry) > 1,
        }
    )


# --- overview -------------------------------------------------------------


@api.get("/overview")
@A.login_required
def overview():
    """Balance, sync age and recent statements, scoped to the selected account.

    **"All accounts" sums the balances and shows the parts** (§21.9). The sum is a
    true figure — it is what those accounts hold together — but unlike a
    single-account balance it cannot be reconciled against any one statement's
    closing figure, which is what this card has implied since Phase 7. So it is
    labelled as a sum and the per-account figures travel with it.
    """
    st = load_settings()
    scope, selected = _account_scope()
    error = None
    parts: list[dict] = []
    total: Decimal | None = None

    try:
        with FireflyClient(st.firefly_url, st.firefly_token or "") as client:
            live = {
                a["attributes"]["name"]: Decimal(str(a["attributes"]["current_balance"]))
                for a in client.asset_accounts()
            }
        for account in scope:
            amount = live.get(account.asset_account)
            parts.append(
                {
                    "slug": account.slug,
                    "label": account.display,
                    "account": account.masked,
                    "balance": _money(amount),
                }
            )
            if amount is not None:
                total = (total or Decimal(0)) + amount
    except FireflyError as exc:
        error = str(exc)

    return jsonify(
        {
            "balance": _money(total),
            "fireflyError": error,
            "account": scope[0].display if len(scope) == 1 else None,
            "selected": selected,
            # Only meaningful for "all"; the client shows the breakdown then.
            "parts": parts if len(parts) > 1 else [],
            # Staleness aggregates to the WORST, not the average: a warning must
            # not be diluted by a fresher account.
            "sync": _sync(service.sync_status()),
            "history": service.sync_history(current_app.config["ARCHIVE"]),
            "pending": bool(session.get("pending")),
        }
    )


def _slice(s) -> dict:
    return {"name": s.name, "amount": _money(s.amount), "count": s.count}


def _breakdown(b) -> dict:
    return {
        "name": b.name,
        "amount": _money(b.amount),
        "count": b.count,
        "parts": [_slice(p) for p in b.parts],
    }


@api.get("/analysis")
@A.login_required
def analysis():
    """The Ledger page's charts. SPEC §18.

    Separate from `/overview` on purpose: this reads every transaction on the
    account and parses the archive, so folding it in would make the balance and
    the sync age — the two things the operator opens the page for — wait behind
    it.

    **Two sources, each authoritative for what it carries.** Money and category
    come from Firefly, because the rules engine assigns the category at store
    time (D5) and re-deriving it here would be a second implementation. The
    clock comes from the statement, because `txn_time` is parsed out of the
    narration (§6.5) and never pushed — Firefly has no idea what time of day
    anything happened.
    """
    st = load_settings()
    scope, selected = _account_scope()
    start, end, window = _date_scope()
    if not st.firefly_token or not scope:
        return _fail(
            "FIREFLY_TOKEN is not set, or no account is registered.", "unconfigured", 503
        )

    # **Everything on this page is additive over transactions, so "all accounts"
    # combines** (§21.9): spend, income, the category breakdown, the roll-ups, the
    # month buckets and the Day Rail. Each is a sum over rows, and the exclusion
    # semantics (§8/§8.1) are per row, so combining cannot change what any figure
    # means. Time of day is a property of the person, not the account, which makes
    # the combined Day Rail the more useful of the two readings.
    #
    # The clock map is still built PER ACCOUNT and merged by external_id, never by
    # the bank's id: those collide between accounts (§21.1), and joining them
    # naively would attach one account's clock to the other's transaction.
    splits: list[dict] = []
    times: dict[str, object] = {}
    coverages = []
    # One line per account, never a sum. Summing balances across accounts needs
    # a last-known figure carried forward for every account on every date, and
    # before the earliest account starts that "total" is one account wearing the
    # word total. Separate lines say the same thing without the caveat (§57).
    balances: list[dict] = []
    try:
        with FireflyClient(st.firefly_url, st.firefly_token) as client:
            live = {a["attributes"]["name"]: a["id"] for a in client.asset_accounts()}
            archive = current_app.config["ARCHIVE"]
            for account in scope:
                account_id = live.get(account.asset_account)
                if account_id is None:
                    return _fail(
                        f"No asset account named {account.asset_account!r}.",
                        "unconfigured",
                        503,
                    )
                splits.extend(
                    split
                    for group in client.account_transactions(account_id)
                    for split in group["attributes"]["transactions"]
                )
                # §114.2. Out of the index. Everything below needs the account's
                # deduped rows and the period its statements cover, and both are
                # a query now rather than a re-read of the whole archive.
                mine = service.archived_transactions([account], archive)
                for txn in mine:
                    times[account.external_id(txn.txn_id)] = txn.txn_time
                    # Tolerated for a pre-migration ledger, where the pushed id is
                    # the bank's bare one.
                    times.setdefault(txn.txn_id, txn.txn_time)
                span = service.archived_coverage([account], archive)
                if span:
                    coverages.append(span)

                points, opening = service.balance_series(mine, start=start, end=end)
                if points or opening:
                    balances.append(
                        {
                            "slug": account.slug,
                            "label": account.display,
                            "points": [
                                {"day": p.day, "balance": _money(p.balance)} for p in points
                            ],
                            "opening": (
                                {"day": opening.day, "balance": _money(opening.balance)}
                                if opening
                                else None
                            ),
                        }
                    )
    except FireflyError as exc:
        return _fail(f"The ledger store did not answer: {exc}", "firefly", 502)

    total_rows = len(splits)
    splits = _splits_within(splits, start, end)

    coverage = (
        (min(c[0] for c in coverages), max(c[1] for c in coverages)) if coverages else None
    )
    # Clipped to the window as well, or every month in a narrowed view is
    # reported as partial — the coverage is what decides that flag (§18), and
    # an uncut coverage says the ledger spans months the chart is not showing.
    if coverage and (start or end):
        coverage = (max(coverage[0], start or coverage[0]), min(coverage[1], end or coverage[1]))
        if coverage[0] > coverage[1]:
            coverage = None

    # §73. Read per request rather than cached: it is a small YAML file and a
    # stale attribution would move money between months invisibly.
    result = service.ledger_analysis(
        splits, times=times, coverage=coverage, attribution=load_attribution()
    )

    return jsonify(
        {
            "spend": _money(result.spend),
            "grossSpend": _money(result.gross_spend),
            "income": _money(result.income),
            "grossIncome": _money(result.gross_income),
            "net": _money(result.net),
            "withdrawals": result.withdrawals,
            "deposits": result.deposits,
            "categories": [_slice(s) for s in result.categories],
            "payees": [_slice(s) for s in result.payees],
            "sources": [_slice(s) for s in result.sources],
            # §64. Firefly's Category, Double and Tag reports — three screens
            # there, one shape here, and all three carry §8/§8.1 because they
            # are computed inside `ledger_analysis` rather than beside it.
            "payeesByCategory": [_breakdown(b) for b in result.payees_by_category],
            "categoriesByPayee": [_breakdown(b) for b in result.categories_by_payee],
            "categoriesByTag": [_breakdown(b) for b in result.categories_by_tag],
            "sourcesByCategory": [_breakdown(b) for b in result.sources_by_category],
            "spread": [
                {
                    "name": x.name, "count": x.count, "low": _money(x.low),
                    "q1": _money(x.q1), "median": _money(x.median),
                    "q3": _money(x.q3), "high": _money(x.high),
                }
                for x in result.spread
            ],
            "categoriesBySource": [_breakdown(b) for b in result.categories_by_source],
            "excludedSpend": [_slice(s) for s in result.excluded_spend],
            # Sent as a total rather than left to the client to subtract: money
            # crosses this boundary as a decimal string and must not be put
            # through a float on the way to being displayed (§16.1).
            "excludedSpendTotal": _money(
                sum((s.amount for s in result.excluded_spend), Decimal(0))
            ),
            "excludedIncome": _slice(result.excluded_income),
            "refunds": _slice(result.refunds),
            "rollups": [
                {
                    "tag": r.tag,
                    "amount": _money(r.amount),
                    "count": r.count,
                    "parts": [_slice(p) for p in r.parts],
                }
                for r in result.rollups
            ],
            "months": [
                {
                    "month": m.month,
                    "spend": _money(m.spend),
                    "income": _money(m.income),
                    "partial": m.partial,
                }
                for m in result.months
            ],
            # Aligned with `months` by position and zero-padded, so the client
            # never has to guess which month an amount belongs to (§57).
            "categoryMonths": [
                {"name": c.name, "amounts": [_money(a) for a in c.amounts], "total": _money(c.total)}
                for c in result.category_months
            ],
            "balances": balances,
            "hours": result.hours,
            "weekdays": result.weekdays,
            "weekdaySpend": [_money(v) for v in result.weekday_spend],
            "clocked": result.clocked,
            "counted": result.counted,
            "uncategorised": _slice(result.uncategorised),
            "notSpend": result.not_spend,
            "window": window,
            "outsideWindow": total_rows - len(splits),
            "selected": selected,
            "accounts": [a.slug for a in scope],
            "coverage": (
                {"from": coverage[0].isoformat(), "to": coverage[1].isoformat()}
                if coverage
                else None
            ),
        }
    )


# --- upload / preview / confirm -------------------------------------------


@api.post("/statement")
@A.login_required
def upload_statement():
    """Validate before saving. A file that fails any check is deleted, never
    left in inbox/ where a later `make sync` would pick it up."""
    uploaded = request.files.get("statement")
    if uploaded is None or not uploaded.filename:
        return _fail("No file chosen.")

    inbox: Path = current_app.config["INBOX"]
    inbox.mkdir(parents=True, exist_ok=True)
    name = secure_filename(uploaded.filename) or "statement.xls"
    destination = inbox / name
    staging = inbox / f".incoming-{name}"
    uploaded.save(staging)

    try:
        size = staging.stat().st_size
        if size == 0:
            raise UnsupportedFormat("the uploaded file is empty")
        if size > MAX_UPLOAD_BYTES:
            raise UnsupportedFormat(f"file is {size} bytes; limit is {MAX_UPLOAD_BYTES}")

        kind = sniff(staging)
        if kind not in ACCEPTED_SNIFF:
            raise UnsupportedFormat(
                f"magic bytes say {kind!r}. The Canara export is a genuine OLE2 "
                ".xls, and the PDF statement is also accepted (SPEC §6.8)."
            )

        parsed = service.parse_statement(staging)
        # §6.7 became §21.2: which of my accounts is this? An unregistered
        # account raises UnknownAccount (an AccountMismatch), which the handler
        # below turns into a 422 AND deletes the staged file — so it can never
        # be picked up by a later `make sync`.
        settings = load_settings()
        client = (
            FireflyClient(settings.firefly_url, settings.firefly_token)
            if settings.firefly_token
            else None
        )
        try:
            account = service.resolve_account(parsed.meta, settings, client=client)
        finally:
            if client is not None:
                client.close()
    except (ParseError, UnsupportedFormat) as exc:
        staging.unlink(missing_ok=True)
        return _fail(f"Rejected: {exc}", "rejected", 422)
    except (BalanceBreak, IntegrityError) as exc:
        staging.unlink(missing_ok=True)
        return _fail(f"Validation failed, nothing saved: {exc}", "invalid", 422)
    except AccountMismatch as exc:
        staging.unlink(missing_ok=True)
        # Already masked to last 4 — §11 holds in error paths too.
        payload = {"error": f"Refused: {exc}", "code": "account_mismatch"}
        if isinstance(exc, UnknownAccount):
            payload["code"] = "unknown_account"
            payload["account"] = exc.masked
            payload["known"] = exc.known
        return jsonify(payload), 422

    staging.rename(destination)
    session["pending"] = str(destination)

    parsed = service.parse_statement(destination)
    payload = _parsed(parsed, filename=name)
    payload["unknown"] = service.unknown_tokens(parsed.transactions)
    # Routed by what the STATEMENT says, never by what the switcher is showing
    # (§21.9): a statement belongs to one account as a matter of fact. Saying
    # which one is what stops that being invisible.
    payload["routed"] = {
        "slug": account.slug,
        "label": account.display,
        "account": account.masked,
        "registered": len(load_accounts()) > 1,
    }
    return jsonify(payload)


@api.get("/statement/pending")
@A.login_required
def pending_statement():
    pending = session.get("pending")
    if not pending or not Path(pending).exists():
        return _fail("Nothing pending — upload a statement first.", "no_pending", 404)
    parsed = service.parse_statement(Path(pending))
    payload = _parsed(parsed)
    payload["unknown"] = service.unknown_tokens(parsed.transactions)
    return jsonify(payload)


@api.delete("/statement/pending")
@A.login_required
def discard_pending():
    pending = session.pop("pending", None)
    if pending:
        Path(pending).unlink(missing_ok=True)
    return jsonify({"ok": True})


@api.post("/statement/confirm")
@A.login_required
def confirm_statement():
    pending = session.get("pending")
    if not pending or not Path(pending).exists():
        return _fail("Nothing pending — upload a statement first.", "no_pending", 404)

    st = load_settings()
    if not st.firefly_token or not st.passbook_asset_account:
        return _fail("FIREFLY_TOKEN or PASSBOOK_ASSET_ACCOUNT is not set.", "unconfigured", 503)

    parsed = service.parse_statement(Path(pending))
    try:
        account = service.resolve_account(parsed.meta, st, allow_register=False)
    except AccountMismatch as exc:
        return _fail(f"Refused: {exc}", "account_mismatch", 422)

    try:
        result = service.push_statement(parsed, st, account=account)
    except FireflyError as exc:
        return _fail(f"Push failed: {exc}", "firefly", 502)

    archived = None
    if result.ok:
        archived = str(
            service.archive_statement(parsed, current_app.config["ARCHIVE"], account)
        )
        session.pop("pending", None)

    return jsonify(
        {
            "parsed": len(parsed.transactions),
            "pushed": result.pushed,
            "duplicates": result.duplicates,
            "failed": result.failed,
            "failures": [{"id": i, "message": m} for i, m in result.failures[:10]],
            "archived": archived,
        }
    )


# --- payees ---------------------------------------------------------------


def _all_transactions(scope=None):
    """Every archived statement plus anything pending, for the accounts in scope.

    **Deduped per account, then concatenated** (§21.1). Deduping across accounts
    on the bank's transaction id is the silent data loss this phase exists to
    prevent: two accounts, 186 rows, 93 survive, no error. `account_transactions`
    narrows first and dedupes inside.
    """
    archive: Path = current_app.config["ARCHIVE"]
    statements = service.archived_statements(archive)
    pending = session.get("pending")
    if pending and Path(pending).exists():
        try:
            statements.append(service.parse_statement(Path(pending)))
        except Exception as exc:  # a bad staged file must not blank the page
            log.warning("skipping pending %s: %s", Path(pending).name, exc)

    accounts = scope if scope is not None else load_accounts()
    if not accounts:
        # Pre-registry: one unnamed ledger, deduped as it always was.
        seen: dict[str, object] = {}
        for statement in statements:
            for txn in statement.transactions:
                seen.setdefault(txn.txn_id, txn)
        return list(seen.values())

    out: list[object] = []
    for account in accounts:
        mine = service.statements_for(account, statements)
        seen = {}
        for statement in mine:
            for txn in statement.transactions:
                seen.setdefault(txn.txn_id, txn)
        out.extend(seen.values())
    return out


@api.get("/payees")
@A.login_required
def payees():
    scope, selected = _account_scope()
    transactions = _all_transactions(scope)
    rows = service.payee_inventory(transactions)

    # Hour-of-day per row, for the Day Rail at aggregate scale. This is the
    # analysis that split Morning Stall from Late Counter by hand in Phase 4;
    # it belongs in the page rather than in a one-off script.
    #
    # Keyed on (token, channel) to match how `payee_inventory` groups rows. On
    # token alone, a token appearing under two channels would hand both rows
    # the same combined histogram while their counts differed — a chart
    # disagreeing with the number beside it. No token spans channels in the
    # current data, which is exactly why this would have gone unnoticed.
    hours: dict[tuple[str, str], list[int]] = {}
    clocked: dict[tuple[str, str], int] = {}
    for txn in transactions:
        key = (txn.payee or "(unparsed)", txn.channel)
        bucket = hours.setdefault(key, [0] * 24)
        clocked.setdefault(key, 0)
        if txn.txn_time:
            bucket[txn.txn_time.hour] += 1
            clocked[key] += 1

    return jsonify(
        {
            "rows": [
                {
                    **_payee_row(r),
                    "hours": hours.get((r.token, r.channel), [0] * 24),
                    # Stated separately because it is NOT r.count: NEFT, CHG,
                    # SCHEME and INT narrations carry no clock. A histogram
                    # labelled with the wrong denominator misinforms exactly
                    # the person who cannot see the chart.
                    "clocked": clocked.get((r.token, r.channel), 0),
                }
                for r in rows
            ],
            "categories": known_categories(),
            "selected": selected,
            "total": len(transactions),
            "totalClocked": sum(clocked.values()),
        }
    )


@api.get("/categories")
@A.login_required
def categories():
    """Only categories that already have a rule. D10: the UI never invents one."""
    return jsonify({"categories": known_categories()})


def _split_submission(body: dict) -> tuple[dict, dict]:
    aliases = {str(k): str(v) for k, v in (body.get("aliases") or {}).items()}
    categories_in = {str(k): str(v) for k, v in (body.get("categories") or {}).items()}
    return aliases, categories_in


@api.post("/payees/diff")
@A.login_required
def payees_diff():
    """Exactly what would change. Writes nothing."""
    aliases_in, categories_in = _split_submission(request.get_json(silent=True) or {})

    current = load_payee_aliases()
    alias_changes = {
        t: v for t, v in aliases_in.items() if (current.get(t) or "") != v.strip()
    }
    merged = dict(current)
    merged.update({t: v.strip() for t, v in alias_changes.items() if v.strip()})

    existing = service.rule_categories()
    category_changes = {
        t: v
        for t, v in categories_in.items()
        if existing.get((merged.get(t) or t)) != v and (v or existing.get(merged.get(t) or t))
    }

    try:
        changes = [plan_aliases(alias_changes), plan_categories(category_changes, merged)]
    except KeyError as exc:
        # D10 in force: an unknown category is refused with the known list,
        # never created on the operator's behalf.
        return _fail(str(exc), "unknown_category", 422)

    return jsonify(
        {
            "changes": [
                {"path": str(c.path), "diff": c.diff()} for c in changes if c.changed
            ],
            "aliasChanges": alias_changes,
            "categoryChanges": category_changes,
        }
    )


@api.post("/payees/apply")
@A.login_required
def payees_apply():
    aliases_in, categories_in = _split_submission(request.get_json(silent=True) or {})
    # Both read the map as it stands NOW — before `plan_aliases` rewrites it.
    current = load_payee_aliases()
    merged = _merged_aliases(current, aliases_in)
    renames = _display_renames(current, aliases_in)

    try:
        plan_aliases(aliases_in).apply()
        # Renames first, in the same plan: a rule matches the display name, so
        # relabelling a payee without following it through `rules.yaml` silently
        # de-categorises the payee (§24.4).
        plan_categories(categories_in, merged, renames=renames).apply()
    except KeyError as exc:
        return _fail(str(exc), "unknown_category", 422)

    st = load_settings()
    summary = "Config written."
    synced: dict | None = None
    if st.firefly_token:
        try:
            with FireflyClient(st.firefly_url, st.firefly_token) as client:
                res = bootstrap_rules(client, load_rules(), st.large_txn_threshold)
                summary = (
                    f"Config written. Rules: {len(res.created)} created, "
                    f"{len(res.updated)} updated, {len(res.existing)} unchanged."
                )
                # The second half of editing a payee, in the same request.
                # Config alone reaches only FUTURE pushes; the rows already in
                # Firefly are what the operator is looking at, and leaving them
                # for a separate destructive step meant they were never moved
                # at all. Rules first, then the rows.
                if st.passbook_asset_account:
                    synced = _sync_now(client, st)
                    summary += _synced_summary(synced)
        except FireflyError as exc:
            summary = f"Config written, but bootstrap failed: {exc}"
    return jsonify({"ok": True, "summary": summary, "synced": synced})


# --- re-apply -------------------------------------------------------------


def _change(c) -> dict:
    return {
        "externalId": c.external_id,
        "groupId": c.group_id,
        "date": c.date,
        "amount": _money(c.amount),
        "kind": c.kind,
        "oldDescription": c.old_description,
        "newDescription": c.new_description,
        "oldCategory": c.old_category,
        "newCategory": c.new_category,
        "oldCounterparty": c.old_counterparty,
        "newCounterparty": c.new_counterparty,
        "oldTags": list(c.old_tags),
        "newTags": list(c.new_tags),
        "nameChanged": c.name_changed,
        "categoryChanged": c.category_changed,
        "counterpartyChanged": c.counterparty_changed,
        "tagsChanged": c.tags_changed,
    }


def _preview(changes: list, considered: int) -> dict:
    """One shape for "what does config say about the rows already in Firefly".

    Shared by `/reapply` and by `/payees/diff`, which asks the same question
    about a config that has not been written yet.
    """
    # Which tags this change would REMOVE from rows, and how many rows each.
    # SPEC §33: a tag is what the roll-ups are built on, and losing one is a
    # semantic loss that a diff of payee lists cannot show. Moving three food
    # categories into an untagged one silently dropped `food` from 29 rows and
    # under-reported food spend — the number stayed plausible.
    lost: dict[str, int] = {}
    for change in changes:
        for tag in set(change.old_tags) - set(change.new_tags):
            lost[tag] = lost.get(tag, 0) + 1

    return {
        "considered": considered,
        "renames": sum(1 for c in changes if c.name_changed),
        "recats": sum(1 for c in changes if c.category_changed),
        "counterparties": sum(1 for c in changes if c.counterparty_changed),
        "retags": sum(1 for c in changes if c.tags_changed),
        "tagsLost": [{"tag": t, "rows": n} for t, n in sorted(lost.items())],
        "dump": _dump_state(),
        "changes": [_change(c) for c in changes],
    }


def _dump_state() -> dict:
    """Whether a database dump recent enough to run a purge exists.

    The UI used to offer a button reading "Back up, then purge and re-push" —
    directly above a note explaining that this container cannot take a database
    dump, because that needs the Docker socket it deliberately does not have
    (§15.1). The button promised the one thing the page had just said it could
    not do, on the only destructive action in the app. What it actually backs up
    is `config/`.
    """
    dump = ops.newest_dump()
    return {
        "name": dump[0] if dump else None,
        "ageMinutes": dump[1] if dump else None,
        "maxAgeMinutes": ops.REAPPLY_DUMP_MAX_AGE_MINUTES,
        "fresh": bool(dump and dump[1] <= ops.REAPPLY_DUMP_MAX_AGE_MINUTES),
    }

def _sync_now(client, st) -> dict:
    """Compare, write, then **re-read**. SPEC §23.

    The re-read is separate and separately guarded. A count of requests that
    returned 200 is a claim about the request; `remaining` is a claim about the
    ledger, and only the second is worth a tick (non-negotiable 11). If the
    verification itself cannot be done, `remaining` is `None` — *unverified*,
    which is a third state and never rendered as a pass.
    """
    archive = current_app.config["ARCHIVE"]
    changes, considered = service.reapply_preview(client, st, archive)
    result = service.sync_ledger(client, changes)

    remaining: int | None
    try:
        after, _ = service.reapply_preview(client, st, archive)
        remaining = len(after)
    except FireflyError as exc:
        # Rows were written. Losing the report of that because the *check*
        # failed would be the worst of both — silent writes and a silent error.
        log.warning("could not re-read the ledger after syncing: %s", exc)
        remaining = None

    return {
        "considered": considered,
        "attempted": len(changes),
        "updated": result.updated,
        "failed": result.failed,
        "failures": [{"externalId": e, "message": m} for e, m in result.failures[:20]],
        "remaining": remaining,
    }

def _synced_summary(synced: dict) -> str:
    """One sentence about the ledger, phrased on what is true after re-reading it."""
    if synced["considered"] == 0:
        # Zero compared is not zero differing (§23.1). Never phrase it as a pass.
        return (
            " No rows in the ledger were compared — nothing in archive/ matched a row's"
            " external_id, so the ledger was not checked."
        )
    if synced["attempted"] == 0:
        return f" Ledger already matched on all {synced['considered']} row(s)."
    parts = [f" Ledger: {synced['updated']} of {synced['attempted']} row(s) updated in place."]
    if synced["failed"]:
        parts.append(f" {synced['failed']} failed.")
    if synced["remaining"] is None:
        parts.append(" Whether any still differ could not be checked — the ledger stopped answering.")
    elif synced["remaining"]:
        parts.append(
            f" {synced['remaining']} row(s) still differ — those need a re-push, "
            "not an update."
        )
    return "".join(parts)

def _merged_aliases(current: dict[str, str], submitted: dict[str, str]) -> dict[str, str]:
    """The alias map as it will read after the write. Clearing one **removes** it.

    The removal is the point. `plan_aliases` deletes an entry whose new value is
    blank, but the merged map used to be built with `if v.strip()`, so a cleared
    alias survived here and nowhere else. `plan_categories` resolves a token to
    its display name through this map, so clearing an alias and choosing a
    category in the same submission filed the category under the alias that had
    just been deleted — while the row would be pushed under its raw token. The
    rule then matched nothing, silently, and the payee looked categorised on the
    page that had just written it.
    """
    merged = dict(current)
    for token, value in submitted.items():
        alias = (value or "").strip()
        if alias:
            merged[token] = alias
        else:
            merged.pop(token, None)
    return merged

def _display_renames(current: dict[str, str], alias_changes: dict[str, str]) -> dict[str, str]:
    """old display name -> new display name, for every token whose alias moved.

    The display name is what `description` carries and therefore what every
    rule matches on, so a rename has to be followed through `rules.yaml` or the
    category is silently orphaned. See `plan_rule_renames`.

    Renames onto themselves and empty names are dropped: a rule payee of `""`
    would match every description in the ledger.
    """
    renames: dict[str, str] = {}
    for token, value in alias_changes.items():
        was = (current.get(token) or token).strip()
        now = ((value or "").strip() or token).strip()
        if was and now and was != now:
            renames[was] = now
    return renames



@api.get("/reapply")
@A.login_required
def reapply_preview():
    st = load_settings()
    if not st.firefly_token or not st.passbook_asset_account:
        return _fail("FIREFLY_TOKEN or PASSBOOK_ASSET_ACCOUNT is not set.", "unconfigured", 503)
    try:
        with FireflyClient(st.firefly_url, st.firefly_token) as client:
            changes, considered = service.reapply_preview(
                client, st, current_app.config["ARCHIVE"]
            )
    except FireflyError as exc:
        return _fail(f"Firefly did not answer: {exc}", "firefly", 502)

    return jsonify({**_preview(changes, considered), "dump": _dump_state()})


@api.post("/reapply/sync")
@A.login_required
def reapply_sync():
    """Write the current config onto the rows already in Firefly. SPEC §23.

    **This is what a payee edit was always supposed to do.** Renaming a payee
    used to write `config/` and sync the rules, and stop there: the ledger kept
    the names it was pushed with, and the only way to move them was
    `/reapply/run` — a purge and a full re-push, gated on a database dump the
    operator has to take on the host. A rename is not worth deleting a ledger
    for, so in practice it never happened.

    Three fields change on rows that already exist. Nothing is deleted, so
    there is no dump gate here: the operation is idempotent and config is the
    source of truth, which means a failed run is re-run rather than recovered.

    It cannot fix everything, and says so rather than implying otherwise. A row
    missing from Firefly, a wrong amount or a wrong date still need
    `/reapply/run`.
    """
    st = load_settings()
    if not st.firefly_token or not st.passbook_asset_account:
        return _fail("FIREFLY_TOKEN or PASSBOOK_ASSET_ACCOUNT is not set.", "unconfigured", 503)
    try:
        with FireflyClient(st.firefly_url, st.firefly_token) as client:
            synced = _sync_now(client, st)
    except FireflyError as exc:
        return _fail(f"Firefly did not answer: {exc}", "firefly", 502)

    log.info(
        "in-place sync: %d updated, %d failed, %s still differ",
        synced["updated"],
        synced["failed"],
        "unknown" if synced["remaining"] is None else synced["remaining"],
    )
    # `remaining is None` is unverified, not clean — it must not read as ok
    # (non-negotiable 11).
    return jsonify({"ok": synced["failed"] == 0 and synced["remaining"] == 0, **synced})


@api.post("/reapply/run")
@A.login_required
def reapply_run():
    """Back up, purge, sync rules, re-push, verify. SPEC §15.2.

    Order is load-bearing. The rules must reach Firefly *before* the re-push:
    they are applied at store time, so a rule the engine has not been told about
    cannot categorise anything. Skipping that step once produced six
    uncategorised rows while every other check still reported green.
    """
    st = load_settings()
    archive: Path = current_app.config["ARCHIVE"]
    steps: list[dict] = []

    # Enforced here, not only in the client. A disabled button is a courtesy; the
    # thing standing between a purge and an unrecoverable ledger has to be a
    # server-side refusal. Checked before anything is copied, deleted or pushed.
    state = _dump_state()
    if not state["fresh"]:
        log.warning("re-apply refused: newest dump is %s", state["ageMinutes"])
        return _fail(
            (
                "No database dump from the last "
                f"{state['maxAgeMinutes']} minutes. This deletes every row on the "
                "account and pushes them again, and the dump is the only way back. "
                "Run `make backup` on the host, then reload."
                + (
                    ""
                    if state["ageMinutes"] is None
                    else f" The newest is {state['name']}, {state['ageMinutes']} minutes old."
                )
            ),
            "stale_backup",
            409,
        )

    try:
        dump = _run_config_backup()
        steps.append({"state": "ok", "message": f"config backed up — {dump}"})
    except Exception as exc:
        return _fail(f"Backup failed, nothing was deleted: {exc}", "backup", 500)

    statements = sorted(
        p for p in archive.rglob("*") if p.is_file() and not p.name.startswith(".")
    )
    if not statements:
        return _fail("Nothing in archive/ to re-push.", "empty_archive", 409)

    with FireflyClient(st.firefly_url, st.firefly_token or "") as client:
        accounts = {a["attributes"]["name"]: a["id"] for a in client.asset_accounts()}
        account_id = accounts.get(st.passbook_asset_account)
        if account_id is None:
            return _fail(
                f"No asset account named {st.passbook_asset_account!r}.", "unconfigured", 503
            )

        candidates, protected = find_candidates(client, account_id)
        # Intent BEFORE the first delete (§19.7). If this request dies here — the
        # container restarts, the machine sleeps — the file is what makes the
        # half-finished state visible instead of merely coherent.
        result = purge_transactions(
            client,
            candidates,
            account=st.passbook_asset_account or "",
            statements=[str(p) for p in statements],
        )
        if not result.ok:
            return _fail(
                f"Purge failed ({result.failed} errors); nothing re-pushed. "
                f"Recorded as {result.intent.name if result.intent else 'no intent'} — "
                "run `passbook purge --resume` on the host.",
                "purge",
                500,
            )
        steps.append(
            {
                "state": "ok",
                "message": (
                    f"purged {result.deleted} row(s), {len(protected)} protected "
                    "(no external_id), trashed records force-deleted"
                ),
            }
        )

        boot = bootstrap_rules(client, load_rules(), st.large_txn_threshold)
        steps.append(
            {
                "state": "ok" if boot.ok else "bad",
                "message": (
                    f"rules synced — {len(boot.created)} created, "
                    f"{len(boot.updated)} updated, {len(boot.existing)} unchanged"
                ),
            }
        )

        if result.intent:
            ops.update_purge_intent(result.intent, stage="repushing")
        pushed = duplicates = failed = 0
        for path in statements:
            parsed = service.parse_statement(path)
            service.account_matches(parsed.meta, st)
            res = service.push_statement(parsed, st, client)
            pushed += res.pushed
            duplicates += res.duplicates
            failed += res.failed
        steps.append(
            {
                "state": "ok" if not failed else "bad",
                "message": f"re-pushed {pushed}, {duplicates} duplicate(s), {failed} failed",
            }
        )

        balance = service.ledger_balance(st, client)

    expected = None
    try:
        newest = max(statements, key=lambda p: p.stat().st_mtime)
        expected = service.parse_statement(newest).meta.closing_balance
    except Exception:
        pass

    reconciles = expected is not None and balance is not None and balance == expected
    steps.append(
        {
            "state": "ok" if reconciles else "bad",
            "message": (
                f"balance {balance} vs statement closing {expected} — "
                + ("reconciles" if reconciles else "DOES NOT RECONCILE")
            ),
        }
    )

    # The intent is cleared only once the ledger itself verifies — not when the
    # last HTTP call returns. §19.7, and §20 is what does the verifying.
    verdict = _ledger_verdict(st)
    steps.append(
        {
            "state": "ok" if verdict["ok"] else "bad",
            "message": f"ledger integrity — {verdict['headline']}",
        }
    )
    if result.intent:
        if verdict["ok"] and reconciles and not failed:
            ops.clear_purge_intent(result.intent)
        else:
            steps.append(
                {
                    "state": "bad",
                    "message": (
                        f"{result.intent.name} kept: the cycle is unfinished, and the "
                        "record is what makes that visible. `passbook purge --resume`."
                    ),
                }
            )

    return jsonify(
        {
            "steps": steps,
            "balance": _money(balance),
            "expected": _money(expected),
            "reconciles": reconciles,
            "ledger": verdict,
        }
    )


def _run_config_backup() -> str:
    """Copy the config this container can actually reach.

    The database dump needs the Docker socket, which this container
    deliberately does not have (§15.3), so `make backup` stays a host action.
    """
    import tarfile

    backups = Path("backups")
    backups.mkdir(parents=True, exist_ok=True)
    target = backups / f"config-prereapply-{date.today():%Y-%m-%d}.tar.gz"
    with tarfile.open(target, "w:gz") as tar:
        for item in sorted(Path("config").glob("*")):
            tar.add(item, arcname=f"config/{item.name}")
    target.chmod(0o600)
    return str(target)


# --- status ---------------------------------------------------------------


def _ledger_verdict(st, scope=None) -> dict:
    """The §20 integrity check, for the Ledger strip.

    `trashed` is deliberately **not** supplied: Firefly's API cannot list
    soft-deleted journals (verified against the pinned tag) and this container has
    no database credentials by design (§15.1). The check therefore reports itself
    unchecked, and the strip must not paint that green — "cannot see" and "fine"
    are different, which is the whole lesson of §19.
    """
    accounts = scope if scope is not None else load_accounts()
    if not st.firefly_token or not accounts:
        return {"ok": None, "headline": "not configured", "checks": []}
    checks: list[service.Check] = []
    try:
        with FireflyClient(st.firefly_url, st.firefly_token) as client:
            intents = [p.name for p in ops.outstanding_purge_intents()]
            for account in accounts:
                # Per account (§21.6). One account's rows are missing from the
                # other by definition, so a single combined verdict would be
                # noise; the worst result across accounts is what the strip shows.
                verdict = service.verify_ledger(
                    client,
                    account,
                    current_app.config["ARCHIVE"],
                    trashed=None,
                    intents=intents,
                )
                prefix = f"{account.slug}: " if len(accounts) > 1 else ""
                checks.extend(
                    service.Check(f"{prefix}{c.name}", c.ok, c.detail) for c in verdict.checks
                )
    except FireflyError as exc:
        return {"ok": None, "headline": f"could not check: {exc}", "checks": []}
    combined = service.LedgerVerdict(checks)
    return {
        "ok": combined.ok,
        "headline": combined.headline,
        "failed": len(combined.failed),
        "unchecked": len(combined.unchecked),
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in combined.checks],
    }


@api.get("/status")
@A.login_required
def status():
    st = load_settings()
    scope, selected = _account_scope()
    expiry = token_expiry(st.firefly_token or "") if st.firefly_token else None
    days_left = (expiry - datetime.now(timezone.utc)).days if expiry else None

    about = None
    firefly_error = None
    try:
        with FireflyClient(st.firefly_url, st.firefly_token or "") as client:
            about = client.about()
    except FireflyError as exc:
        firefly_error = str(exc)

    remote, remote_error = ops.remote_backups(os.environ.get("PASSBOOK_RCLONE_REMOTE"))
    auth = A.current_auth()

    return jsonify(
        {
            "sync": _sync(service.sync_status()),
            "token": {
                # Shape only. The token itself never crosses this boundary.
                "shapeOk": bool(st.firefly_token and st.firefly_token.count(".") == 2),
                "expiry": expiry.date().isoformat() if expiry else None,
                "daysLeft": days_left,
            },
            "firefly": {"about": about, "error": firefly_error},
            "account": {
                "assetAccount": scope[0].asset_account if len(scope) == 1 else None,
                "assertionConfigured": bool(load_accounts()),
                "selected": selected,
                "count": len(load_accounts()),
            },
            "drift": alias_drift(),
            "ledger": _ledger_verdict(st, scope),
            "backups": {
                "local": [_artefact(a) for a in ops.local_backups()],
                "ageDays": ops.backup_age(),
                "staleDays": ops.BACKUP_STALE_DAYS,
                "remote": [_artefact(a) for a in remote],
                "remoteError": remote_error,
            },
            "auth": A.totp_status(auth),
        }
    )


# -- adding a bank, from the browser -----------------------------------------
# SPEC §27, §34, §49. A bank is a description of where its columns are, written
# from this page and saved to `config/banks/<slug>.yaml`. No Python, and no
# statement leaves the machine: every route below reads the uploaded file in a
# temporary directory and deletes it.

@api.get("/banks")
@A.login_required
def bank_list():
    """Which banks passbook can already read. SPEC §23.2.

    A GET, because the Add-a-bank page asks this on load: most people who land
    there do not need the page at all — a profile for their bank ships — and
    the opening line saying so is the difference between a ten-minute task and
    none.

    Resolves through `supported_banks()` rather than a constant, so a profile
    dropped into `config/banks/` shows up without a restart.
    """
    return jsonify({"banks": list(SUPPORTED_BANKS), "builtin": list(BUILTIN_BANKS)})


@api.post("/banks/try")
@A.login_required
def bank_try():
    """Parse the statement with a profile that has not been saved. SPEC §49.

    **Writes nothing.** No profile, no registry entry, no staged file, no push.

    It exists because the loop without it was: save the profile, go to another
    page, upload again, read a one-line rejection, come back, guess, repeat.
    Three rounds of that produced `row 10: transaction has no balance` — true,
    unactionable, and impossible for me to diagnose without reading a file the
    operator has told me never to open.

    So the diagnosis goes to the person who is allowed to see the data. On
    failure this returns **the banded rows themselves**: the operator looks at
    their own statement, in their own browser, and sees which column their
    balance actually landed in. That is one glance instead of a guessing game,
    and nothing leaves the machine that was not already on it.
    """
    upload = request.files.get("statement")
    if upload is None or not upload.filename:
        return _fail("No file chosen.")

    def _json(name: str) -> dict:
        try:
            loaded = json.loads(request.form.get(name) or "{}")
            return loaded if isinstance(loaded, dict) else {}
        except ValueError:
            return {}

    columns = {str(k).strip(): str(v).strip() for k, v in _json("columns").items() if str(k).strip()}
    metadata = {str(k).strip(): str(v).strip() for k, v in _json("metadata").items() if str(k).strip()}
    derive = (request.form.get("deriveTxnId") or "").lower() in {"1", "true", "yes"}
    try:
        dates = [str(d).strip() for d in json.loads(request.form.get("dates") or "[]") if str(d).strip()]
    except ValueError:
        dates = []
    if not columns:
        return _fail("Name the columns first.", "invalid", 422)

    scratch = Path(tempfile.mkdtemp(prefix="try-"))
    path = scratch / (secure_filename(upload.filename) or "statement")
    try:
        upload.save(path)
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            return _fail("That file is far too large to be a statement.", "too_large", 413)
        password = (request.form.get("password") or "").strip() or None
        try:
            return jsonify(_try_profile(path, password, columns, metadata, derive, dates))
        except PdfPasswordWrong as exc:
            return jsonify({"error": str(exc), "code": "pdf_password_wrong"}), 422
        except PdfPasswordRequired as exc:
            return jsonify({"error": str(exc), "code": "pdf_password"}), 422
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _try_profile(path, password, columns, metadata, derive, dates) -> dict:
    """Band the file with this mapping and say what happened. SPEC §49."""
    from ...loaders import _table, sniff
    from ...loaders._table import from_rows, norm
    from ...loaders.pdf import _decrypt, _lines
    from ...loaders.pdf_table import _learn_columns, _split, describe, find_header, to_grid
    from ...loaders.pdf_table import shape as pdf_shape
    from ...validate import BalanceBreak, check_continuity

    wanted = {norm(k): v for k, v in columns.items()}
    order = [f for f in ("date", "txn_id", "narration", "debit", "credit", "balance")
             if f in set(wanted.values()) or f == "narration"]
    if derive and "txn_id" in order:
        order.remove("txn_id")

    out: dict = {"container": sniff(path), "order": order}

    if out["container"] == "pdf":
        import pdfplumber

        with pdfplumber.open(_decrypt(path, password)) as document:
            lines = [line for page in document.pages for line in _lines(page)]
        header = find_header(lines, wanted)
        if header is None:
            out["error"] = (
                "None of those column names were found together on one line. Check "
                "the spelling against the table above — the words have to match, "
                "though case, spaces and punctuation do not."
            )
            return out
        at, cols, _headings = header
        split = _split(cols)
        text_edges, money_edges = _learn_columns(lines[at + 1 :], cols, split)
        out["headerLine"] = at
        # SPEC §50. The page as geometry and shapes — shareable, and useless to
        # anyone. `09-05-2026` becomes `92-92-94`; a payee becomes `A5`. Enough
        # to debug every banding failure this module has had, and it carries no
        # name, no payee, no account number and no amount.
        out["shapes"] = [
            f"{entry['line']:>3}: "
            + "  ".join(f"{w['shape']}@{w['x0']}-{w['x1']}" for w in entry["words"])
            for entry in describe(lines, SHAPE_LINES)
        ]
        # `to_grid` writes one grid row per preamble line and then the header,
        # so the header sits at the same index and the data starts after it.
        data_from = at + 1
        # Where each column was found, so a misread is visible as a number
        # rather than as a wrong total. Coordinates are not data.
        out["columns"] = {f: [round(x0, 1), round(x1, 1)] for f, (x0, x1) in cols.items()}
        # Both sides: where each column's values really start, and where its
        # figures really end. §51 — a heading is not its column.
        out["edges"] = {
            f: round(x, 1) for f, x in {**text_edges, **money_edges}.items()
        }
        grid = to_grid(lines, wanted, order)
    else:
        from ...loaders import read_grid

        grid = read_grid(path, out["container"], password)
        try:
            data_from = _table._find_header(grid or [], derive)[0] + 1
        except Exception:
            data_from = 1

    if not grid:
        out["error"] = "Nothing could be read out of that file."
        return out

    # The banded rows, for the operator's own eyes. This is their statement on
    # their screen; the point is that they can see which column their balance
    # landed in.
    out["banded"] = [[str(cell) for cell in row] for row in grid[:BANDED_PREVIEW]]
    out["bandedFrom"] = 0

    # The date column's own values, so a format can be offered rather than
    # demanded. Taken from the grid this mapping produced, which is the only
    # place they are known to be dates.
    if "date" in order:
        column = order.index("date")
        # **Data rows only.** Sampling from the preamble too fed account numbers
        # and phone numbers to the format detector, which requires every sample
        # to parse and therefore returned nothing at all.
        samples = [
            row[column]
            for row in grid[data_from:]
            if len(row) > column and row[column].strip()
        ][:40]
        out["dateCandidates"] = _table.date_formats_that_parse(samples)
        out["dateSample"] = samples[0] if samples else ""

    labels = _table.META_LABELS
    try:
        _table.META_LABELS = {**labels, **{norm(k): v for k, v in metadata.items()}}
        # The proposed formats, not the registered ones: this profile has
        # deliberately not been saved (§49).
        _table.DATE_FORMATS_OVERRIDE = dates or out.get("dateCandidates") or []
        # And the columns under test. `from_rows` re-derives the mapping from
        # the header text in the grid, and those words are only aliases once
        # the profile is saved — which is exactly what Try-it has not done.
        _table.COL_ALIASES_OVERRIDE = wanted
        meta, txns = from_rows(grid, derive_txn_id=derive)
    except Exception as exc:
        out["error"] = str(exc)
        # `row 11: transaction has no balance` names a grid row, and the banded
        # table right below it is that same grid. Marking the row turns "find
        # the one it named" into "look at the highlighted line", which is the
        # difference between a diagnosis and a word search. §49.4.
        named = re.search(r"\brow (\d+)\b", str(exc))
        if named:
            row = int(named.group(1))
            out["failedRow"] = row
            # The failing row as SHAPES, one safe line to paste. §52.2.
            #
            # The banded table above it shows real content, which is right for
            # the operator and useless for asking anyone else — so the row that
            # actually failed is also rendered the §50 way. Asking "which column
            # shows a dash" and getting no answer twice is a sign the question
            # was the wrong shape, not that nobody was listening.
            if 0 <= row < len(grid):
                out["failedRowShapes"] = "  ".join(
                    f"{field}={pdf_shape(str(cell)) or '—'}"
                    for field, cell in zip(order, grid[row])
                )
            if row >= out.get("bandedFrom", 0) + BANDED_PREVIEW:
                # The row it named is past the window. Move the window rather
                # than showing fourteen rows that all parsed.
                start = max(0, row - BANDED_PREVIEW // 2)
                out["banded"] = [
                    [str(cell) for cell in r] for r in grid[start : start + BANDED_PREVIEW]
                ]
                out["bandedFrom"] = start
        return out
    finally:
        _table.META_LABELS = labels
        _table.DATE_FORMATS_OVERRIDE = None
        _table.COL_ALIASES_OVERRIDE = None

    out["rows"] = len(txns)
    out["account"] = mask_account(meta.account_number)
    out["period"] = [meta.period_from.isoformat(), meta.period_to.isoformat()]
    out["opening"] = _money(meta.opening_balance)
    out["closing"] = _money(meta.closing_balance)
    try:
        check_continuity(meta, txns)
        out["ok"] = True
    except BalanceBreak as exc:
        # Parsed but does not add up: a column is mapped wrongly. This is the
        # check doing its job, so it is reported as the finding it is.
        out["ok"] = False
        out["error"] = str(exc)
    return out


@api.post("/banks")
@A.login_required
def bank_profile_save():
    """Write `config/banks/<bank>.yaml` from the browser. SPEC §34.

    A profile is a description of a layout, not a program, which is the whole
    reason adding a bank does not need Python. Writing one from a terminal did
    still need a terminal, and that was the last step keeping a non-developer
    out of their own second bank.

    Validated by **loading it back**: a profile that cannot be parsed is worse
    than none, because the parser raises on it rather than falling back — which
    is correct (§27.2) and would leave every bank unreadable until someone
    found the file.
    """
    body = request.get_json(silent=True) or {}
    bank = re.sub(r"[^a-z0-9-]", "", str(body.get("bank") or "").strip().lower())
    if not bank:
        return _fail("Give the bank a short name — lowercase letters and digits.", "invalid", 422)
    if bank in BUILTIN_BANKS:
        return _fail(f"{bank!r} is built in; it needs no profile.", "invalid", 422)

    columns = {str(k).strip(): str(v).strip() for k, v in (body.get("columns") or {}).items() if str(k).strip()}
    unknown = sorted(set(columns.values()) - set(profiles.FIELDS))
    if unknown:
        # Named properly, and it says which side is which. This message used to
        # read `unknown field(s): ['Balance', 'Chq', 'Date', ...]` — the
        # operator's own column headings listed back at them, because the page
        # posted the map inverted (§41.2). If it ever happens again the message
        # should at least say what it was expecting.
        return _fail(
            f"{'This is not a field' if len(unknown) == 1 else 'These are not fields'}"
            f" passbook knows: {', '.join(unknown)}."
            f" The fields are {', '.join(profiles.FIELDS)}.",
            "invalid",
            422,
        )
    # SPEC §44. `txn_id` is the one field a bank may genuinely not print —
    # Union Bank's cheque-number column is blank for every UPI and NEFT row.
    # Opt-in per profile and never inferred: an id synthesised because somebody
    # forgot to map a column would silently change how every row is identified,
    # forever.
    derive = bool(body.get("deriveTxnId"))
    required = CORE_COLS if derive else REQUIRED_COLS
    missing = required - set(columns.values())
    if missing:
        return _fail(
            f"still missing: {', '.join(sorted(missing))}. Every one is needed — "
            "without them the balance chain cannot be checked.",
            "invalid",
            422,
        )
    if derive and "txn_id" in set(columns.values()):
        return _fail(
            "You mapped a reference column and also ticked \u201cno reference "
            "number\u201d. Pick one — a real reference is always better.",
            "invalid",
            422,
        )

    metadata = {str(k).strip(): str(v).strip() for k, v in (body.get("metadata") or {}).items() if str(k).strip()}
    dates = [str(d).strip() for d in (body.get("dates") or []) if str(d).strip()]
    if dates:
        log.info("bank profile %s declares date format(s) %s", bank, dates)

    directory = profiles.PROFILES_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{bank}.yaml"
    document: dict = {"bank": bank, "columns": columns}
    if derive:
        document["derive_txn_id"] = True
    if metadata:
        document["metadata"] = metadata
    if dates:
        document["dates"] = dates

    header = (
        f"# {bank} statement layout. SPEC §27.\n"
        "# Written by the Add account page. Header matching ignores case, spaces\n"
        "# and punctuation, so only the words have to be right.\n"
    )
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(header + yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    tmp.replace(path)

    try:
        loaded = profiles.load_profiles()
    except profiles.ProfileError as exc:
        path.unlink(missing_ok=True)
        return _fail(f"That profile does not load: {exc}", "invalid", 422)

    log.info("bank profile written: %s (%d columns)", bank, len(columns))
    return jsonify(
        {"ok": True, "bank": bank, "path": str(path), "banks": [p["bank"] for p in loaded]}
    )


def _with_try_hint(exc) -> str:
    """A parse failure, plus where to go and see why. SPEC §49.3.

    `row 10: transaction has no balance` is true and unactionable, and this page
    cannot do better — it has one line and no room for evidence. The Add-a-bank
    page has the evidence: it prints the banded rows so the operator can see
    which column their balance actually landed in.

    An operator who hit this three times in a row was on the wrong page each
    time, and nothing on the page said there was a right one. Only added when a
    profile exists, because with no profile the remedy is to add the bank rather
    than to re-check one.
    """
    message = str(exc)
    try:
        from ...loaders.profiles import load_profiles

        if not load_profiles():
            return message
    except Exception:
        return message
    return (
        f"{message} — open **Accounts \u2192 Add a bank**, upload this same file and "
        "press **Try it**. It shows your statement laid out as the profile reads it, "
        "so you can see which column that figure landed in. Nothing is written there."
    )


def _metadata_preview(grid, header_row: int, proposed: dict[str, str]) -> dict[str, str]:
    """What the proposed labels resolve to in this file, masked. SPEC §45.

    Runs the **real** `_find_metadata`, with the operator's labels merged over
    the built-ins, rather than reimplementing the matching. Three strategies
    live in there (§44.5) and a second copy of them in the browser would drift
    from the one that decides whether an import succeeds.
    """
    from ...loaders import _table

    original = _table.META_LABELS
    try:
        _table.META_LABELS = {**original, **{COL_ALIASES_NORM(k): v for k, v in proposed.items()}}
        found = _table._find_metadata(grid, header_row)
    except Exception:  # a preview must never be the reason a page fails
        return {}
    finally:
        _table.META_LABELS = original

    out = {}
    for field, value in found.items():
        # The account number is the one being hunted for and the one that must
        # not be echoed (§11). Everything else here is already a label or a
        # branch code, neither of which is a credential.
        out[field] = mask_account(value) if field == "account_number" else value
    return out


@api.post("/banks/inspect")
@A.login_required
def bank_inspect():
    """`passbook inspect`, in the browser. SPEC §34.

    Adding a bank needs one thing the operator cannot get anywhere else: a look
    at their own file the way a parser sees it. That was CLI-only, which is a
    problem for the person who installed this from GitHub and has never opened
    a terminal.

    Reads the grid and **nothing else** — it does not stage the file, does not
    validate it, does not touch the registry and never writes. Safe on a
    statement from any bank, in any state, including one this cannot parse at
    all. That is the point: it is for the files that do NOT parse yet.
    """
    upload = request.files.get("statement")
    if upload is None or not upload.filename:
        return _fail("No file chosen.")

    scratch = Path(tempfile.mkdtemp(prefix="inspect-"))
    path = scratch / (secure_filename(upload.filename) or "statement")
    try:
        upload.save(path)
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            return _fail("That file is far too large to be a statement.", "too_large", 413)

        kind = sniff(path)
        password = (request.form.get("password") or "").strip() or None
        try:
            grid = read_grid(path, kind, password)
        except PdfPasswordWrong as exc:
            # A DIFFERENT code from "needs a password", and that is the point.
            # Both used to be `pdf_password`, which means "show the password
            # box" — and the box was already showing, so a wrong password
            # produced no message and no change at all. §41.
            return jsonify({"error": str(exc), "code": "pdf_password_wrong"}), 422
        except PdfPasswordRequired as exc:
            return jsonify({"error": str(exc), "code": "pdf_password"}), 422
        except Exception as exc:  # a grid reader is allowed to fail on junk
            return _fail(f"Could not read it: {type(exc).__name__}", "rejected", 422)

        if grid is None:
            return _fail(
                f"No reader for {kind!r}. Upload the spreadsheet or PDF your bank gives you.",
                "rejected",
                422,
            )

        # SPEC §45. The page sends the metadata labels it is proposing, and gets
        # back what they actually resolve to — masked. Without this the operator
        # types a label, saves the profile, uploads on another page, and finds
        # out there whether it worked. The answer belongs where the question is.
        proposed: dict[str, str] = {}
        raw = (request.form.get("metadata") or "").strip()
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    proposed = {
                        str(k).strip(): str(v).strip()
                        for k, v in loaded.items()
                        if str(k).strip() and str(v).strip()
                    }
            except ValueError:
                proposed = {}

        aliases = _all_aliases()
        best_row, best = 0, {}
        for index, row in enumerate(grid[:50]):
            found: dict[str, int] = {}
            for column, cell in enumerate(row):
                field = COL_ALIASES_NORM(cell)
                mapped = aliases.get(field)
                if mapped and mapped not in found:
                    found[mapped] = column
            if len(found) > len(best):
                best_row, best = index, found

        return jsonify(
            {
                "container": kind,
                "rows": len(grid),
                # repr() per cell, so a single space is visibly a space. That
                # distinction is the likeliest silent bug in the whole project.
                "grid": [[repr(cell)[:40] for cell in row[:10]] for row in grid[:20]],
                "headerRow": best_row if best else None,
                "matched": {field: column for field, column in sorted(best.items())},
                "missing": sorted(REQUIRED_COLS - best.keys()),
                "required": sorted(REQUIRED_COLS),
                "banks": list(SUPPORTED_BANKS),
                # What the proposed labels find, masked. §11 holds here as
                # everywhere: the account number never crosses this boundary in
                # full, not even to the page that is trying to locate it.
                "metaFound": _metadata_preview(grid, best_row, proposed),
            }
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# -- managing accounts, from the browser -------------------------------------
# SPEC §25. Registering, renaming and removing an account were CLI-only, which
# made the registry something only a terminal could reach — on the one screen
# where a wrong value is permanent, because the slug namespaces every
# `external_id` the account will ever push.

def _pending_password() -> str | None:
    """The password for the staged file, for this session only. §30.

    An encrypted PDF is decrypted at upload and then read again by the preview,
    the confirm and the payee inventory. Without carrying the password those
    later reads fail on a file the operator has already unlocked — which reads
    as the upload having silently half-worked.

    Session-scoped and never written to disk. The cookie is signed and
    httpOnly; signing out or discarding the file drops it.
    """
    return (session.get("pending_password") or "").strip() or None

@api.patch("/accounts/<slug>")
@A.login_required
def rename_account(slug: str):
    """Rename an account. SPEC §40.

    A **display** decision and nothing else. `slug` is untouched, because it
    namespaces `external_id` — renaming it would orphan every row already
    pushed under the old one (§21.1), silently, and the next push would create
    the whole account again as duplicates.

    This is not §23.4's problem in miniature. A *payee* rename moves the row out
    from under its own categorisation rule, because rules match the display
    name; an *account* name is matched by nothing. Nothing keys on it, nothing
    is pushed with it, and Firefly never sees it. It changes what the switcher,
    the masthead and the Payees caption call this account, and that is all.

    An empty name is a reset, not an error: it puts the account back to
    `Canara ****1111`, which is a name nobody has to maintain.
    """
    registry = load_accounts()
    account = find_account(registry, slug)
    if account is None:
        return _fail(f"No account {slug!r} is registered.", "unknown_account", 404)

    label = str((request.get_json(silent=True) or {}).get("label") or "").strip()
    if len(label) > ACCOUNT_LABEL_MAX:
        return _fail(
            f"That name is {len(label)} characters; keep it under {ACCOUNT_LABEL_MAX} "
            "so it fits in the switcher.",
            "too_long",
        )
    # Two accounts with one name is not a validation nicety: the switcher, the
    # masthead and every "which account is this" caption would then be unable to
    # tell them apart, which is the exact job the name exists to do.
    clash = next(
        (a for a in registry if a.slug != slug and label and a.display.casefold() == label.casefold()),
        None,
    )
    if clash is not None:
        return _fail(
            f"{clash.display} already goes by that name.", "duplicate_label"
        )

    renamed = account.renamed(label)
    save_accounts([renamed if a.slug == slug else a for a in registry])
    log.info("account %s renamed to %r", slug, renamed.display)
    return jsonify({"ok": True, "account": _account_summary(renamed, None)})


@api.get("/accounts/<slug>/removal")
@A.login_required
def account_removal(slug: str):
    """What removing this account would and would not do. SPEC §38.

    Removal is a **registry** edit, not a ledger one. Everything already in
    Firefly stays, `archive/` stays, and passbook simply stops routing to it
    and stops managing it: `reapply_preview` iterates the registry, so those
    rows fall out of every comparison and nothing will ever touch them again.

    Two things make that safe to offer from a button, and both are stated to
    the operator rather than assumed:

    * it deletes nothing, so there is nothing to restore from;
    * re-adding under the **same slug** reconnects it exactly, because
      `external_id` is `<slug>-<txn_id>` and the ids are derived, not stored.

    And one thing makes it sharp, which is why this endpoint exists at all
    instead of a bare DELETE: re-adding under a *different* slug mints new
    external_ids for the same rows, so a later push would duplicate the whole
    account rather than dedupe against it. The count is what makes that real —
    "this would leave 93 rows unmanaged" is a sentence an operator can act on
    and "are you sure?" is not.
    """
    registry = load_accounts()
    account = find_account(registry, slug)
    if account is None:
        return _fail(f"No account {slug!r} is registered.", "unknown_account", 404)

    rows: int | None = None
    reason = ""
    st = load_settings()
    if st.firefly_token:
        try:
            with FireflyClient(st.firefly_url, st.firefly_token) as client:
                rows = service.rows_in_ledger(client, account.asset_account)
        except FireflyError as exc:
            # Not fatal. The count sharpens the warning; it is not the warning,
            # and refusing to show the screen because Firefly is asleep would
            # make this the one management action that needs the stack up.
            reason = f"The ledger store did not answer, so the row count is unknown: {exc}"

    files = len(service.statements_for(account, service.archived_statements()))
    return jsonify(
        {
            "account": _account_summary(account, None),
            "ledgerRows": rows,
            "archiveFiles": files,
            "countReason": reason,
            "last": len(registry) == 1,
        }
    )


@api.delete("/accounts/<slug>")
@A.login_required
def remove_account(slug: str):
    """Forget an account. Deletes no transaction and no file. SPEC §38."""
    registry = load_accounts()
    account = find_account(registry, slug)
    if account is None:
        return _fail(f"No account {slug!r} is registered.", "unknown_account", 404)

    remaining = [a for a in registry if a.slug != slug]
    if remaining:
        save_accounts(remaining)
    else:
        # The file must not be left holding `accounts: []`. `load_accounts`
        # treats an empty registry as "never configured" and falls back to the
        # single-account settings path (§21.3), which is the right behaviour and
        # the only one that keeps a one-account install working — but a written
        # empty list is a different state from an absent file to anything that
        # reads the YAML, so remove the file instead of writing a lie into it.
        ACCOUNTS_FILE.unlink(missing_ok=True)

    # No server-side selection to clear: the scope is a query parameter and
    # `_account_scope` already drops slugs it does not recognise, so a browser
    # still holding this one falls back to the first account rather than 404ing.
    log.warning("account %s removed from the registry; no ledger rows were touched", slug)
    return jsonify(
        {
            "ok": True,
            "removed": slug,
            "remaining": [_account_summary(a, None) for a in remaining],
        }
    )

@api.get("/accounts/candidates")
@A.login_required
def account_candidates():
    """What a new account could be attached to. SPEC §26.

    Registering from the UI needs two things the operator should not have to
    look up: which Firefly asset accounts exist, and which are already claimed.
    Two registry accounts cannot share one asset account — they would merge in
    Firefly whatever the registry said — so the claimed ones are returned marked
    rather than hidden, because "why is my account not listed" is a worse
    question than seeing it greyed out.
    """
    st = load_settings()
    if not st.firefly_token:
        return _fail("FIREFLY_TOKEN is not set.", "unconfigured", 503)
    try:
        with FireflyClient(st.firefly_url, st.firefly_token) as client:
            names = [a["attributes"]["name"] for a in client.asset_accounts()]
    except FireflyError as exc:
        return _fail(f"The ledger store did not answer: {exc}", "firefly", 502)

    taken = {a.asset_account for a in load_accounts()}
    return jsonify(
        {
            "assetAccounts": [{"name": n, "taken": n in taken} for n in names],
            "banks": list(SUPPORTED_BANKS),
        }
    )


@api.post("/accounts/asset")
@A.login_required
def create_asset_account():
    """Create the Firefly asset account, so nobody has to leave passbook. §26.7.

    Registering a second account needed a Firefly asset account to exist first,
    which meant a trip to another app, three menus, and remembering to set the
    currency. That is the sort of step that stops a person adding their second
    account at all.

    Two guards, both of which Firefly would enforce anyway but which produce a
    much worse message from over there:

      * a name already in use — `uniqueAccountForUser` on the pinned tag;
      * a name already claimed by a registered passbook account, which is a
        different and more confusing failure (§21.1: two accounts sharing one
        asset account merge in Firefly whatever the registry says).
    """
    st = load_settings()
    if not st.firefly_token:
        return _fail("FIREFLY_TOKEN is not set.", "unconfigured", 503)

    name = " ".join(str((request.get_json(silent=True) or {}).get("name") or "").split())
    if not name:
        return _fail("Give the account a name.", "invalid", 422)
    if len(name) > 1024:
        return _fail("That name is too long.", "invalid", 422)

    if any(a.asset_account == name for a in load_accounts()):
        return _fail(f"{name!r} is already claimed by a registered account.", "invalid", 422)

    try:
        with FireflyClient(st.firefly_url, st.firefly_token) as client:
            if any(a["attributes"]["name"] == name for a in client.asset_accounts()):
                return _fail(f"The ledger already has an account called {name!r}.", "invalid", 422)
            name = _store_asset_account(client, name)
    except ValidationFailed as exc:
        return _fail(f"The ledger refused it: {exc}", "invalid", 422)
    except FireflyError as exc:
        return _fail(f"The ledger store did not answer: {exc}", "firefly", 502)

    return jsonify({"ok": True, "name": name})

def _store_asset_account(client, name: str, opening: "StatementMeta | None" = None) -> str:
    """Create one asset account and return the name Firefly settled on. §56.1.

    Shared by the explicit button and by registration, which creates it as part
    of registering rather than making the operator do it first: *"I dont want to
    create manually in Firefly again as I upload the statement in UI."*

    **The opening balance comes from the statement, and §95 is why.** Without
    it Firefly starts the account at zero, and every figure on it is short by
    the opening amount forever — the account balances against nothing, and the
    §20 balance check fails on a ledger that is otherwise perfectly correct.

    It was caught the only way it could be: `verify-ledger` reported
    `opening balance MISSING` on a freshly registered account with no rows in
    it yet, before a single transaction had been pushed. The registration flow
    created the account and never told it where the money started.

    `opening_balance` and `opening_balance_date` are `required_with` each other
    on the pinned tag (`Account/StoreRequest.php` line 107), so they go
    together or not at all — and `opening` being None is a legitimate case: the
    explicit "create in Firefly" button has no statement to read one from.
    """
    payload = {
        "name": name,
        "type": "asset",
        # required_if:type,asset — an asset account without a role is
        # rejected by the validator on the pinned tag.
        "account_role": "defaultAsset",
        "currency_code": CURRENCY,
        "include_net_worth": True,
        "active": True,
    }
    if opening is not None:
        # The day BEFORE the period starts. Dated on the first day it would sit
        # alongside that day's transactions, and Firefly would order it
        # arbitrarily among them — the opening balance is the state before any
        # of them, not one of them.
        payload["opening_balance"] = str(opening.opening_balance)
        payload["opening_balance_date"] = (
            opening.period_from - timedelta(days=1)
        ).isoformat()

    created = client.store_account(payload)
    log.info(
        "created Firefly asset account %r%s",
        name,
        f" opening {opening.opening_balance}" if opening else " (no opening balance)",
    )
    return (created.get("data") or {}).get("attributes", {}).get("name", name)

@api.post("/accounts/inspect")
@A.login_required
def account_inspect():
    """Stage a statement for an account that is **not registered yet**. SPEC §26.

    `/statement` refuses an unregistered account and deletes the staged file
    (§21.7), and that must not change: the property it protects is that a
    statement can never *silently* import into the wrong ledger. This route is
    not silent — it is reached only from the page whose entire purpose is
    registering an account, and it **cannot push**. Pushing still goes through
    `/statement/confirm`, by which point the account exists and the ordinary
    refusal applies again.

    Everything else the normal path does still happens: format sniffing, the
    balance-continuity invariant, and the file landing in `inbox/` staging.
    """
    upload = request.files.get("statement")
    if upload is None or not upload.filename:
        return _fail("No file chosen.")

    inbox: Path = current_app.config["INBOX"]
    inbox.mkdir(parents=True, exist_ok=True)
    name = secure_filename(upload.filename) or "statement"
    staging = inbox / f".staging-{name}"
    upload.save(staging)

    if staging.stat().st_size > MAX_UPLOAD_BYTES:
        staging.unlink(missing_ok=True)
        return _fail("That file is far too large to be a statement.", "too_large", 413)

    try:
        if sniff(staging) not in ACCEPTED_SNIFF:
            raise UnsupportedFormat(f"{name} is not a statement this can read")
        # Parses AND validates. A file whose balance chain does not hold is not
        # a statement to register an account from — the number in it cannot be
        # trusted either.
        password = (request.form.get("password") or "").strip() or None
        # For its side effect: it raises on a file that will not parse or
        # will not chain. The result is discarded because the file has not
        # been staged yet — it is re-read from its final home below.
        service.parse_statement(staging, password=password)
    except PdfPasswordWrong as exc:
        staging.rename(inbox / name)
        return jsonify({"error": str(exc), "code": "pdf_password_wrong"}), 422
    except PdfPasswordRequired as exc:
        staging.rename(inbox / name)
        return jsonify({"error": str(exc), "code": "pdf_password"}), 422
    except (ParseError, UnsupportedFormat) as exc:
        staging.unlink(missing_ok=True)
        return _fail(f"Rejected: {_with_try_hint(exc)}", "rejected", 422)
    except (BalanceBreak, IntegrityError) as exc:
        staging.unlink(missing_ok=True)
        # `balance_break`, not `invalid`: the client attaches "re-download the
        # statement rather than editing it" to this and that is advice about a
        # FILE. Under one code it also printed under "unknown field(s)" from a
        # bank-profile form, which is advice about nothing. §41.
        return _fail(f"Validation failed, nothing saved: {exc}", "balance_break", 422)

    destination = inbox / name
    staging.rename(destination)
    session["pending"] = str(destination)
    # No account yet — this is the inspect path, which runs BEFORE one is
    # registered. An unrouted staged statement shows on every tab, which is
    # right: it belongs to none of them until it is registered. §104.
    session.pop("pending_slug", None)
    session["pending_password"] = password or ""

    payload = _parsed(service.parse_statement(destination, password=password), filename=name)
    payload["unknown"] = []
    return jsonify(payload)


@api.post("/accounts")
@A.login_required
def accounts_add():
    """Register the staged statement's account. SPEC §21.3, §26.

    **The account number comes from the statement, never from a form.** It is in
    the file (§6.3), and a hand-typed one wrong by a digit would give the
    account its own namespace and its own ledger — silently, forever, because
    `external_id` is built from the slug and nothing would ever disagree with
    itself. So this reads the pending upload rather than accepting a number.
    """
    body = request.get_json(silent=True) or {}
    pending = session.get("pending")
    if not pending or not Path(pending).exists():
        return _fail(
            "Upload the new account's statement first — the account number is read "
            "from it, never typed.",
            "no_pending",
            409,
        )

    try:
        parsed = service.parse_statement(Path(pending), password=_pending_password())
    except (ParseError, BalanceBreak, IntegrityError) as exc:
        return _fail(str(exc), "unreadable", 422)

    # SPEC §56.1. Blank means "you decide" — the statement already knows the
    # account number, and `Union ****2222` is a better name than anything a
    # person types at this point in the flow. The operator can still name one.
    asset = str(body.get("assetAccount") or "").strip()
    bank = str(body.get("bank") or "canara").strip().lower()
    if not asset:
        asset = f"{bank.capitalize()} {parsed.meta.masked_account}".strip()

    registry = load_accounts()
    if any(a.asset_account == asset for a in registry):
        return _fail(
            f"{asset!r} already belongs to another account. Two accounts sharing one "
            "asset account merge in the ledger whatever the registry says.",
            "invalid",
            422,
        )

    number = parsed.meta.account_number.strip()
    already = next((a for a in registry if a.account_number.strip() == number), None)
    if already:
        # Idempotent, and says so. Re-uploading a statement for an account that
        # is already registered is an ordinary thing to do.
        return jsonify(
            {
                "ok": True,
                "created": False,
                "account": _registered(already),
            }
        )

    # **Create the Firefly side here, if it is not there.** Registering an
    # account and giving it somewhere to post are one intention, and splitting
    # them made the operator go and do half of it by hand: *"I dont want to
    # create manually in Firefly again as I upload the statement in UI."*
    #
    # Only when it does not already exist. Naming an account Firefly already
    # has attaches to it — that is the operator pointing at something they made
    # on purpose, and inventing a second one beside it would be the wrong kind
    # of helpful.
    made_asset = False
    st = load_settings()
    if not st.firefly_token:
        return _fail("FIREFLY_TOKEN is not set.", "unconfigured", 503)
    try:
        with FireflyClient(st.firefly_url, st.firefly_token) as client:
            if not any(a["attributes"]["name"] == asset for a in client.asset_accounts()):
                # §95. The statement is right here and it knows where the
                # money started; not passing it is what left a registered
                # account balancing against zero.
                asset = _store_asset_account(client, asset, parsed.meta)
                made_asset = True
    except ValidationFailed as exc:
        return _fail(f"The ledger refused the new account: {exc}", "invalid", 422)
    except FireflyError as exc:
        # Nothing has been written to the registry yet, so this is a clean stop.
        return _fail(f"The ledger store did not answer: {exc}", "firefly", 502)

    try:
        account = service.register_from_statement(
            parsed.meta, asset_account=asset, bank=bank
        )
    except (RegistryError, ValueError) as exc:
        return _fail(str(exc), "invalid", 422)

    log.info(
        "account registered from the UI: %s (%s)%s",
        account.slug,
        account.masked,
        " and its ledger account created" if made_asset else "",
    )
    return jsonify(
        {
            "ok": True,
            "created": True,
            "assetCreated": made_asset,
            "account": _registered(account),
        }
    )

def _registered(account) -> dict:
    """Masked to last 4, always — this crosses the boundary into a page (§11)."""
    return {
        "slug": account.slug,
        "label": account.display,
        "account": account.masked,
        "assetAccount": account.asset_account,
    }


# -- every row, searchable ----------------------------------------------------
# SPEC §27. The page passbook never had, and the last routine reason to open
# Firefly. Deliberately NO running balance: a Balance column over a filtered,
# reordered view asserts a continuity that is not there, and the balance chain
# is the spine of this project.

@api.get("/transactions")
@A.login_required
def transactions():
    """Every row in the ledger, searchable. SPEC §61.

    The page passbook never had, and the last routine reason to open Firefly.
    Firefly calls it the Audit report; here it is just the list.

    **Two sources, each authoritative for what it carries** — the same split as
    `/analysis`. Money, category and tags come from Firefly, because the rules
    engine assigns the category at store time (D5). The clock and the raw
    narration come from the statement, because `txn_time` is parsed out of the
    narration (§6.5) and never pushed.

    **There is deliberately no running balance.** §16.4 refuses one on any view
    that can be filtered or reordered, and this view is nothing but filtering
    and reordering: a Balance column over a search result asserts a continuity
    that is not there, and §6.6 is the spine of this project. The statement
    sheet keeps its balance column; this does not get one.
    """
    st = load_settings()
    scope, selected = _account_scope()
    start, end, window = _date_scope()
    if not st.firefly_token or not scope:
        return _fail(
            "FIREFLY_TOKEN is not set, or no account is registered.", "unconfigured", 503
        )

    query = (request.args.get("q") or "").strip().lower()
    want_category = (request.args.get("category") or "").strip()
    want_tag = (request.args.get("tag") or "").strip()
    direction = (request.args.get("direction") or "").strip().lower()
    # §83. The filters a fintech analyst actually reaches for, and the two the
    # page could not express: a size band and a sort. Both are applied AFTER
    # the text search, so `matched` is what the caption reports either way.
    want_min = _as_amount(request.args.get("min"))
    want_max = _as_amount(request.args.get("max"))
    sort = (request.args.get("sort") or "date").strip().lower()
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    limit = 100

    rows: list[dict] = []
    try:
        with FireflyClient(st.firefly_url, st.firefly_token) as client:
            live = {a["attributes"]["name"]: a["id"] for a in client.asset_accounts()}
            archive = current_app.config["ARCHIVE"]
            for account in scope:
                account_id = live.get(account.asset_account)
                if account_id is None:
                    return _fail(
                        f"No asset account named {account.asset_account!r}.",
                        "unconfigured",
                        503,
                    )
                mine = service.archived_transactions([account], archive)
                # Keyed on the namespaced external_id, never the bare txn_id:
                # two accounts at one bank emit identical ids (non-negotiable
                # 10), and this loop is building ONE list across accounts.
                clocks: dict[str, object] = {}
                narrations: dict[str, str] = {}
                for txn in mine:
                    key = account.external_id(txn.txn_id)
                    clocks[key] = txn.txn_time
                    narrations[key] = txn.narration
                    # **Both forms, exactly as `/analysis` does it.** The map
                    # was keyed on the namespaced id only and the fallback
                    # below stripped the namespace off the LOOKUP — probing a
                    # namespaced dict with a bare key, which misses by
                    # construction. That is §23.1's join bug turned around: a
                    # pre-migration row whose Firefly external_id is the bare
                    # `20260509000001` silently lost its time of day. Safe
                    # because `mine` is one account's statements, so the bare
                    # id is unambiguous here (§21.1).
                    clocks.setdefault(txn.txn_id, txn.txn_time)
                    narrations.setdefault(txn.txn_id, txn.narration)

                for group in client.account_transactions(account_id):
                    for split in group["attributes"]["transactions"]:
                        # Withdrawals and deposits only. Firefly's own audit
                        # report lists the opening balance too, but it is an
                        # account fact rather than a transaction: it has no
                        # payee, no external_id and no Out or In value, so it
                        # renders as an empty row — and it made this page count
                        # 114 where `verify-ledger` counts 113, which is the
                        # kind of off-by-one that gets read as a missing row.
                        if str(split.get("type") or "") not in ("withdrawal", "deposit"):
                            continue
                        external = str(split.get("external_id") or "")
                        moment = clocks.get(external)
                        if moment is None and external:
                            moment = clocks.get(service.txn_id_of(external))
                        narration = narrations.get(external) or narrations.get(
                            service.txn_id_of(external), ""
                        )
                        rows.append(
                            {
                                "id": external or str(split.get("transaction_journal_id") or ""),
                                "group": str(group["id"]),
                                "account": account.slug,
                                "accountLabel": account.display,
                                "date": str(split.get("date") or "")[:10],
                                "time": moment.isoformat() if moment else None,
                                "description": str(split.get("description") or ""),
                                "category": str(split.get("category_name") or ""),
                                "counterparty": service._counterparty(split),
                                "tags": sorted(str(t) for t in (split.get("tags") or [])),
                                "kind": str(split.get("type") or ""),
                                "amount": _money(service._split_amount(split)),
                                "narration": narration,
                            }
                        )
    except FireflyError as exc:
        return _fail(f"The ledger store did not answer: {exc}", "firefly", 502)

    total = len(rows)
    rows = [r for r in rows if _row_in_window(r, start, end)]
    outside = total - len(rows)

    if direction in ("in", "out"):
        wanted = "deposit" if direction == "in" else "withdrawal"
        rows = [r for r in rows if r["kind"] == wanted]
    if want_category:
        # `(no category)` is how the analysis names the empty one, so the same
        # string has to reach a row whose category really is empty.
        rows = [
            r
            for r in rows
            if (r["category"] or "(no category)") == want_category
        ]
    if want_tag:
        rows = [r for r in rows if want_tag in r["tags"]]
    if want_min is not None:
        rows = [r for r in rows if Decimal(r["amount"]) >= want_min]
    if want_max is not None:
        rows = [r for r in rows if Decimal(r["amount"]) <= want_max]
    if query:
        rows = [r for r in rows if _row_matches(r, query)]

    matched = len(rows)
    # Newest first by default. A ledger browser is opened to see what just
    # happened, which is the opposite of the statement sheet's order — and the
    # reason reordering is allowed here at all is that this view carries no
    # running balance to invalidate (§16.4).
    #
    # Amount sorts compare Decimals, never the display strings: `"9.00"` sorts
    # above `"10000.00"` lexically, which is the kind of wrong that looks fine.
    if sort == "amount":
        rows.sort(key=lambda r: (Decimal(r["amount"]), r["date"]), reverse=True)
    elif sort == "amount-asc":
        rows.sort(key=lambda r: (Decimal(r["amount"]), r["date"]))
    elif sort == "oldest":
        rows.sort(key=lambda r: (r["date"], r["id"]))
    else:
        rows.sort(key=lambda r: (r["date"], r["id"]), reverse=True)
    start_at = (page - 1) * limit
    return jsonify(
        {
            "rows": rows[start_at : start_at + limit],
            "matched": matched,
            "total": total,
            "outsideWindow": outside,
            "page": page,
            "pages": max(1, (matched + limit - 1) // limit),
            "window": window,
            "selected": selected,
            "accounts": [a.slug for a in scope],
            "sort": sort,
            # Every tag present in scope, so the tag filter can be a real
            # dropdown rather than something you have to already know.
            "tags": sorted({t for r in rows for t in r["tags"]}),
        }
    )


def _row_in_window(row: dict, start: date | None, end: date | None) -> bool:
    if start is None and end is None:
        return True
    day = _as_date(row["date"])
    if day is None:
        return False
    return (start is None or day >= start) and (end is None or day <= end)


def _row_matches(row: dict, query: str) -> bool:
    """Free text over everything a person would type.

    Includes the RAW narration, which is not rendered in the table: searching
    for a UTR or a bank reference is exactly the case where the display name is
    no help, and it is the reason this page can replace Firefly's search.
    """
    haystack = (
        f"{row['description']} {row['category']} {row['counterparty']} "
        f"{row['narration']} {row['amount']} {' '.join(row['tags'])}"
    )
    return query in haystack.lower()
