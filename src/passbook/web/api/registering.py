"""Registering an account from its own statement."""

from pathlib import Path
from flask import current_app, jsonify, request, session
from werkzeug.utils import secure_filename
from ... import service
from ...config import RegistryError, load_accounts, load_settings
from ...firefly.client import FireflyError, ValidationFailed
from ...loaders import UnsupportedFormat, sniff
from ...loaders._table import ParseError
from ...loaders.pdf import PdfPasswordRequired, PdfPasswordWrong
from ...validate import BalanceBreak, IntegrityError
from .. import auth as A
from ._base import _client, ACCEPTED_SNIFF, MAX_UPLOAD_BYTES, _fail, _parsed, api, log
from .accounts import _pending_password, _store_asset_account
from .banks import _with_try_hint


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
        with _client(st.firefly_url, st.firefly_token) as client:
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


