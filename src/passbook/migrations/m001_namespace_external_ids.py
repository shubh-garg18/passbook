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

**Why it cannot be an in-place edit.** `external_id` is the identity a row is
addressed by, so changing it is not an update — it is a different row. The only
path is the proven one: purge, then re-push from `archive/`. That is a delete,
which is why this migration refuses to start without a fresh dump and why
nothing is marked applied until the ledger verifies.
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

    Read from the ledger, not from a version file: a marker claiming this is
    done while the rows disagree is the same failure that let a ledger sit at
    21 of 93 rows behind an all-green strip.
    """
    from .. import service

    known = {a["name"] for a in ctx.store.asset_accounts()}
    out: dict[str, list[str]] = {}
    for account in ctx.registry:
        if account.asset_account not in known:
            continue
        stale = [
            external
            for row in ctx.store.account_transactions(account.asset_account)
            if (external := row.get("external_id")) and not service.is_namespaced(external)
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
    `passbook purge --confirm --yes` takes, followed by the same write `passbook
    sync` does, and it refuses to delete rows this machine cannot rebuild from
    `archive/`. A second copy of the most dangerous path in this project is the
    last thing a migration should be.
    """
    for account in ctx.registry:
        ctx.purge_and_repush(account)


def verify(ctx) -> str | None:
    stale = _bare_ids(ctx)
    if not stale:
        return None
    total = sum(len(v) for v in stale.values())
    return f"{total} row(s) still carry a bare id after the re-push"
