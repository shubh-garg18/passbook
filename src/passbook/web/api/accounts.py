"""The account registry — list, rename, remove."""

from datetime import timedelta
from flask import jsonify, request, session
from ... import service
from ...config import (
    ACCOUNTS_FILE,
    SUPPORTED_BANKS,
    find_account,
    load_accounts,
    load_settings,
    save_accounts,
)
from ...firefly.push import CURRENCY
from ...firefly.client import FireflyError, ValidationFailed
from ...models import StatementMeta
from .. import auth as A
from ._base import _client, ACCOUNT_LABEL_MAX, _fail, api, log
from ._scope import _account_scope, _account_summary


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
            with _client(st.firefly_url, st.firefly_token) as client:
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
        with _client(st.firefly_url, st.firefly_token) as client:
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
        with _client(st.firefly_url, st.firefly_token) as client:
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

