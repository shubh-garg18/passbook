"""Settings from .env. SPEC §3."""

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml
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


PAYEES_MD = Path("payees.md")
ARCHIVE = Path("archive")

# Canara's net banking will not serve a statement from arbitrarily far back, so
# a long gap is not just lateness — it is a window that closes.
#
# Two thresholds because the two situations need different responses: past
# STALE you are late, past URGENT you are plausibly losing rows that no backup
# can recover, because they only ever existed at the bank.
SYNC_STALE_DAYS = 10
SYNC_URGENT_DAYS = 21


def last_sync(archive: Path | None = None) -> tuple[str, int] | None:
    """Newest archived statement and its age in days, or None if never synced.

    Uses `archive/`, not `inbox/`: a file only lands there after a *successful*
    push, so it is a record of what actually reached the ledger rather than what
    was merely downloaded.
    """
    archive = archive or ARCHIVE
    if not archive.is_dir():
        return None
    files = [p for p in archive.rglob("*") if p.is_file() and not p.name.startswith(".")]
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    age = (datetime.now() - datetime.fromtimestamp(newest.stat().st_mtime)).days
    return newest.name, age


# Columns `passbook payees` generates. Anything else in payees.md is the
# operator's own and is carried across when the file is regenerated.
GENERATED_COLUMNS = {
    "#", "token", "len", "alias", "chan", "txns",
    "withdrawn", "deposited", "total", "first", "last",
}


def parse_payees_table(path: Path | None = None) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Return (header, {token: {column: value}}) from payees.md.

    Columns are located by header name rather than position, because the file
    gains and loses columns as the operator annotates it.
    """
    path = path or PAYEES_MD
    if not path.exists():
        return [], {}

    header: list[str] = []
    rows: dict[str, dict[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not header:
            if any(c.lower() == "token" for c in cells):
                header = cells
            continue
        if set("".join(cells)) <= {"-", ":"}:
            continue  # the |---|---| separator
        row = dict(zip(header, cells))
        token = next((v for k, v in row.items() if k.lower() == "token"), "")
        if token and token != "(unparsed)":
            rows[token] = row
    return header, rows


def parse_payees_markdown(path: Path | None = None) -> dict[str, str]:
    """Token -> alias, out of payees.md. Used only to report drift."""
    _, rows = parse_payees_table(path)
    out: dict[str, str] = {}
    for token, row in rows.items():
        alias = next((v for k, v in row.items() if k.lower() == "alias"), "")
        out[token] = alias
    return out


def alias_drift(
    yaml_path: Path | None = None, md_path: Path | None = None
) -> list[str]:
    """Report only genuine disagreement — an edit that would be lost.

    payees.md's Alias column is now *generated* from the yaml by
    `passbook payees`, so the yaml being ahead of the file is ordinary
    staleness, not drift: the next regeneration fixes it and nothing is at
    risk. Reporting that fired every time the UI was used as intended, which
    trains the warning to be ignored.

    What still matters is the other direction: payees.md claiming an alias the
    yaml does not have, or contradicting it. That is a hand-edit that the next
    regeneration will silently discard, so it is worth a word before it goes.

    **Detects, never syncs.** The yaml stays the source of truth; a markdown
    typo must never be able to change ledger behaviour.
    """
    yaml_aliases = load_payee_aliases(yaml_path)
    md_aliases = parse_payees_markdown(md_path)
    if not md_aliases:
        return []

    problems = []
    for token, md_alias in sorted(md_aliases.items()):
        if not md_alias:
            continue  # blank is fine — the yaml is authoritative
        yaml_alias = yaml_aliases.get(token)
        if not yaml_alias:
            problems.append(
                f"{token!r}: payees.md says {md_alias!r} but the yaml has no alias — "
                f"a hand-edit that regenerating payees.md would discard"
            )
        elif md_alias != yaml_alias:
            problems.append(
                f"{token!r}: payees.md says {md_alias!r}, yaml says {yaml_alias!r} — "
                f"the yaml wins; regenerate to sync"
            )
    return problems


# --- the account registry. SPEC §21 ------------------------------------------
# Replaces PASSBOOK_ACCOUNT_NUMBER and PASSBOOK_ASSET_ACCOUNT, which could only
# ever describe one account. Gitignored like `rules.yaml` and
# `payee_aliases.yaml`: it names real account numbers (§11).
#
# **`bank` is present from day one although only `canara` shipped first.** A
# second bank is the obvious next step, and the reshaping cost of adding the
# field later is the whole registry plus every `external_id` in the ledger — the
# migration §21.2 exists to do once.

ACCOUNTS_FILE = Path("config/accounts.yaml")

def supported_banks() -> tuple[str, ...]:
    """Every bank with a registered dialect. SPEC §22.5.

    Read from `passbook.banks` rather than kept as a literal here, so adding a
    bank is one new file and never a second list to remember. Imported lazily:
    `config` is imported by everything, and the bank modules import back into
    the parser.
    """
    from .banks import slugs

    return tuple(slugs())


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
    def display(self) -> str:
        return self.label or self.asset_account or self.slug

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
        if bank not in supported_banks():
            raise RegistryError(
                f"accounts[{index}] bank {bank!r} is not supported; "
                f"there is a dialect for {', '.join(supported_banks())} only. "
                "See docs/adding-a-bank.md."
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
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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


def load_payee_aliases(path: Path | None = None) -> dict[str, str]:
    """Truncated payee token -> canonical display name. SPEC §3, D10.

    Missing or empty file is normal and yields {} — aliases are operator
    knowledge, and there is nothing to infer from the statement alone.
    """
    path = path or PAYEE_ALIASES
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
