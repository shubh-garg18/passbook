"""Upload, preview, confirm."""

from pathlib import Path
from flask import current_app, jsonify, request, session
from werkzeug.utils import secure_filename
from ... import service
from ...config import load_accounts, load_settings
from ...firefly.client import FireflyError
from ...loaders import UnsupportedFormat, sniff
from ...loaders._table import ParseError
from ...validate import AccountMismatch, BalanceBreak, IntegrityError, UnknownAccount
from .. import auth as A
from . import _base
from ._base import ACCEPTED_SNIFF, MAX_UPLOAD_BYTES, _fail, _parsed, api


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
        # Constructed through `_base` like every other route, so a test's fake
        # client reaches this path too — patching the package attribute cannot.
        client = (
            _base.FireflyClient(settings.firefly_url, settings.firefly_token)
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


