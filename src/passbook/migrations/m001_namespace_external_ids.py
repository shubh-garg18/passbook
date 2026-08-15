"""Namespace every `external_id` with its account's slug. SPEC §21.1, §21.2.

**This is the baseline migration**, and on a fresh install it is a no-op: a
ledger built by any version that has this file already writes
`<slug>-<txn_id>`, so `pending()` finds nothing and `make upgrade` stamps the
version without touching anything.

It exists for the install that predates §21.1 — where every row carries the
bank's bare `YYYYMMDD` + ordinal id. Canara sequences that per account, so a
second Canara account emits *the same ids*: measured on the two committed
fixtures, 93 of 93 identical, and the same masked last four. A dict keyed on the
bare id merged two accounts and kept 93 of 186 rows with no error at all.

**Why it cannot be an in-place edit.** Firefly's API has no way to change an
`external_id` without rewriting the row, so the only path is the proven one from
§19.5: purge, then re-push from `archive/`. That is a delete, which is why this
migration refuses to start without a fresh dump and why nothing is marked
applied until §20 passes.
"""

from __future__ import annotations

VERSION = 1
NAME = "namespace-external-ids"
DESCRIPTION = (
    "Re-push every row so its external_id carries the account slug "
    "(canara-1111-20260509000001 rather than 20260509000001). Needed before a "
    "second account from the same bank can be added safely."
)


def _bare_ids(ctx) -> dict[str, list[str]]:
    """Rows still carrying the bank's bare id, per account slug.

    Read from Firefly, not from a version file: a marker claiming this is done
    while the rows disagree is the §19 failure in miniature.
    """
    from .. import service

    live = {a["attributes"]["name"]: a["id"] for a in ctx.client.asset_accounts()}
    out: dict[str, list[str]] = {}
    for account in ctx.registry:
        account_id = live.get(account.asset_account)
        if account_id is None:
            continue
        stale = [
            external
            for group in ctx.client.account_transactions(account_id)
            for split in group.get("attributes", {}).get("transactions", [])
            if (external := split.get("external_id")) and not service.is_namespaced(external)
        ]
        if stale:
            out[account.slug] = stale
    return out


def pending(ctx) -> str | None:
    stale = _bare_ids(ctx)
    if not stale:
        return None
    total = sum(len(v) for v in stale.values())
    where = ", ".join(f"{slug}: {len(ids)}" for slug, ids in sorted(stale.items()))
    return (
        f"{total} row(s) still carry the bank's bare transaction id ({where}). "
        "Those ids are sequenced per account and collide between accounts (§21.1)."
    )


def run(ctx) -> None:
    """Purge and re-push, per account, through the code that already does it.

    Nothing bespoke happens here. `ctx.purge_and_repush` is the same path
    `passbook purge --confirm --yes` and `passbook purge --resume` take: intent
    is recorded before the first delete, the tombstones are force-deleted so the
    re-push is not refused as duplicates (§7.3), every archived statement goes
    back, and the record is cleared only once §20 passes. A second copy of the
    most dangerous path in this project is the last thing a migration should be.
    """
    for account in ctx.registry:
        ctx.purge_and_repush(account)


def verify(ctx) -> str | None:
    stale = _bare_ids(ctx)
    if not stale:
        return None
    total = sum(len(v) for v in stale.values())
    return f"{total} row(s) still carry a bare id after the re-push"
