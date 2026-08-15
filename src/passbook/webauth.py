"""Web credentials: password, TOTP, backup codes, remembered devices. SPEC §16.

Flask-free on purpose. `passbook web-password` and `passbook web-totp` are
recovery paths that have to work when the container will not start, so nothing
here may import Flask.

Everything lives in `config/web-auth.json`, which is gitignored, mode 600, and
already bind-mounted writable so the UI can rewrite it. §15.5 explains why it is
there rather than in `.env`.

**Two different hashes, for two different threats.**

* The *password* is user-chosen and low-entropy, so it gets Werkzeug's scrypt.
* *Backup codes* and *device tokens* are generated here from `secrets`, at 50
  and 256 bits respectively. A slow KDF buys nothing against a value that
  cannot be guessed, and device tokens are checked on every single request —
  scrypt there would put ~100 ms on each page load. Both get salted SHA-256.

The stored file therefore never contains a recoverable secret except
`totp_secret`, which is unavoidable: TOTP verification needs the shared secret
in the clear. That is the same trade every authenticator makes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

WEB_AUTH_FILE = Path("config/web-auth.json")

BACKUP_CODE_COUNT = 8

# Warn from here down, not from zero. Codes are single-use, so the count only
# ever falls, and at zero a lost phone means `make web-totp RESET=yes` on the
# host is the sole remaining door (§16.2). Announcing that at nought is
# announcing it too late — the point of the warning is that there is still time
# to regenerate them from a browser you are already signed in to.
LOW_BACKUP_CODES = 2
DEVICE_REMEMBER_DAYS = 30

# Base32 without I/L/O/1 so a code read off a screen and typed back cannot be
# ambiguous. 10 chars from a 32-symbol alphabet is ~50 bits.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LEN = 10


def hash_password(password: str) -> str:
    """Werkzeug's default (scrypt)."""
    return generate_password_hash(password)


@dataclass
class WebAuth:
    username: str | None = None
    password_hash: str | None = None
    totp_secret: str | None = None
    totp_enrolled_at: str | None = None
    # The last TOTP counter accepted. A code is single-use within its window;
    # without this, a code shoulder-surfed or captured from a proxy stays valid
    # for its remaining seconds.
    totp_last_counter: int | None = None
    salt: str | None = None
    backup_codes: list[str] = field(default_factory=list)
    devices: list[dict] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password_hash)

    @property
    def totp_enrolled(self) -> bool:
        return bool(self.totp_secret)

    @property
    def backup_codes_left(self) -> int:
        return len(self.backup_codes)

    def to_json(self) -> str:
        return json.dumps(
            {
                "username": self.username,
                "password_hash": self.password_hash,
                "totp_secret": self.totp_secret,
                "totp_enrolled_at": self.totp_enrolled_at,
                "totp_last_counter": self.totp_last_counter,
                "salt": self.salt,
                "backup_codes": self.backup_codes,
                "devices": self.devices,
            },
            indent=2,
        ) + "\n"


def load(path: Path | None = None) -> WebAuth:
    """Read the store. A missing or unreadable file yields an empty WebAuth —
    which reads as "not configured", never as "authenticated"."""
    path = path or WEB_AUTH_FILE
    if not path.exists():
        return WebAuth()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError, OSError):
        return WebAuth()
    if not isinstance(data, dict):
        return WebAuth()
    return WebAuth(
        username=(data.get("username") or "").strip() or None,
        password_hash=(data.get("password_hash") or "").strip() or None,
        totp_secret=(data.get("totp_secret") or "").strip() or None,
        totp_enrolled_at=data.get("totp_enrolled_at") or None,
        totp_last_counter=data.get("totp_last_counter"),
        salt=(data.get("salt") or "").strip() or None,
        backup_codes=[c for c in (data.get("backup_codes") or []) if isinstance(c, str)],
        devices=[d for d in (data.get("devices") or []) if isinstance(d, dict)],
    )


def save(auth: WebAuth, path: Path | None = None) -> Path:
    """Write atomically, owner-only.

    Atomic because the UI rewrites this while serving: a half-written file locks
    the operator out of their own ledger with no way back except a terminal,
    which is the friction this whole phase exists to remove.
    """
    path = path or WEB_AUTH_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if not auth.salt:
        auth.salt = secrets.token_hex(16)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(auth.to_json(), encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)
    return path


# --- password -------------------------------------------------------------

# Verifying against a *stored* hash and verifying against nothing must cost the
# same, or response time reveals which usernames exist. Computed once, lazily,
# so import stays cheap; the value is never compared for truth, only for time.
_DUMMY: str | None = None


def _dummy_hash() -> str:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = generate_password_hash(secrets.token_urlsafe(32))
    return _DUMMY


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Constant-work password check.

    When there is no stored hash — unknown username, or an unconfigured
    install — this still runs a full scrypt verification against a throwaway
    hash before returning False. Without it the unknown-username branch returns
    in microseconds while the known-username branch takes ~100 ms, which is a
    trivially measurable account-enumeration oracle.
    """
    if not stored_hash:
        check_password_hash(_dummy_hash(), password)
        return False
    try:
        return check_password_hash(stored_hash, password)
    except (ValueError, TypeError):
        # An unparseable hash is a misconfiguration, not a wrong password.
        # Werkzeug returns False rather than raising for some shapes, so the
        # caller also shape-checks; this catches the rest.
        return False


# --- TOTP -----------------------------------------------------------------


def new_totp_secret() -> str:
    """A 160-bit base32 secret, the RFC 4226 recommendation."""
    import pyotp

    return pyotp.random_base32()


def totp_uri(secret: str, username: str, issuer: str = "passbook") -> str:
    import pyotp

    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


QR_BORDER = 4


def totp_qr_svg(uri: str) -> str:
    """QR as inline SVG, sized by its container. segno is pure-Python.

    **`omitsize=True` is load-bearing, not tidiness.** Left off, segno emits
    `width="45" height="45"` and *no* `viewBox`. An SVG with intrinsic pixel
    dimensions and no viewBox does not scale: `width:100%` grows the canvas
    while the drawing stays 45 px in the top-left corner. That rendered a QR
    far too small for a phone camera to lock onto, which is a silent failure —
    the page looks right, the scan just never succeeds. With `omitsize` segno
    writes a viewBox instead and CSS controls the size.

    `border=4` is the quiet zone the QR spec requires. segno defaults to 4 for
    a full QR, but it is stated here because it is the difference between a
    code a camera finds instantly and one it hunts for.

    `crispEdges` keeps the module edges hard when the browser scales the
    coordinate space up by 6-7x; the default antialiasing greys them and costs
    contrast at exactly the moment contrast is what the decoder needs.
    """
    import io

    import segno

    code = segno.make(uri, error="m")
    # segno writes bytes even for SVG, so this is a BytesIO, not a StringIO.
    buffer = io.BytesIO()
    code.save(
        buffer,
        kind="svg",
        scale=1,
        border=QR_BORDER,
        omitsize=True,
        svgclass=None,
        lineclass=None,
        xmldecl=False,
    )
    svg = buffer.getvalue().decode("utf-8")
    return svg.replace(
        "<svg ",
        '<svg preserveAspectRatio="xMidYMid meet" shape-rendering="crispEdges" ',
        1,
    )


def totp_qr_modules(uri: str) -> int:
    """Side length of the QR in modules, quiet zone included. For tests."""
    import segno

    return segno.make(uri, error="m").symbol_size(scale=1, border=QR_BORDER)[0]


def verify_totp(auth: WebAuth, code: str, *, at: datetime | None = None) -> bool:
    """Check a 6-digit code, then burn its counter.

    `valid_window=1` accepts the adjacent 30-second steps, which is what makes
    this usable on a laptop whose clock has drifted. Replay is blocked by
    refusing any counter at or below the last one accepted.
    """
    import pyotp

    if not auth.totp_secret:
        return False
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False

    totp = pyotp.TOTP(auth.totp_secret)
    now = at or datetime.now(timezone.utc)
    if not totp.verify(code, for_time=now, valid_window=1):
        return False

    # Which step actually matched — needed to reject a replay of it.
    step = int(now.timestamp()) // totp.interval
    matched = next(
        (s for s in (step - 1, step, step + 1) if totp.at(s * totp.interval) == code),
        step,
    )
    if auth.totp_last_counter is not None and matched <= auth.totp_last_counter:
        return False
    auth.totp_last_counter = matched
    return True


# --- backup codes ---------------------------------------------------------


def _digest(salt: str, value: str) -> str:
    return hmac.new(salt.encode("ascii"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_backup_codes(auth: WebAuth, count: int = BACKUP_CODE_COUNT) -> list[str]:
    """Return plaintext codes and store only their digests.

    Mandatory, not optional: TOTP alone means a lost or wiped phone locks the
    operator out of their own ledger. `passbook web-totp --reset` is the other
    way back, but it needs a terminal on the host, which is exactly what this
    phase removes from the routine.
    """
    if not auth.salt:
        auth.salt = secrets.token_hex(16)
    plain = [
        "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
        for _ in range(count)
    ]
    auth.backup_codes = [_digest(auth.salt, c) for c in plain]
    return plain


def consume_backup_code(auth: WebAuth, code: str) -> bool:
    """Single use. Removes the digest on success — mutates `auth`; save it."""
    if not auth.salt or not auth.backup_codes:
        return False
    normalised = (code or "").strip().upper().replace("-", "").replace(" ", "")
    if not normalised:
        return False
    wanted = _digest(auth.salt, normalised)
    for index, stored in enumerate(auth.backup_codes):
        if hmac.compare_digest(stored, wanted):
            del auth.backup_codes[index]
            return True
    return False


# --- remembered devices ---------------------------------------------------


def new_device_token() -> str:
    return secrets.token_urlsafe(32)


def remember_device(auth: WebAuth, token: str, days: int = DEVICE_REMEMBER_DAYS) -> None:
    if not auth.salt:
        auth.salt = secrets.token_hex(16)
    prune_devices(auth)
    auth.devices.append(
        {
            "digest": _digest(auth.salt, token),
            "expires": (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(),
        }
    )


def device_valid(auth: WebAuth, token: str | None) -> bool:
    """Does this device token let the second factor be skipped?

    Only the second factor. The password is still required every time — a
    remembered device is not a remembered session.
    """
    if not token or not auth.salt or not auth.devices:
        return False
    wanted = _digest(auth.salt, token)
    now = datetime.now(timezone.utc)
    for entry in auth.devices:
        if not hmac.compare_digest(entry.get("digest", ""), wanted):
            continue
        try:
            expires = datetime.fromisoformat(entry["expires"])
        except (KeyError, ValueError):
            return False
        return expires > now
    return False


def prune_devices(auth: WebAuth) -> int:
    now = datetime.now(timezone.utc)
    kept = []
    for entry in auth.devices:
        try:
            if datetime.fromisoformat(entry["expires"]) > now:
                kept.append(entry)
        except (KeyError, ValueError):
            continue
    dropped = len(auth.devices) - len(kept)
    auth.devices = kept
    return dropped


def forget_devices(auth: WebAuth) -> int:
    count = len(auth.devices)
    auth.devices = []
    return count


# --- migration ------------------------------------------------------------


def migrate_from_env() -> WebAuth | None:
    """Pick up a pre-§16 credential so an existing install is not locked out.

    Reads the §15.5 two-field file, or failing that the §14.6 `.env` pair.
    Returns None when there is nothing to migrate.
    """
    from .config import Settings

    path = WEB_AUTH_FILE
    if path.exists():
        return None
    settings = Settings()
    user = settings.passbook_web_user
    hashed = settings.web_password_hash
    if not user or not hashed:
        return None
    return WebAuth(username=user, password_hash=hashed, salt=secrets.token_hex(16))


def b32_pretty(secret: str) -> str:
    """Group the secret in fours — it is transcribed by hand when a camera
    cannot read the QR."""
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))
