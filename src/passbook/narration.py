"""Narration grammars. SPEC §6.5.

Nine shapes observed across the 93-row reference statement. Each is a named
matcher, tried in order, with a permissive OTHER fallback. This module never
raises on an unrecognised narration.

Counts in the reference statement (May-Aug 2026):
    66  UPI/DR/     14  UPI/CR/      4  INET-IMPS-   3  NEFT
     1  UPI/REF/     1  UPI .. R01   1  PMSBY        1  SMS CHARGES
     1  SBINT        1  DEBIT CARD
"""

import re
from datetime import time

from .models import CHG, IMPS, INT, NEFT, OTHER, SCHEME, UPI, Transaction

# THE most important line in this module. SPEC §6.5.
# UPI narrations end with `DD/MM/YYYY HH:MM:SS`, whose slashes would otherwise
# add four spurious tokens to a naive split('/') and push the payee off by one.
# The optional leading `/` is consumed too, so the separator does not survive as
# a trailing empty token.
TRAILING_TIMESTAMP = re.compile(r"\s*/?\s*\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2}:\d{2})?\s*$")

# Stripping it is not the same as discarding it. The time of day is real signal
# and there is no time column on the statement, so this is the only place it
# exists. Searched anywhere rather than anchored: on `UPI/REF/` and
# `INET-IMPS-` the timestamp sits mid-string, followed by a trailing reference,
# so the anchored pattern above never fires on those.
EMBEDDED_TIMESTAMP = re.compile(r"\d{2}/\d{2}/\d{4}\s+(\d{2}):(\d{2}):(\d{2})")

_REVERSAL_CODE = re.compile(r"^R\d{2}$")


def strip_trailing_timestamp(narration: str) -> str:
    return TRAILING_TIMESTAMP.sub("", narration)


def extract_time(narration: str) -> time | None:
    """Recover the time of day from the narration's embedded timestamp.

    Returns None when the narration carries no time — plain-text shapes (SBINT,
    charges, PMSBY), NEFT, and the short `R01` reversal form, which has a date
    but no clock. Out-of-range values are ignored rather than raised on: this
    module never raises on a narration. SPEC §6.5.
    """
    match = EMBEDDED_TIMESTAMP.search(narration)
    if not match:
        return None
    hour, minute, second = (int(g) for g in match.groups())
    try:
        return time(hour, minute, second)
    except ValueError:
        return None


def _clean(token: str) -> str | None:
    token = token.strip()
    return token or None


def _handle(vpa_token: str) -> str | None:
    """`**15659@YBL` -> `YBL`. The VPA itself is masked by the bank and is not a
    reliable key; only the handle survives intact. SPEC §6.5."""
    if "@" in vpa_token:
        return _clean(vpa_token.rsplit("@", 1)[1])
    return None


def parse(narration: str, matchers: "tuple | None" = None) -> dict:
    """Return the fields narration.py owns. Never raises.

    `matchers` is the grammar set to try, in order — `bank.narration_matchers`
    for whichever bank wrote the statement (SPEC §22.5). It defaults to Canara's
    so every existing caller and the whole CLI keep working unchanged; a second
    bank passes its own tuple rather than editing this function.
    """
    matchers = CANARA_MATCHERS if matchers is None else matchers
    raw = narration.strip()
    fields: dict = {
        "channel": OTHER,
        "payee": None,
        "utr": None,
        "counterparty_bank": None,
        "is_reversal": False,
        "txn_time": extract_time(raw),
    }
    if not raw:
        return fields

    for matcher in matchers:
        got = matcher(raw)
        if got is not None:
            fields.update(got)
            return fields
    return fields


_WHITESPACE = re.compile(r"\s+")


def enrich(
    transactions: list[Transaction], aliases: dict[str, str] | None = None
) -> list[Transaction]:
    """Apply every narration-derived field, in the one order that is correct.

    Single definition on purpose: the CLI, the golden-file generator and the
    test fixtures all call this, so none of them can drift from the others.

    Order matters — the backfill needs every row parsed before it can match on
    UTR, and aliasing is last because it reads the payee the backfill may have
    just filled in.
    """
    for txn in transactions:
        for key, value in parse(txn.narration).items():
            setattr(txn, key, value)
        # SPEC §6.8: collapse whitespace runs in the PAYEE only.
        #
        # Canara's PDF wraps a narration across lines and the renderer discards
        # the whitespace run at the break, so `XENGRU  EP` in the XLS arrives as
        # `XENGRU EP` from the PDF. The payee token is the string rules match on
        # (D10), so without this the same transaction categorises differently
        # depending on which export it came from — which would make the
        # fallback a trap rather than a fallback.
        #
        # `narration` is deliberately NOT touched: §7.2 sends it to Firefly's
        # notes verbatim, and the raw string is per-format evidence. PDF-sourced
        # notes therefore differ in whitespace from XLS-sourced notes for the
        # same transaction. That is inherent to the format, not a defect.
        #
        # Verified collision-free on the reference statement: 52 distinct
        # tokens before collapsing, 52 after.
        if txn.payee:
            txn.payee = _WHITESPACE.sub(" ", txn.payee).strip()

    backfill_reversal_payees(transactions)

    if aliases:
        for txn in transactions:
            if txn.payee and txn.payee in aliases:
                txn.payee_alias = aliases[txn.payee]
    return transactions


def backfill_reversal_payees(transactions: list[Transaction]) -> int:
    """Give each reversal the payee of the debit it reverses. SPEC §6.5.

    The short `UPI/<UTR>/R01/DD/MM/YYYY` form carries no payee at all, so a
    refund would otherwise show up as `Unknown (UPI)` and be invisible when
    netting spend against a counterparty. The UTR is the link — verified on the
    reference statement, where the one reversal shares its UTR with exactly one
    other row.

    Only fills what is empty, and only from a non-reversal. Returns the count
    filled.
    """
    by_utr: dict[str, Transaction] = {}
    for txn in transactions:
        if txn.utr and not txn.is_reversal and txn.payee:
            # First writer wins: the original debit precedes its reversal.
            by_utr.setdefault(txn.utr, txn)

    filled = 0
    for txn in transactions:
        if not txn.is_reversal or txn.payee or not txn.utr:
            continue
        origin = by_utr.get(txn.utr)
        if origin is not None:
            txn.payee = origin.payee
            txn.counterparty_bank = txn.counterparty_bank or origin.counterparty_bank
            filled += 1
    return filled


# --- 2. UPI reversal / refund -------------------------------------------------
# `UPI/<UTR>/R01/DD/MM/YYYY` — short form, no DR/CR token. The UTR matches the
# original debit's UTR, which is what lets them be linked. Verified: the one
# reversal in the reference statement shares its UTR with exactly one other row.
def _upi_reversal(raw: str) -> dict | None:
    tokens = raw.split("/")
    if tokens[0] != "UPI" or not any(_REVERSAL_CODE.match(t.strip()) for t in tokens):
        return None
    return {
        "channel": UPI,
        "utr": _clean(tokens[1]) if len(tokens) > 1 else None,
        "is_reversal": True,
    }


# --- 1. UPI debit / credit / reference ----------------------------------------
# `UPI/DR/<UTR>/<payee>/<bank>/**<masked VPA>@<handle>/<purpose>//<RRN>/<ts>`
#
# SPEC §6.5 documented only the DR form. The reference statement also carries 14
# `UPI/CR/` and one `UPI/REF/`, all sharing this positional layout, so one
# matcher covers all three. Direction is NOT taken from here — the
# Withdrawals/Deposits columns are authoritative (SPEC §6.5).
_UPI_KINDS = ("DR", "CR", "REF")


def _upi_transfer(raw: str) -> dict | None:
    tokens = strip_trailing_timestamp(raw).split("/")
    if tokens[0] != "UPI" or len(tokens) < 4 or tokens[1].strip() not in _UPI_KINDS:
        return None
    return {
        "channel": UPI,
        "utr": _clean(tokens[2]),
        "payee": _clean(tokens[3]),
        "counterparty_bank": _handle(tokens[5]) if len(tokens) > 5 else None,
    }


# --- 3. IMPS ------------------------------------------------------------------
# `INET-IMPS-CR/<payee>/<bank>/<account>/<phone>/<phone>/DD/MM/YYYY HH:MM:SS/<ref>`
# The timestamp is mid-string here, not trailing, so the strip above is a no-op —
# harmless, because payee and bank are read positionally from the front.
def _imps(raw: str) -> dict | None:
    if not raw.startswith("INET-IMPS"):
        return None
    tokens = raw.split("/")
    return {
        "channel": IMPS,
        "payee": _clean(tokens[1]) if len(tokens) > 1 else None,
        "counterparty_bank": _clean(tokens[2]) if len(tokens) > 2 else None,
    }


# --- 4. NEFT ------------------------------------------------------------------
# `NEFT CR-<bank ref>-<IFSC>-<full payee>--<...>`
# Hyphen-separated, space inside the prefix, and the payee is NOT truncated.
# This is where inbound income lands.
_NEFT_PREFIX = re.compile(r"^NEFT\s+(CR|DR)-", re.IGNORECASE)


def _neft(raw: str) -> dict | None:
    if not _NEFT_PREFIX.match(raw):
        return None
    parts = raw.split("-")
    ifsc = parts[2].strip() if len(parts) > 2 else ""
    return {
        "channel": NEFT,
        "payee": _clean(parts[3]) if len(parts) > 3 else None,
        "utr": _clean(parts[1]) if len(parts) > 1 else None,
        # First 4 of an IFSC is the bank code: HDFC0000060 -> HDFC.
        "counterparty_bank": ifsc[:4] or None,
    }


# --- 5. Scheme / insurance ----------------------------------------------------
# `PMSBY RENEWAL(26-27) - <customer id> - <policy no>`
# Embeds the customer ID, which is also the PDF statement password. Nothing from
# the body is extracted — payee stays None so the ID cannot leak into a Firefly
# description or a payee report. SPEC §11.
def _scheme(raw: str) -> dict | None:
    if "PMSBY" not in raw.upper():
        return None
    return {"channel": SCHEME, "payee": "PMSBY"}


# --- 7. Savings interest ------------------------------------------------------
# `SBINT FOR THE PERIOD FROM<date> TO <date>` — note the missing space after
# FROM, which is the bank's own formatting, not a transcription slip.
def _interest(raw: str) -> dict | None:
    if not raw.upper().startswith("SBINT"):
        return None
    return {"channel": INT, "payee": "Savings Interest"}


# --- 6 & 8. Charges -----------------------------------------------------------
# `SMS CHARGES ON ACTUAL BASIS` and
# `DEBIT CARD ANNUAL CHARGES XXXXXXXXXXX<last4>` — both plain text.
def _charges(raw: str) -> dict | None:
    if "CHARGES" not in raw.upper():
        return None
    return {"channel": CHG, "payee": "Bank Charges"}


# --- the grammar set, as a bank hands it to the parser ------------------------
# SPEC §22.5. `banks/canara.py` registers this tuple; a second bank ships its own
# matchers in its own module and never touches this file. Order is meaningful —
# first hit wins — so the reversal shape, which is a prefix of the transfer
# shape, has to come first.
CANARA_MATCHERS: tuple = (
    _upi_reversal,
    _upi_transfer,
    _imps,
    _neft,
    _scheme,
    _interest,
    _charges,
)
