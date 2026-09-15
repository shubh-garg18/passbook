"""Build passbook's own ledger from `archive/`. DECISIONS.md §36.3.

**On a fresh install this is a no-op.** A ledger built by any version that has
this file already holds its rows, so `pending()` finds nothing and `make
upgrade` stamps the version without touching anything.

It exists for the install that predates §36, where the rows lived in a separate
application's database. That application is gone, its tables are not ours, and
**no row is read out of them** — the rebuild trusts `archive/` instead.

That is the whole point rather than an expedient. `archive/` holds the files the
bank produced, every one of them validated by the balance-continuity invariant
before it was archived, and rebuilding from them cannot inherit whatever was
wrong in the store being replaced. It is also not a new code path: it is the
disaster-recovery path, drilled on every `make dr-drill`.

**What an upgrading user sees.** `passbook upgrade --check` reports this as
pending and says how many statements it will read. `make upgrade` takes a
backup first, runs it, and refuses to stamp the version unless the ledger then
verifies against the archive it was built from.

**What they lose.** Nothing that came from a statement. Categories and tags are
re-derived from `config/rules.yaml` as each row is written, which is where they
came from originally; a category assigned by hand in the old application's own
UI, to a row, was never something passbook could see and is not recoverable
from here. `passbook payees` is where that decision belongs now, and it reaches
every row that carries the payee rather than the one that was clicked.
"""

from __future__ import annotations

VERSION = 2
NAME = "rebuild-from-archive"
DESCRIPTION = (
    "Write every archived statement into passbook's own ledger. Needed once, "
    "on an install whose rows were kept by a separate application."
)


def _missing(ctx) -> dict[str, tuple[int, int]]:
    """`slug -> (archived, stored)`, for accounts the ledger is short on.

    Read from the ledger and from `archive/`, never from a version file: a
    marker claiming this is done while the rows disagree is the same failure
    that let a ledger sit at 21 of 93 rows behind an all-green strip.
    """
    from .. import audit, service

    known = {a["name"] for a in ctx.store.asset_accounts()}
    statements = service.archived_statements()
    out: dict[str, tuple[int, int]] = {}
    for account in ctx.registry:
        mine = service.statements_for(account, statements)
        archived = {t.txn_id for s in mine for t in s.transactions}
        if not archived:
            continue
        stored = (
            {service.txn_id_of(i) for i in ctx.store.identities(account.asset_account)}
            if account.asset_account in known
            else set()
        )
        if archived - stored:
            out[account.slug] = (len(archived), len(stored))
    return out


def pending(ctx) -> str | None:
    short = _missing(ctx)
    if not short:
        return None
    where = ", ".join(
        f"{slug}: {stored} of {archived}" for slug, (archived, stored) in sorted(short.items())
    )
    return (
        f"the ledger is short of what archive/ holds ({where}). On an install "
        "upgrading from the separate ledger application, this is every row."
    )


def run(ctx) -> None:
    """Write each account's archived statements, through the path `sync` uses.

    Nothing bespoke happens here, and nothing is deleted: `push_statement`
    skips a row whose identity is already stored, so this is safe to re-run and
    safe on an account that is only partly rebuilt.

    The asset account is created first, with its **opening** balance taken from
    the earliest statement — without it every figure on the account is short by
    that amount forever, and the balance can never equal the bank's.
    """
    from .. import audit, service

    statements = service.archived_statements()
    for account in ctx.registry:
        mine = service.statements_for(account, statements)
        if not mine:
            ctx.say(f"{account.slug}: nothing in archive/ — skipped")
            continue

        earliest = min(mine, key=lambda s: s.meta.period_from)
        ctx.store.store_account(
            account.asset_account,
            earliest.meta.opening_balance,
            earliest.meta.period_from,
            "INR",
        )
        ctx.say(
            f"{account.slug}: opening {earliest.meta.opening_balance} "
            f"on {earliest.meta.period_from}"
        )

        written = already = failed = 0
        for parsed in mine:
            result = service.push_statement(
                service.parse_statement(parsed.path),
                ctx.settings,
                ctx.store,
                account=account,
            )
            written += result.pushed
            already += result.skipped
            failed += result.failed
        ctx.say(f"{account.slug}: wrote {written}, {already} already there, {failed} failed")
        audit.record(
            ctx.store,
            "rebuild",
            f"rebuilt {account.slug} from archive/: {written} row(s) from "
            f"{len(mine)} statement(s)",
            affected=written,
            account=account.slug,
        )
        if failed:
            raise RuntimeError(f"{account.slug}: {failed} row(s) could not be written")


def verify(ctx) -> str | None:
    short = _missing(ctx)
    if not short:
        return None
    where = ", ".join(
        f"{slug}: {stored} of {archived}" for slug, (archived, stored) in sorted(short.items())
    )
    return f"the ledger is still short after the rebuild ({where})"
