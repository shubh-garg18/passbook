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
is an IEEE double the moment it is parsed, and CLAUDE.md's first non-negotiable
does not stop at the process boundary. The client formats the string without
ever converting it.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, session
from werkzeug.utils import secure_filename

from .. import ops, service, webauth
from ..config import alias_drift, load_accounts, load_payee_aliases, load_settings, token_expiry
from ..configwrite import known_categories, plan_aliases, plan_categories
from ..firefly.bootstrap import bootstrap as bootstrap_rules
from ..firefly.bootstrap import load_rules
from ..firefly.client import FireflyClient, FireflyError
from ..firefly.purge import find_candidates
from ..firefly.purge import purge as purge_transactions
from ..loaders import UnsupportedFormat, sniff
from ..loaders._table import ParseError
from ..validate import AccountMismatch, BalanceBreak, IntegrityError, UnknownAccount
from . import auth as A

log = logging.getLogger(__name__)

api = Blueprint("api", __name__, url_prefix="/api")

# A Canara three-month export is ~30 KB. Ten megabytes is already absurd, and
# refusing early keeps a mistake from becoming a disk problem.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

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
    try:
        with FireflyClient(st.firefly_url, st.firefly_token) as client:
            live = {a["attributes"]["name"]: a["id"] for a in client.asset_accounts()}
            archived = service.archived_statements(current_app.config["ARCHIVE"])
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
                mine = service.statements_for(account, archived)
                for txn_id, moment in service.transaction_times(mine).items():
                    times[account.external_id(txn_id)] = moment
                    # Tolerated for a pre-migration ledger, where the pushed id is
                    # the bank's bare one.
                    times.setdefault(txn_id, moment)
                span = service.statement_coverage(mine)
                if span:
                    coverages.append(span)
    except FireflyError as exc:
        return _fail(f"Firefly did not answer: {exc}", "firefly", 502)

    coverage = (
        (min(c[0] for c in coverages), max(c[1] for c in coverages)) if coverages else None
    )
    result = service.ledger_analysis(splits, times=times, coverage=coverage)

    return jsonify(
        {
            "spend": _money(result.spend),
            "grossSpend": _money(result.gross_spend),
            "income": _money(result.income),
            "grossIncome": _money(result.gross_income),
            "withdrawals": result.withdrawals,
            "deposits": result.deposits,
            "categories": [_slice(s) for s in result.categories],
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
            "hours": result.hours,
            "clocked": result.clocked,
            "counted": result.counted,
            "uncategorised": _slice(result.uncategorised),
            "notSpend": result.not_spend,
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
    merged = dict(load_payee_aliases())
    merged.update({t: v.strip() for t, v in aliases_in.items() if v.strip()})

    try:
        plan_aliases(aliases_in).apply()
        plan_categories(categories_in, merged).apply()
    except KeyError as exc:
        return _fail(str(exc), "unknown_category", 422)

    st = load_settings()
    summary = "Config written."
    reapply_hint = False
    if st.firefly_token:
        try:
            with FireflyClient(st.firefly_url, st.firefly_token) as client:
                res = bootstrap_rules(client, load_rules(), st.large_txn_threshold)
            summary = (
                f"Config written. Rules: {len(res.created)} created, "
                f"{len(res.updated)} updated, {len(res.existing)} unchanged."
            )
            reapply_hint = bool(res.updated or res.created)
        except FireflyError as exc:
            summary = f"Config written, but bootstrap failed: {exc}"
    return jsonify({"ok": True, "summary": summary, "reapplyHint": reapply_hint})


# --- re-apply -------------------------------------------------------------


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

    return jsonify(
        {
            "considered": considered,
            "renames": sum(1 for c in changes if c.name_changed),
            "recats": sum(1 for c in changes if c.category_changed),
            "dump": _dump_state(),
            "changes": [
                {
                    "externalId": c.external_id,
                    "date": c.date,
                    "amount": _money(c.amount),
                    "oldDescription": c.old_description,
                    "newDescription": c.new_description,
                    "oldCategory": c.old_category,
                    "newCategory": c.new_category,
                    "nameChanged": c.name_changed,
                    "categoryChanged": c.category_changed,
                }
                for c in changes
            ],
        }
    )


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
