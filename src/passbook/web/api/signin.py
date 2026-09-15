"""Signing in: session, second factor, recovery, password. SPEC §16.2."""

from __future__ import annotations

from datetime import datetime, timezone


from flask import current_app, jsonify, request, session

from ... import reminders, webauth
from .. import auth as A

from ._base import (
    MIN_PASSWORD_LENGTH,
    _fail,
    api,
    log,
)


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
        auth,
        str(body.get("code") or ""),
        str(body.get("backupCode") or ""),
        str(body.get("recoveryCode") or ""),
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


@api.post("/session/recover")
def session_recover():
    """Email a one-time recovery code. SPEC §29.

    Reachable **only after the password step has passed** — it is a second
    factor, not a way around the first. The response is deliberately the same
    shape whether or not a code was sent for a *configured* account, but this is
    a single-user app on 127.0.0.1 and pretending an unconfigured mail server is
    a configured one would just leave the operator stuck with no explanation. So
    a missing address or mail server is reported plainly; a working one says
    only the masked address.
    """
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

    auth = A.current_auth()
    if not auth.recovery_email:
        return _fail(
            "No recovery address is set for this sign-in. Use a backup code, or "
            "`make web-totp RESET=yes` on the host.",
            "no_recovery_email",
            409,
        )

    try:
        code = webauth.issue_recovery_code(auth)
    except webauth.RecoveryError as exc:
        return _fail(str(exc), "no_recovery_email", 409)
    # Stored BEFORE the send. A code that reaches the inbox but was never
    # recorded is a code that cannot work, and the operator would have no way to
    # tell that from a wrong one.
    A.store_auth(auth)

    minutes = webauth.RECOVERY_TTL_SECONDS // 60
    try:
        reminders.send_text(
            "passbook sign-in code",
            f"Your passbook sign-in code is:\n\n    {code}\n\n"
            f"It works once and expires in {minutes} minutes.\n\n"
            "If you did not just try to sign in, someone has your password — "
            "change it, and the code above will not help them on its own.\n",
            to=auth.recovery_email,
        )
    except reminders.SendFailed as exc:
        # Burn it. A code that was minted but never delivered must not sit live
        # for ten minutes.
        auth.recovery = None
        A.store_auth(auth)
        log.warning("recovery code could not be sent: %s", exc)
        return _fail(str(exc), "email", 502)

    log.warning("recovery code emailed to %s", webauth.mask_email(auth.recovery_email))
    return jsonify(
        {
            "sent": True,
            "to": webauth.mask_email(auth.recovery_email),
            "expiresInMinutes": minutes,
        }
    )


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

    # Collected here because enrolment is the one moment the operator is
    # already thinking about being locked out. Asking later means asking never,
    # and a recovery address added after the phone is lost is no use at all.
    # Optional: backup codes are still issued, and a blank address just means
    # this account has one recovery path instead of two.
    raw_email = str(body.get("recoveryEmail") or "").strip()
    if raw_email:
        try:
            auth.recovery_email = webauth.normalise_email(raw_email)
        except webauth.RecoveryError as exc:
            return _fail(str(exc), "invalid", 422)

    auth.totp_secret = candidate
    auth.totp_enrolled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    auth.totp_last_counter = probe.totp_last_counter
    codes = webauth.generate_backup_codes(auth)
    A.store_auth(auth)
    session.pop("totp_candidate", None)
    A.begin_session(username)
    log.info("TOTP enrolled; %d backup codes issued", len(codes))

    # The only time these are ever readable. Stored as salted digests.
    return jsonify(
        {
            "stage": "done",
            "backupCodes": codes,
            "recoveryEmail": webauth.mask_email(auth.recovery_email),
        }
    )


@api.put("/recovery-email")
@A.login_required
def recovery_email():
    """Set or clear the recovery address. SPEC §29.

    Signed in only — changing where a sign-in code is delivered is itself a
    sensitive act, and doing it from a half-authenticated session would turn
    recovery into a way in.
    """
    raw = str((request.get_json(silent=True) or {}).get("email") or "").strip()
    auth = A.current_auth()
    if not raw:
        auth.recovery_email = None
        # An address that no longer receives must not leave a live challenge
        # addressed to it.
        auth.recovery = None
    else:
        try:
            auth.recovery_email = webauth.normalise_email(raw)
        except webauth.RecoveryError as exc:
            return _fail(str(exc), "invalid", 422)
    A.store_auth(auth)
    log.warning("recovery address %s", "cleared" if not raw else "changed")
    return jsonify({"ok": True, "recoveryEmail": webauth.mask_email(auth.recovery_email)})


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
