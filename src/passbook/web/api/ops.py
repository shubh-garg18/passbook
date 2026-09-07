"""Status, and the ledger's own verdict on itself."""

import os
from datetime import datetime, timezone
from flask import current_app, jsonify
from ... import ops, service
from ...config import alias_drift, load_accounts, load_settings, token_expiry
from ...firefly.client import FireflyError
from .. import auth as A
from ._base import _client, _artefact, _sync, api
from ._scope import _account_scope


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
        with _client(st.firefly_url, st.firefly_token) as client:
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
        with _client(st.firefly_url, st.firefly_token or "") as client:
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


