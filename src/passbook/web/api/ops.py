"""Status, and the ledger's own verdict on itself."""

import os
from datetime import datetime, timezone
from flask import current_app, jsonify
from ... import ops, service
from ...config import alias_drift, load_accounts, load_settings, token_expiry
from ...firefly.client import FireflyError
from .. import auth as A
from ._reconcile import _dump_state
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


@api.get("/backup")
@A.login_required
def backup_state():
    """Whether a backup can be taken here, and how old the newest one is. §37."""
    can, why = backup.available()
    return jsonify({"available": can, "reason": why, "dump": _dump_state()})


@api.post("/backup")
@A.login_required
def backup_run():
    """Take a database dump, from the UI. SPEC §37.

    This is the action `/reapply/run` is gated on, and until now it needed a
    terminal — so the most destructive thing in the app was guarded by a step
    the operator least likely to have a terminal could not perform.

    Reports what it did **and what it did not**: there is no git repository in
    this image, so no source bundle. The source is on GitHub; the ledger is not
    anywhere else, and letting one word cover both would be the more dangerous
    simplification.
    """
    try:
        result = backup.run()
    except backup.BackupFailed as exc:
        log.warning("backup failed: %s", exc)
        return _fail(str(exc), "backup", 500)

    log.warning("backup taken from the UI: %s", result.dump)
    return jsonify(
        {
            "ok": True,
            "dump": result.dump,
            "dumpBytes": result.dump_bytes,
            "config": result.config,
            "configBytes": result.config_bytes,
            "sourceBundle": result.source_bundle,
            # Re-read, so the freshness the purge gate uses is the one reported
            # here rather than one inferred from the write having returned.
            "state": _dump_state(),
        }
    )


# --- the reminder ---------------------------------------------------------
# SPEC §24. `service.sync_status` already tells the operator there is no cron to
# catch a late sync, because WSL2 stops when Windows sleeps. This is the answer
# to that, and the answer is deliberately not "run a scheduler here".


def _schedule_json(schedule, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    upcoming = reminders.next_occurrences(schedule, now, 5)
    return {
        **schedule.to_dict(),
        # Never returned. The UID is stable so a re-import updates the event
        # rather than duplicating it, and it has no business in a page.
        "uid": None,
        "label": schedule.label,
        "weekdays": list(reminders.WEEKDAYS),
        "frequencies": list(reminders.FREQUENCIES),
        "maxDayOfMonth": reminders.MAX_DAY_OF_MONTH,
        "timezone": reminders.TZID,
        # Computed here, from the same function that writes the RRULE, so the
        # list on screen cannot disagree with what the calendar will do.
        "upcoming": [m.isoformat(timespec="minutes") for m in upcoming],
        "filename": reminders.filename(schedule),
        # Zero configuration: Google takes the whole event, recurrence included,
        # as query parameters. This is why the mail server is optional.
        "googleUrl": reminders.google_calendar_url(schedule, now=now),
        # Whether the button can be offered at all, and where it would send.
        # The address is masked: §11 does not stop at account numbers, and this
        # is rendered on a page.
        "email": _email_state(),
    }


def _email_state() -> dict:
    """What the page needs to render the mail section.

    **The password is never here.** `hasPassword` says whether one is stored so
    the field can show a placeholder instead of an empty box that looks like the
    setting was lost; the value itself never crosses this boundary (§11).
    """
    mail = reminders.mail_settings(load_settings())
    return {
        "configured": mail.ready,
        "to": reminders._mask_email(mail.recipient) if mail.recipient else None,
        "host": mail.host,
        "port": mail.port,
        "user": mail.user,
        "recipient": mail.to,
        "hasPassword": bool(mail.password),
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


