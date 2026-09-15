"""Settings from .env. SPEC §3."""

import base64
import binascii
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml

from .yamlfile import read_yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import mask_account

PAYEE_ALIASES = Path("config/payee_aliases.yaml")


class Settings(BaseSettings):
    # extra="ignore": .env is shared with docker compose and holds APP_KEY,
    # DB_PASSWORD and friends, none of which belong to the CLI.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # repr=False so a stray traceback cannot spill them. SPEC §11.
    passbook_account_number: str | None = Field(default=None, repr=False)
    firefly_token: str | None = Field(default=None, repr=False)

    # Web UI (Phase 7). The password is stored only as a Werkzeug hash; the
    # plaintext never touches .env, the repo, or a log. SPEC §14.
    passbook_web_user: str | None = None
    passbook_web_secret: str | None = Field(default=None, repr=False)

    # Base64 (urlsafe) of the Werkzeug hash. Stored encoded, not raw, because a
    # raw scrypt hash is `scrypt:N:r:p$salt$digest` — it contains `$`, and an
    # UNQUOTED `$` value is truncated at the first `$` by `set -a; . ./.env`,
    # which `make check`, `backup` and `restore` all use. Measured, not
    # theorised. Base64 is alphanumeric plus `-_=`, so it needs no quoting,
    # cannot be word-split, cannot be interpolated, and cannot wrap.
    passbook_web_password_hash_b64: str | None = Field(default=None, repr=False)

    # The pre-v3.1 raw form. Still read so an existing setup keeps working, but
    # `make check` warns and `passbook web-password` removes it on write.
    passbook_web_password_hash: str | None = Field(default=None, repr=False)

    @property
    def web_password_hash(self) -> str | None:
        """The Werkzeug hash, however it happens to be stored."""
        if self.passbook_web_password_hash_b64:
            return decode_hash(self.passbook_web_password_hash_b64)
        return (self.passbook_web_password_hash or "").strip() or None

    # Canara encrypts the PDF statement. OPTIONAL: absent is fine until a PDF
    # is actually uploaded, and the loader's error then names this variable.
    # Measured for this account: the password is the LAST FOUR DIGITS of the
    # account number, not the Customer ID. Treated as a credential regardless —
    # never logged, never echoed, never rendered. SPEC §11, §6.8.
    canara_pdf_password: str | None = Field(default=None, repr=False)

    # --- the reminder, delivered by email. SPEC §24.4 ------------------------
    # Optional in every sense: absent, the Reminder page still writes a
    # calendar file to download. Present, it can mail the invite straight to
    # the address Google Calendar watches, which is one click instead of a
    # download and an import.
    #
    # For Gmail this is an APP PASSWORD (Google account -> Security -> 2-Step
    # Verification -> App passwords), never the account password. It is a
    # credential: repr=False, never logged, never returned by the API.
    passbook_smtp_host: str | None = None
    passbook_smtp_port: int = 587
    passbook_smtp_user: str | None = None
    passbook_smtp_password: str | None = Field(default=None, repr=False)
    # Where the invite goes. Defaults to the SMTP user, which for Gmail is the
    # address whose calendar it lands in.
    passbook_reminder_email: str | None = None

    @property
    def reminder_recipient(self) -> str | None:
        return (self.passbook_reminder_email or self.passbook_smtp_user or "").strip() or None

    @property
    def smtp_ready(self) -> bool:
        """Enough to send. Checked before a button is offered, not after."""
        return bool(
            self.passbook_smtp_host
            and self.passbook_smtp_user
            and self.passbook_smtp_password
            and self.reminder_recipient
        )

    firefly_url: str = "http://localhost:8080"
    # The Firefly asset account statements are posted into. Named rather than
    # guessed: an instance can hold several, and posting 93 rows into the wrong
    # one is tedious to undo.
    passbook_asset_account: str | None = None
    large_txn_threshold: Decimal = Decimal("10000")


def load_settings() -> Settings:
    return Settings()


ENV_FILE = Path(".env")

# Web credentials live in config/web-auth.json — see `passbook.webauth`, which
# owns reading and writing it. The two thin helpers that used to live here were
# removed in §16: they round-tripped only `username` and `password_hash`, so a
# caller who used them to save would silently erase the TOTP secret and the
# backup codes sitting in the same file.

def encode_hash(hash_value: str) -> str:
    """Werkzeug hash -> a single line safe for every consumer of .env."""
    return base64.urlsafe_b64encode(hash_value.encode("utf-8")).decode("ascii")


def decode_hash(encoded: str) -> str | None:
    """Reverse of encode_hash. None if it does not decode — a mangled value
    must fail as "misconfigured", never silently as "wrong password"."""
    try:
        # validate=True matters: without it base64 silently DISCARDS characters
        # outside the alphabet, so arbitrary junk "decodes" to plausible bytes
        # and a misconfiguration masquerades as a wrong password.
        # Whitespace from a hand-edit is stripped first, but nothing else is.
        return base64.urlsafe_b64decode(
            "".join(encoded.split()).encode("ascii"), 
        ).decode("utf-8") if _is_b64(encoded) else None
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


def _is_b64(value: str) -> bool:
    cleaned = "".join(value.split())
    return bool(cleaned) and all(
        c.isalnum() or c in "-_=" for c in cleaned
    ) and len(cleaned) % 4 == 0


def _entry_span(lines: list[str], index: int) -> int:
    """How many lines one KEY=... entry occupies.

    Usually one. But a value opened with a quote that does not close on the
    same line continues onto the next — which is exactly what a wrapped
    terminal paste produces, and exactly the breakage this function has to be
    able to clean up rather than leave half-replaced.
    """
    line = lines[index]
    _, _, value = line.partition("=")
    value = value.strip()
    if not value or value[0] not in "\"'":
        return 1
    quote = value[0]
    if len(value) > 1 and value.endswith(quote):
        return 1
    span = 1
    while index + span < len(lines):
        span += 1
        if lines[index + span - 1].rstrip().endswith(quote):
            break
    return span


def set_env_values(
    updates: dict[str, str],
    remove: list[str] | None = None,
    path: Path | None = None,
) -> Path:
    """Set keys in .env in place. Replaces, never appends a duplicate.

    Written rather than printed for pasting: a long value wrapped by the
    terminal and pasted as two lines produced a hash with a newline through the
    middle of it, which surfaced only as "login failed".
    """
    path = path or ENV_FILE
    remove = remove or []
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []

    out: list[str] = []
    seen: set[str] = set()
    index = 0
    while index < len(lines):
        line = lines[index]
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            index += _entry_span(lines, index)
            if key not in seen:
                out.append(f"{key}={updates[key]}\n")
                seen.add(key)
            continue
        if key in remove:
            index += _entry_span(lines, index)
            continue
        out.append(line)
        index += 1

    missing = [k for k in updates if k not in seen]
    if missing:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        for key in missing:
            out.append(f"{key}={updates[key]}\n")

    path.write_text("".join(out), encoding="utf-8")
    path.chmod(0o600)
    return path


ARCHIVE = Path("archive")

# Canara's net banking will not serve a statement from arbitrarily far back, so
# a long gap is not just lateness — it is a window that closes.
#
# Two thresholds because the two situations need different responses: past
# STALE you are late, past URGENT you are plausibly losing rows that no backup
# can recover, because they only ever existed at the bank.
SYNC_STALE_DAYS = 10
SYNC_URGENT_DAYS = 21


def last_sync(
    archive: Path | None = None, only: "set[Path] | None" = None
) -> tuple[str, int, date] | None:
    """Newest archived statement, its age in days, and the day it landed.

    `only` narrows it to a set of paths — the files belonging to the accounts in
    scope (§104). Passed in rather than derived here, because deciding which
    account a statement belongs to means reading the statement, and that is
    `service.statements_for`'s job and not this module's.

    Uses `archive/`, not `inbox/`: a file only lands there after a *successful*
    push, so it is a record of what actually reached the ledger rather than what
    was merely downloaded.

    The date is returned alongside the age rather than left to be recomputed,
    because the two must never disagree — a masthead stamped with one date next
    to a caption counting from another is the kind of small lie this project
    keeps finding.
    """
    archive = archive or ARCHIVE
    if not archive.is_dir():
        return None
    files = [p for p in archive.rglob("*") if p.is_file() and not p.name.startswith(".")]
    if only is not None:
        files = [p for p in files if p in only]
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    when = datetime.fromtimestamp(newest.stat().st_mtime)
    age = (datetime.now() - when).days
    return newest.name, age, when.date()


# --- the account registry. SPEC §21 ------------------------------------------
# Replaces PASSBOOK_ACCOUNT_NUMBER and PASSBOOK_ASSET_ACCOUNT, which could only
# ever describe one account. Gitignored like `rules.yaml` and
# `payee_aliases.yaml`: it names real account numbers (§11).
#
# **`bank` is present from day one although only `canara` is supported.** A
# second bank is the obvious next step, and the reshaping cost of adding the
# field later is the whole registry plus every `external_id` in the ledger — the
# migration this phase exists to do once.

ACCOUNTS_FILE = Path("config/accounts.yaml")

# Loaders exist per bank (§6.2 dispatches on magic bytes, not on this), so a
# statement can only be routed to an account whose bank has one.
# Canara is built in; anything else comes from a profile in `config/banks/`.
# The guard stays — the registry still refuses a bank nothing can read, so a
# statement can never be parsed by the wrong loader — but adding to it is now
# a YAML file rather than a code change. SPEC §27.
BUILTIN_BANKS = ("canara",)


def supported_banks() -> tuple[str, ...]:
    from .loaders.profiles import ProfileError, known_banks

    try:
        extra = tuple(known_banks())
    except ProfileError:
        # A broken profile must not make every bank unsupported; the parser
        # raises on it loudly at the point it actually matters.
        extra = ()
    return BUILTIN_BANKS + tuple(b for b in extra if b not in BUILTIN_BANKS)


class _SupportedBanks(tuple):
    """`SUPPORTED_BANKS` used to be a constant and is read in a few places.

    It stays subscriptable and iterable, but resolves through `supported_banks()`
    every time so a profile dropped into `config/banks/` is picked up without a
    restart — which is the whole point of a profile.
    """

    def __new__(cls):
        return super().__new__(cls, ())

    def __iter__(self):
        return iter(supported_banks())

    def __contains__(self, item):
        return item in supported_banks()

    def __len__(self):
        return len(supported_banks())

    def __getitem__(self, index):
        return supported_banks()[index]

    def __repr__(self):
        return repr(supported_banks())


SUPPORTED_BANKS = _SupportedBanks()

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class Account:
    """One account this install knows about.

    `slug` is the **namespace for `external_id`** (§21.1) and is therefore
    immutable once any row carries it: changing it orphans every pushed row from
    the statement that produced it.
    """

    slug: str
    bank: str
    account_number: str
    asset_account: str
    label: str = ""

    @property
    def masked(self) -> str:
        """Last 4 only — this is what may appear in a log or a page (§11)."""
        return mask_account(self.account_number)

    @property
    def bank_name(self) -> str:
        """`canara` → `Canara`. A slug is a filename; nothing in the UI shows one."""
        return " ".join(part.capitalize() for part in re.split(r"[-_]", self.bank) if part)

    @property
    def display(self) -> str:
        """What a person is shown when this account has to be named. SPEC §40.

        `Canara ****1111` unless the operator has renamed it, and the fallback
        matters more than it looks. It used to be the **Firefly asset account's
        name**, which is a string chosen in another app for another purpose: it
        can be anything, it can be the same for two accounts until Firefly
        refuses, and on a fresh install it is often just "Checking Account".
        Bank plus last four is the one label that can never name two of these
        and never needs explaining.
        """
        return self.label or f"{self.bank_name} {self.masked}".strip()

    def renamed(self, label: str) -> "Account":
        """A copy under a new name. **`slug` is untouched** — it namespaces
        `external_id`, so changing it would orphan every pushed row (§21.1).
        A rename here is a display decision and nothing else."""
        return replace(self, label=label.strip())

    def external_id(self, txn_id: str) -> str:
        """`canara-1111-20260509000001`. SPEC §21.1.

        The bank's own id is `YYYYMMDD` + a per-date ordinal **sequenced per
        account**, so two Canara accounts produce identical ids. Namespacing it
        by slug makes the id unique per user, keeps it derivable from
        statement + registry alone (so a re-push reproduces it byte for byte),
        and leaves it readable: the account is visible at a glance in Firefly, in
        a log line and in a purge-intent file.
        """
        return f"{self.slug}-{txn_id}"

    def to_dict(self) -> dict:
        out = {
            "slug": self.slug,
            "bank": self.bank,
            "account_number": self.account_number,
            "asset_account": self.asset_account,
        }
        if self.label:
            out["label"] = self.label
        return out


class RegistryError(ValueError):
    """The registry is unusable — never guessed around."""


def default_slug(bank: str, account_number: str) -> str:
    """`canara-1111`. A default, not an identity.

    Deliberately **not** the last 4 alone: `validate.assert_account` already
    documents that two accounts can share their last 4, and two banks can share
    it as well. The registry enforces uniqueness on top of this, and asks for an
    explicit slug when the default collides.
    """
    return f"{bank.strip().lower()}-{account_number.strip()[-4:]}"


def parse_accounts(data: dict) -> list[Account]:
    """Validate and build. Every failure is loud: a mis-parsed registry would
    route a statement into the wrong ledger, which is the one outcome §6.7 has
    existed to prevent since Phase 2."""
    entries = (data or {}).get("accounts") or []
    if not isinstance(entries, list):
        raise RegistryError("`accounts:` must be a list")

    accounts: list[Account] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RegistryError(f"accounts[{index}] is not a mapping")
        missing = [k for k in ("slug", "bank", "account_number", "asset_account") if not entry.get(k)]
        if missing:
            raise RegistryError(f"accounts[{index}] is missing {missing}")
        slug = str(entry["slug"]).strip()
        bank = str(entry["bank"]).strip().lower()
        if not _SLUG.match(slug):
            raise RegistryError(
                f"slug {slug!r} must be lowercase letters, digits and hyphens — "
                "it is part of every external_id in the ledger"
            )
        if bank not in SUPPORTED_BANKS:
            raise RegistryError(
                f"accounts[{index}] bank {bank!r} is not supported; "
                f"there is a loader for {', '.join(SUPPORTED_BANKS)} only"
            )
        accounts.append(
            Account(
                slug=slug,
                bank=bank,
                account_number=str(entry["account_number"]).strip(),
                asset_account=str(entry["asset_account"]).strip(),
                label=str(entry.get("label") or "").strip(),
            )
        )

    for field_name, getter in (("slug", lambda a: a.slug),
                               ("account_number", lambda a: a.account_number),
                               ("asset_account", lambda a: a.asset_account)):
        seen: dict[str, str] = {}
        for account in accounts:
            key = getter(account)
            if key in seen:
                # Masked in the message even for account_number: §11 holds in
                # error paths too, which is where full numbers usually leak.
                shown = account.masked if field_name == "account_number" else key
                raise RegistryError(
                    f"two accounts share {field_name} {shown!r} ({seen[key]} and "
                    f"{account.slug}) — a shared slug would merge two ledgers"
                )
            seen[key] = account.slug
    return accounts


def load_accounts(path: Path | None = None, settings: "Settings | None" = None) -> list[Account]:
    """The registry, or a one-account registry synthesised from `.env`.

    **Zero config for the single-account case** (§21.3): an install that predates
    this phase has `PASSBOOK_ACCOUNT_NUMBER` and `PASSBOOK_ASSET_ACCOUNT` and no
    registry file, and keeps working untouched — including `make dr-drill`, which
    passes those two variables into a recovered container. Nothing asks the
    operator to migrate a file they never knew existed.
    """
    path = path or ACCOUNTS_FILE
    if path.exists():
        try:
            data = read_yaml(path)
        except yaml.YAMLError as exc:
            raise RegistryError(f"{path} is not readable YAML: {exc}") from exc
        accounts = parse_accounts(data)
        if accounts:
            return accounts

    settings = settings if settings is not None else load_settings()
    number = (settings.passbook_account_number or "").strip()
    asset = (settings.passbook_asset_account or "").strip()
    if not number or not asset:
        return []
    return [
        Account(
            slug=default_slug("canara", number),
            bank="canara",
            account_number=number,
            asset_account=asset,
        )
    ]


def save_accounts(accounts: list[Account], path: Path | None = None) -> Path:
    """Write atomically. A half-written registry is a statement routed nowhere."""
    path = path or ACCOUNTS_FILE
    parse_accounts({"accounts": [a.to_dict() for a in accounts]})  # validate before writing
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Which accounts this install knows about. SPEC §21.\n"
        "#\n"
        "# GITIGNORED: it names real account numbers (§11). `make backup` carries\n"
        "# it in the encrypted config tarball, which is its only copy.\n"
        "#\n"
        "# `slug` is the namespace for every external_id this account pushes, so\n"
        "# it is IMMUTABLE once rows exist — changing it orphans them from the\n"
        "# statements that produced them.\n"
        "#\n"
        "# `payee_aliases.yaml` and `rules.yaml` are deliberately SHARED across\n"
        "# accounts: the same person's payees are the same whichever account paid,\n"
        "# and Firefly's categories are per-user. See §21.5.\n"
    )
    body = yaml.safe_dump(
        {"accounts": [a.to_dict() for a in accounts]}, sort_keys=False, allow_unicode=True
    )
    temp = path.with_suffix(".yaml.tmp")
    temp.write_text(header + body, encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)
    return path


def find_account(accounts: list[Account], slug: str) -> "Account | None":
    return next((a for a in accounts if a.slug == slug), None)


ATTRIBUTION_FILE = Path("config/attribution.yaml")


def load_attribution(path: Path | None = None):
    """`config/attribution.yaml` -> `service.Attribution`. SPEC §73.

    Absent or empty means no shift, which is exactly the behaviour before the
    feature existed — a reporting rule that changed every month bucket the day
    it shipped, by default, would be indefensible.
    """
    from decimal import Decimal

    from .service import Attribution

    target = path or ATTRIBUTION_FILE
    if not target.is_file():
        return Attribution()
    raw = read_yaml(target, default=None)
    # A top-level list, a string, or `null` are all things a hand-edited file
    # can be. `load_payee_aliases` already guards this way; without it a
    # malformed document raised AttributeError inside a request handler and
    # took the whole analysis page down with a 500 rather than being ignored.
    if not isinstance(raw, dict):
        return Attribution()
    keep = {}
    for external_id, amount in (raw.get("keep") or {}).items():
        # Decimal from the STRING form, never from a parsed float: PyYAML reads
        # `5000.10` as a float and `Decimal(float)` carries the binary error
        # into a money figure (non-negotiable 1).
        keep[str(external_id)] = Decimal(str(amount))
    # Clamped, not trusted. `to_day: 0` made `replace(day=min(0, 31))` raise
    # ValueError inside a request handler; 29-31 would silently skip February
    # for the same reason `reminders.Schedule` caps day-of-month at 28.
    def day(name: str, default: int) -> int:
        try:
            value = int(raw.get(name, default))
        except (TypeError, ValueError):
            return default
        return max(1, min(28, value))

    return Attribution(
        categories=frozenset(str(c) for c in (raw.get("categories") or [])),
        before_day=day("before_day", 10),
        to_day=day("to_day", 22),
        keep=keep,
    )


def load_payee_aliases(path: Path | None = None) -> dict[str, str]:
    """Truncated payee token -> canonical display name. SPEC §3, D10.

    Missing or empty file is normal and yields {} — aliases are operator
    knowledge, and there is nothing to infer from the statement alone.
    """
    path = path or PAYEE_ALIASES
    if not path.exists():
        return {}
    loaded = read_yaml(path)
    mapping = loaded.get("aliases") if isinstance(loaded, dict) else None
    if not mapping:
        return {}
    return {str(key): str(value) for key, value in mapping.items()}


def token_expiry(token: str) -> datetime | None:
    """Read the `exp` claim out of a JWT without verifying it or calling out.

    A Firefly Personal Access Token is an RS256 JWT valid for 365 days, and
    Firefly gives no warning before it lapses — the failure just looks like a
    generic 401. We only need the expiry, so the signature is irrelevant here;
    nothing is trusted on the basis of this value.

    Returns None if the token is not a JWT or carries no usable `exp`.
    """
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # restore base64url padding
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(exp, tz=timezone.utc)
