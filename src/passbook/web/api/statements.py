"""Upload, preview, push. SPEC §7.2, §30."""

from __future__ import annotations

from pathlib import Path


from flask import current_app, jsonify, request, session
from werkzeug.utils import secure_filename

from ... import service
from ...config import (
    load_accounts,
    load_settings,
)
from ...store import LedgerError
from ...loaders import UnsupportedFormat, sniff
from ...loaders._table import ParseError
from ...loaders.pdf import PdfPasswordRequired, PdfPasswordWrong
from ...validate import AccountMismatch, BalanceBreak, IntegrityError, UnknownAccount
from .. import auth as A

from ._base import (
    ACCEPTED_SNIFF,
    MAX_UPLOAD_BYTES,
    _ledger,
    _fail,
    _parsed,
    _pending_password,
    api,
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
                f"this looks like {kind!r}. Upload the statement your bank gives "
                "you: a spreadsheet (.xls or .xlsx) or a PDF. The file is checked "
                "by its contents, not its name, so a renamed file will not pass "
                "either."
            )

        # In memory for the length of this request and never stored (§30).
        # Most Indian banks hand out password-protected PDFs; decryption is
        # pikepdf, in this process. Nothing is ever uploaded anywhere — an
        # online "unlocker" would be handed the account number, the customer
        # ID, the address and every counterparty (non-negotiable 7).
        password = (request.form.get("password") or "").strip() or None
        parsed = service.parse_statement(staging, password=password)
        # §6.7 became §21.2: which of my accounts is this? An unregistered
        # account raises UnknownAccount (an AccountMismatch), which the handler
        # below turns into a 422 AND deletes the staged file — so it can never
        # be picked up by a later `make sync`.
        settings = load_settings()
        # Request-scoped like every other site, so an upload that goes on to
        # register an account does not open a second store.
        #
        # **The ledger is optional here, and that is deliberate.** A preview
        # writes nothing, and `resolve_account` consults the store only in one
        # case: an empty registry with no `PASSBOOK_ASSET_ACCOUNT` set, where
        # the single asset account it holds is the answer. Refusing to show
        # somebody their own statement because the database is down would be
        # the wrong trade, so the store is offered when it opens and omitted
        # when it does not.
        try:
            with _ledger() as store:
                account = service.resolve_account(parsed.meta, settings, store=store)
        except LedgerError:
            account = service.resolve_account(parsed.meta, settings, store=None)
    except (PdfPasswordRequired, PdfPasswordWrong) as exc:
        # A distinct code, because the remedy is a password rather than a
        # different file — and the file is kept STAGED so the retry does not
        # ask the operator to pick it again. §30.
        #
        # Two codes, not one: `pdf_password` means "show the box" and
        # `pdf_password_wrong` means "the box is already showing and the answer
        # was wrong". Collapsing them made a wrong password do nothing visible
        # at all, because the client's only response to `pdf_password` is to
        # open a box that was open. §41.
        staging.rename(destination)
        session["pending_encrypted"] = str(destination)
        code = "pdf_password_wrong" if isinstance(exc, PdfPasswordWrong) else "pdf_password"
        return jsonify({"error": str(exc), "code": code}), 422
    except (ParseError, UnsupportedFormat) as exc:
        staging.unlink(missing_ok=True)
        return _fail(f"Rejected: {exc}", "rejected", 422)
    except (BalanceBreak, IntegrityError) as exc:
        staging.unlink(missing_ok=True)
        # `balance_break`, not `invalid`: the client attaches "re-download the
        # statement rather than editing it" to this and that is advice about a
        # FILE. Under one code it also printed under "unknown field(s)" from a
        # bank-profile form, which is advice about nothing. §41.
        return _fail(f"Validation failed, nothing saved: {exc}", "balance_break", 422)
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
    # Which account it is FOR. §104: the "review and push it" banner is about
    # one account's statement, so it belongs on that account's page and not on
    # whichever one the switcher happens to be showing. Recorded here because
    # this is where the routing was decided — working it out again in
    # `/overview` would mean re-parsing the file on every page load, and for an
    # encrypted PDF, holding its password to do it.
    session["pending_slug"] = account.slug
    session.pop("pending_encrypted", None)
    # Kept for THIS session only, so `/statement/pending` and the confirm step
    # can re-read an encrypted file without asking again. Never written to disk;
    # the session cookie is signed and httpOnly.
    session["pending_password"] = password or ""

    parsed = service.parse_statement(destination, password=password)
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
    parsed = service.parse_statement(Path(pending), password=_pending_password())
    payload = _parsed(parsed)
    payload["unknown"] = service.unknown_tokens(parsed.transactions)
    return jsonify(payload)


@api.delete("/statement/pending")
@A.login_required
def discard_pending():
    pending = session.pop("pending", None)
    # Both of these go with it. A password left behind would be a credential
    # kept for a file that no longer exists, and a stale `pending_encrypted`
    # would offer a retry for nothing. §30.
    session.pop("pending_password", None)
    session.pop("pending_slug", None)
    encrypted = session.pop("pending_encrypted", None)
    for path in (pending, encrypted):
        if path:
            Path(path).unlink(missing_ok=True)
    return jsonify({"ok": True})


@api.post("/statement/confirm")
@A.login_required
def confirm_statement():
    pending = session.get("pending")
    if not pending or not Path(pending).exists():
        return _fail("Nothing pending — upload a statement first.", "no_pending", 404)

    st = load_settings()
    if not st.passbook_asset_account:
        return _fail("PASSBOOK_ASSET_ACCOUNT is not set.", "unconfigured", 503)

    parsed = service.parse_statement(Path(pending), password=_pending_password())
    try:
        account = service.resolve_account(parsed.meta, st, allow_register=False)
    except AccountMismatch as exc:
        return _fail(f"Refused: {exc}", "account_mismatch", 422)

    try:
        result = service.push_statement(parsed, st, account=account)
    except LedgerError as exc:
        return _fail(f"Push failed: {exc}", "ledger", 502)

    archived = None
    if result.ok:
        archived = str(
            service.archive_statement(
                parsed,
                current_app.config["ARCHIVE"],
                account,
                # So the archived copy is readable without it, forever (§105.1).
                # This is the last thing the password is used for; it is dropped
                # from the session three lines below.
                password=_pending_password(),
            )
        )
        session.pop("pending", None)
        session.pop("pending_password", None)
        session.pop("pending_slug", None)

    return jsonify(
        {
            "parsed": len(parsed.transactions),
            "pushed": result.pushed,
            # Already in the ledger, by identity (§119) or by the ledger's
            # content hash. The UI reads them as one number, "skipped".
            "already": result.already,
            "duplicates": result.duplicates,
            "failed": result.failed,
            "failures": [{"id": i, "message": m} for i, m in result.failures[:10]],
            "archived": archived,
        }
    )
