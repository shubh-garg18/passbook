"""Rules, backups, the reminder and the status page."""

from __future__ import annotations

import os
from datetime import datetime


from flask import current_app, jsonify, request

from ... import audit, backup, ops, reminders, release, service
from ...config import (
    load_accounts,
    load_settings,
)
from ...store import LedgerError
from .. import auth as A

from ._base import (
    _artefact,
    _ledger,
    _fail,
    _sync,
    api,
    log,
)
from ._reconcile import (
    _dump_state,
    _ledger_verdict,
)
from ._scope import (
    _account_scope,
)


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
    # Best-effort, and after the fact. A backup that worked and an audit row
    # that did not is a backup that worked — `audit.record` swallows its own
    # failures for exactly this reason.
    try:
        with _ledger() as store:
            audit.record(store, "backup", f"took a database dump — {result.dump}")
    except LedgerError:
        pass
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


@api.get("/reminder")
@A.login_required
def reminder_get():
    try:
        schedule = reminders.load()
    except ValueError as exc:
        return _fail(str(exc), "bad_reminder", 500)
    return jsonify(_schedule_json(schedule))


@api.put("/reminder")
@A.login_required
def reminder_put():
    body = request.get_json(silent=True) or {}
    try:
        current = reminders.load()
    except ValueError as exc:
        return _fail(str(exc), "bad_reminder", 500)

    fields = ("enabled", "frequency", "weekday", "day_of_month", "hour", "minute", "lead_minutes")
    merged = {**current.to_dict(), **{k: body[k] for k in fields if k in body}}
    try:
        schedule = reminders.Schedule(**merged)
    except ValueError as exc:
        # The bounds are the message. A silently clamped hour is a reminder that
        # fires at a time nobody chose.
        return _fail(str(exc), "invalid", 422)

    reminders.save(schedule)
    log.info("reminder saved: %s (enabled=%s)", schedule.label, schedule.enabled)
    return jsonify(_schedule_json(schedule))


@api.put("/reminder/mail")
@A.login_required
def reminder_mail():
    """Save the mail server, from the UI rather than from `.env`. SPEC §24.4.

    An omitted `password` means *keep the stored one* — the field arrives empty
    because the page never received it, and treating that as "clear it" would
    delete the credential every time the operator corrected a typo in the host.
    Clearing is explicit: send `password: ""` with `clearPassword: true`.
    """
    body = request.get_json(silent=True) or {}
    current = reminders.load_mail()

    password = current.password
    if body.get("clearPassword"):
        password = ""
    elif body.get("password"):
        password = str(body["password"])

    try:
        mail = reminders.Mail(
            host=body.get("host", current.host),
            port=body.get("port", current.port),
            user=body.get("user", current.user),
            password=password,
            to=body.get("to", current.to),
        )
    except ValueError as exc:
        return _fail(str(exc), "invalid", 422)

    reminders.save_mail(mail)
    log.info("reminder mail settings saved (host=%s, configured=%s)", mail.host, mail.ready)
    return jsonify(_schedule_json(reminders.load()))


@api.post("/reminder/email")
@A.login_required
def reminder_email():
    """Mail the recurring invite to the calendar. SPEC §24.4.

    One message, sent once, carrying the recurrence — the calendar owns every
    firing after it. This adds no scheduler and nothing has to be running when
    the reminder is due.
    """
    st = load_settings()
    try:
        schedule = reminders.load()
        to = reminders.send_invite(schedule, st)  # file settings first, then .env
    except reminders.SendFailed as exc:
        # 422, not 500: the message names what to fix, and it is the operator's
        # configuration rather than a fault in the app.
        return _fail(str(exc), "email", 422)
    except ValueError as exc:
        return _fail(str(exc), "bad_reminder", 500)

    return jsonify({"ok": True, "to": reminders._mask_email(to), "label": schedule.label})


@api.get("/reminder.ics")
@A.login_required
def reminder_ics():
    """The calendar file itself. Downloaded, never fetched by anyone else.

    Not a subscription URL: this is bound to 127.0.0.1, so Google's servers
    cannot reach it and a subscribed calendar would silently stop updating.
    A downloaded file is honest about being a snapshot — and the UID makes
    re-importing an edit rather than a duplicate.
    """
    try:
        schedule = reminders.load()
    except ValueError as exc:
        return _fail(str(exc), "bad_reminder", 500)

    name = reminders.filename(schedule)
    reminders.assert_safe_filename(name)
    body = reminders.to_ics(schedule, url=request.host_url.rstrip("/"))
    return current_app.response_class(
        body,
        mimetype="text/calendar",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            # It is generated per request from config; a cached copy would hand
            # back yesterday's schedule after an edit.
            "Cache-Control": "no-store",
        },
    )


# --- what happened ----------------------------------------------------------


@api.get("/activity")
@A.login_required
def activity():
    """What has been done to this ledger, newest first.

    **Config is the half this exists for.** Every statement is in `archive/`
    and `verify-ledger` compares the ledger against it, so the rows have a
    paper trail already. Renaming a payee, moving a category, deleting one,
    removing an account — those change what the ledger *says* and left no trace
    anywhere, because `config/` is gitignored and has no history.

    It reports and never computes. No figure on any page comes from here.
    """
    action = (request.args.get("action") or "").strip() or None
    try:
        limit = min(500, max(1, int(request.args.get("limit") or 200)))
    except ValueError:
        limit = 200

    entries = []
    error = None
    try:
        with _ledger() as store:
            entries = audit.history(store, limit=limit, action=action)
    except LedgerError as exc:
        error = str(exc)

    return jsonify(
        {
            "entries": entries,
            "actions": list(audit.ACTIONS),
            "selected": action,
            "error": error,
        }
    )


# --- updates ----------------------------------------------------------------


@api.get("/update")
@A.login_required
def update_available():
    """Is there a newer passbook, and what changed in it?

    **This route reports. It does not update**, and that is a decision rather
    than an omission. Applying an update means pulling the repository and
    rebuilding an image, which needs the Docker socket — and the one container
    that listens on a port and parses uploaded files is the last place that
    socket may ever appear. `test_ops_only_ever_executes_rclone` asserts at AST
    level that this process can execute nothing but rclone, and shipping an
    updater here would be the first thing to break it.

    So the page names the one command to run. On Windows that command is a
    file to double-click.
    """
    found = release.check(force=request.args.get("force") == "1")
    return jsonify(
        {
            "current": found.current[:7],
            "currentDate": found.current_date,
            "latest": found.latest[:7],
            "latestDate": found.latest_date,
            # Tri-state. `null` is "could not check", which the page shows in
            # amber — never as a tick, and never as "you are behind" either.
            "behind": found.behind,
            "changes": found.commits,
            "error": found.error,
            # What to run. Sent by the server so the page cannot drift from the
            # Makefile, and so a Windows user is told about the launcher rather
            # than a command they have no shell for.
            "command": "make update",
            "windows": "launchers\\update-passbook.cmd",
        }
    )


# --- status ---------------------------------------------------------------


@api.get("/status")
@A.login_required
def status():
    st = load_settings()
    scope, selected = _account_scope()

    # Reachability, asked of the ledger itself rather than inferred from a
    # credential being present. The card used to report on a token's shape and
    # expiry, which said whether a string looked right — not whether anything
    # answered.
    accounts = None
    ledger_error = None
    try:
        with _ledger() as store:
            accounts = len(store.asset_accounts())
    except LedgerError as exc:
        ledger_error = str(exc)

    remote, remote_error = ops.remote_backups(os.environ.get("PASSBOOK_RCLONE_REMOTE"))
    auth = A.current_auth()

    return jsonify(
        {
            "sync": _sync(service.sync_status(scope, current_app.config["ARCHIVE"])),
            "store": {"accounts": accounts, "error": ledger_error},
            "account": {
                "assetAccount": scope[0].asset_account if len(scope) == 1 else None,
                "assertionConfigured": bool(load_accounts()),
                "selected": selected,
                "count": len(load_accounts()),
            },
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
