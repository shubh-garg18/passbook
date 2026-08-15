"""Flask-side authentication. SPEC §16.2.

Two factors, a remembered-device escape hatch, and a rate limiter.

The threat model changed between Phase 7 and here. Phase 7 was bound to
127.0.0.1 and the password existed mainly so that Phase 8 (Tailscale) would not
have to retrofit one. Phase 10 assumes the Tailscale exposure is coming, so:

* a password alone is no longer the boundary — TOTP is required;
* an unknown username must cost the same as a known one (see
  `webauth.verify_password`);
* repeated attempts are throttled, because a service reachable from a phone is
  reachable from whatever else is on the tailnet.

**Backup codes are mandatory, not offered.** A ledger you cannot open because
you replaced your phone is a worse outcome than the one TOTP prevents.
"""

from __future__ import annotations

import functools
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from flask import current_app, g, jsonify, request, session

from .. import webauth
from ..webauth import WebAuth

log = logging.getLogger(__name__)

SESSION_KEY = "operator"
PENDING_KEY = "pending_2fa"
DEVICE_COOKIE = "pb_device"
CSRF_COOKIE = "pb_csrf"
CSRF_HEADER = "X-Passbook-CSRF"

# Failures per window before the account is locked out, and for how long.
# Generous enough that a fat-fingered password twice is not an event; tight
# enough that online guessing is hopeless against a 12-character minimum.
MAX_ATTEMPTS = 6
WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 15 * 60

# The half-authenticated window between password and TOTP.
PENDING_TTL_SECONDS = 5 * 60


@dataclass
class _Bucket:
    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0


# In-memory, deliberately. One container, one operator; a restart clearing the
# counters is not a weakness an outside attacker can reach, and persisting it
# would mean writing to the credential file on every failed guess.
_BUCKETS: dict[str, _Bucket] = {}


def _bucket_key(username: str) -> str:
    # Keyed on the remote address too, so an attacker cannot lock the operator
    # out of their own tool simply by guessing the username repeatedly.
    return f"{request.remote_addr or '-'}|{(username or '').strip().lower()[:64]}"


def throttle_state(username: str) -> tuple[bool, int]:
    """(locked, seconds_remaining)."""
    bucket = _BUCKETS.get(_bucket_key(username))
    if not bucket:
        return False, 0
    now = time.monotonic()
    if bucket.locked_until > now:
        return True, int(bucket.locked_until - now) + 1
    return False, 0


def record_failure(username: str) -> None:
    key = _bucket_key(username)
    bucket = _BUCKETS.setdefault(key, _Bucket())
    now = time.monotonic()
    bucket.failures = [t for t in bucket.failures if now - t < WINDOW_SECONDS]
    bucket.failures.append(now)
    if len(bucket.failures) >= MAX_ATTEMPTS:
        bucket.locked_until = now + LOCKOUT_SECONDS
        bucket.failures.clear()
        log.warning(
            "login locked out for %ds after %d failures (username=%r)",
            LOCKOUT_SECONDS,
            MAX_ATTEMPTS,
            (username or "")[:64],
        )


def record_success(username: str) -> None:
    _BUCKETS.pop(_bucket_key(username), None)


def reset_throttle() -> None:
    """Test hook. Never called from a request path."""
    _BUCKETS.clear()


# --- credential loading ---------------------------------------------------


def current_auth() -> WebAuth:
    """The store, re-read per request.

    The UI rewrites this file — change password, enrol TOTP, burn a backup
    code — so caching it in app config would make a change take effect only at
    the next container restart.
    """
    injected = current_app.config.get("WEB_AUTH_FIXED")
    if injected is not None:
        return injected
    if "web_auth" not in g:
        g.web_auth = webauth.load()
    return g.web_auth


def store_auth(auth: WebAuth) -> None:
    if current_app.config.get("WEB_AUTH_FIXED") is not None:
        current_app.config["WEB_AUTH_FIXED"] = auth
        return
    webauth.save(auth)
    g.web_auth = auth


# --- the two factors ------------------------------------------------------


def check_password(username: str, password: str) -> tuple[bool, str]:
    """(ok, reason). The reason is for the server log only.

    The client is always told the same thing. Which half was wrong is free
    information to an attacker — but when it is *you* locked out, "bad password
    for a known username" and "no credential configured" are entirely different
    problems, and guessing between them from an identical page is an hour this
    project has already spent once.

    Both branches perform a full scrypt verification; see
    `webauth.verify_password` for why.
    """
    auth = current_auth()
    submitted = (username or "").strip()

    if not auth.configured:
        webauth.verify_password(None, password)
        if not auth.username and not auth.password_hash:
            return False, "no credential configured — run `make web-password`"
        if not auth.username:
            return False, "no username configured"
        return False, "no password hash configured — run `make web-password`"

    user_ok = secrets.compare_digest(submitted, auth.username or "")
    # Always verified, even when the username is wrong, so the two paths cost
    # the same. Against the real hash when the user matches; against a dummy
    # otherwise.
    password_ok = webauth.verify_password(
        auth.password_hash if user_ok else None, password
    )

    if not user_ok and not password_ok:
        return False, "unknown username and bad password"
    if not user_ok:
        return False, "unknown username"
    if not password_ok:
        return False, "bad password for a known username"
    return True, "ok"


def check_second_factor(auth: WebAuth, code: str, backup_code: str) -> tuple[bool, str]:
    """TOTP, or a single-use backup code. Mutates `auth` on success."""
    if backup_code:
        if webauth.consume_backup_code(auth, backup_code):
            store_auth(auth)
            log.warning(
                "backup code used — %d of %d remain",
                auth.backup_codes_left,
                webauth.BACKUP_CODE_COUNT,
            )
            return True, "backup code accepted"
        return False, "bad backup code"

    if webauth.verify_totp(auth, code):
        store_auth(auth)  # persists the burnt counter, blocking replay
        return True, "ok"
    return False, "bad or reused TOTP code"


# --- session --------------------------------------------------------------


def begin_session(username: str) -> None:
    session.clear()
    session[SESSION_KEY] = username
    session.permanent = False


def pending_username() -> str | None:
    """Who passed the password step, if that step is still fresh."""
    pending = session.get(PENDING_KEY)
    if not isinstance(pending, dict):
        return None
    started = pending.get("at", 0)
    if time.time() - started > PENDING_TTL_SECONDS:
        session.pop(PENDING_KEY, None)
        return None
    return pending.get("username")


def begin_pending(username: str) -> None:
    session[PENDING_KEY] = {"username": username, "at": time.time()}


def is_authenticated() -> bool:
    return bool(session.get(SESSION_KEY))


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return jsonify({"error": "Not signed in.", "code": "unauthenticated"}), 401
        return view(*args, **kwargs)

    return wrapped


# --- CSRF -----------------------------------------------------------------


def issue_csrf() -> str:
    token = request.cookies.get(CSRF_COOKIE)
    if not token or len(token) < 32:
        token = secrets.token_urlsafe(32)
    g.csrf_token = token
    return token


def csrf_ok() -> bool:
    """Double-submit: the header must match the cookie.

    SameSite=Strict already blocks the cross-site form post, and the API only
    accepts JSON. This is the third layer, and it is the one that still holds
    if a future change relaxes either of the other two.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    header = request.headers.get(CSRF_HEADER) or ""
    return bool(cookie) and secrets.compare_digest(cookie, header)


def totp_status(auth: WebAuth) -> dict:
    """What the client may know about the second factor. No secret, ever."""
    return {
        "enrolled": auth.totp_enrolled,
        "enrolledAt": auth.totp_enrolled_at,
        "backupCodesLeft": auth.backup_codes_left,
        # Computed here so the strip, the Status card and the Account page cannot
        # disagree about when to start worrying. See webauth.LOW_BACKUP_CODES.
        "backupCodesLow": auth.backup_codes_left <= webauth.LOW_BACKUP_CODES,
        "rememberedDevices": len(
            [d for d in auth.devices if _not_expired(d)]
        ),
    }


def _not_expired(entry: dict) -> bool:
    try:
        return datetime.fromisoformat(entry["expires"]) > datetime.now(timezone.utc)
    except (KeyError, ValueError, TypeError):
        return False
