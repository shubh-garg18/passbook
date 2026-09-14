"""What a transaction's identity is, and how to read it back. SPEC §21.1.

This lives below `service` so that `firefly.push` can reach it: the pusher must
be able to ask "is this row already in the ledger?" before posting, and asking
means comparing identities. `service` re-exports every name here, so
`service.txn_id_of` remains the public spelling.
"""

import re

#: What a transaction id looks like, in the two forms a bank can give it.
#:
#: `20260509000001` — Canara's `YYYYMMDD` plus a per-date ordinal — and
#: `d-<16 hex>`, which is §44.4's hash of the whole row, used for a bank that
#: prints no per-row reference at all. Union is that bank.
#:
#: **The second form was missing.** SPEC §105: `_NAMESPACED` accepted only 14
#: digits, so `union-2222-d-0f4b…` matched nothing and every read of it was
#: wrong in a different way — `slug_of` said None, `txn_id_of` handed back the
#: whole namespaced string, and `verify-ledger` reported all 32 rows as
#: carrying "the bank's bare id" and told the operator to run a migration that
#: had already been run. A red cross for something that is fine is the same
#: failure as a green tick for something that is not (non-negotiable 11).
_TXN_ID_FORM = r"\d{14}|d-[0-9a-f]{16}"
_NAMESPACED = re.compile(rf"^(?P<slug>[a-z0-9][a-z0-9-]*)-(?P<txn_id>{_TXN_ID_FORM})$")


def txn_id_of(external_id: str) -> str:
    """The bank's own id, from either form.

    `canara-1111-20260509000001` -> `20260509000001`, and a bare id passes
    through. Tolerant reads are what let the migration (§21.2) be run when it
    suits instead of being forced by a version bump.
    """
    text = (external_id or "").strip()
    match = _NAMESPACED.match(text)
    return match.group("txn_id") if match else text


def slug_of(external_id: str) -> str | None:
    """Which account pushed this row, or None for a pre-migration id."""
    match = _NAMESPACED.match((external_id or "").strip())
    return match.group("slug") if match else None


def is_namespaced(external_id: str) -> bool:
    return bool(_NAMESPACED.match((external_id or "").strip()))
