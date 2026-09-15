"""Comparing this config against the ledger, and writing the difference.

SPEC §107. These were spread across `ops`, `payees` and `reapply`, and the split
that separated those three surfaced why that was wrong: every one of them
imported from the other two. Verifying the ledger, taking the dump a rewrite
needs, shaping a preview and applying an in-place sync are one concern, and it
is not any of the three pages that use it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path


from flask import current_app

from ... import ops, service
from ...config import (
    load_accounts,
)
from ...store import LedgerError

from ._base import (
    _ledger,
    _money,
    log,
)



def _change(c) -> dict:
    return {
        "externalId": c.external_id,
        "date": c.date,
        "amount": _money(c.amount),
        "kind": c.kind,
        "oldDescription": c.old_description,
        "newDescription": c.new_description,
        "oldCategory": c.old_category,
        "newCategory": c.new_category,
        "oldCounterparty": c.old_counterparty,
        "newCounterparty": c.new_counterparty,
        "oldTags": list(c.old_tags),
        "newTags": list(c.new_tags),
        "nameChanged": c.name_changed,
        "categoryChanged": c.category_changed,
        "counterpartyChanged": c.counterparty_changed,
        "tagsChanged": c.tags_changed,
    }


def _preview(changes: list, considered: int) -> dict:
    """One shape for "what does config say about the rows already in the ledger".

    Shared by `/reapply` and by `/payees/diff`, which asks the same question
    about a config that has not been written yet.
    """
    # Which tags this change would REMOVE from rows, and how many rows each.
    # SPEC §33: a tag is what the roll-ups are built on, and losing one is a
    # semantic loss that a diff of payee lists cannot show. Moving three food
    # categories into an untagged one silently dropped `food` from 29 rows and
    # under-reported food spend — the number stayed plausible.
    lost: dict[str, int] = {}
    for change in changes:
        for tag in set(change.old_tags) - set(change.new_tags):
            lost[tag] = lost.get(tag, 0) + 1

    return {
        "considered": considered,
        "renames": sum(1 for c in changes if c.name_changed),
        "recats": sum(1 for c in changes if c.category_changed),
        "counterparties": sum(1 for c in changes if c.counterparty_changed),
        "retags": sum(1 for c in changes if c.tags_changed),
        "tagsLost": [{"tag": t, "rows": n} for t, n in sorted(lost.items())],
        "dump": _dump_state(),
        "changes": [_change(c) for c in changes],
    }


def _dump_state() -> dict:
    """Whether a database dump recent enough to run a purge exists.

    The UI used to offer a button reading "Back up, then purge and re-push" —
    directly above a note explaining that this container cannot take a database
    dump, because that needs the Docker socket it deliberately does not have
    (§15.1). The button promised the one thing the page had just said it could
    not do, on the only destructive action in the app. What it actually backs up
    is `config/`.
    """
    dump = ops.newest_dump()
    return {
        "name": dump[0] if dump else None,
        "ageMinutes": dump[1] if dump else None,
        "maxAgeMinutes": ops.REAPPLY_DUMP_MAX_AGE_MINUTES,
        "fresh": bool(dump and dump[1] <= ops.REAPPLY_DUMP_MAX_AGE_MINUTES),
    }


def _sync_now(store, st) -> dict:
    """Compare, write, then **re-read**. SPEC §23.

    The re-read is separate and separately guarded. A count of requests that
    returned 200 is a claim about the request; `remaining` is a claim about the
    ledger, and only the second is worth a tick (non-negotiable 11). If the
    verification itself cannot be done, `remaining` is `None` — *unverified*,
    which is a third state and never rendered as a pass.
    """
    archive = current_app.config["ARCHIVE"]
    changes, considered = service.reapply_preview(store, st, archive)
    result = service.sync_ledger(store, changes)

    remaining: int | None
    try:
        after, _ = service.reapply_preview(store, st, archive)
        remaining = len(after)
    except LedgerError as exc:
        # Rows were written. Losing the report of that because the *check*
        # failed would be the worst of both — silent writes and a silent error.
        log.warning("could not re-read the ledger after syncing: %s", exc)
        remaining = None

    return {
        "considered": considered,
        "attempted": len(changes),
        "updated": result.updated,
        "failed": result.failed,
        "failures": [{"externalId": e, "message": m} for e, m in result.failures[:20]],
        "remaining": remaining,
    }


def _synced_summary(synced: dict) -> str:
    """One sentence about the ledger, phrased on what is true after re-reading it."""
    if synced["considered"] == 0:
        # Zero compared is not zero differing (§23.1). Never phrase it as a pass.
        return (
            " No rows in the ledger were compared — nothing in archive/ matched a row's"
            " external_id, so the ledger was not checked."
        )
    if synced["attempted"] == 0:
        return f" Ledger already matched on all {synced['considered']} row(s)."
    parts = [f" Ledger: {synced['updated']} of {synced['attempted']} row(s) updated in place."]
    if synced["failed"]:
        parts.append(f" {synced['failed']} failed.")
    if synced["remaining"] is None:
        parts.append(" Whether any still differ could not be checked — the ledger stopped answering.")
    elif synced["remaining"]:
        parts.append(
            f" {synced['remaining']} row(s) still differ — those need a re-push, "
            "not an update."
        )
    return "".join(parts)


def _ledger_verdict(st, scope=None) -> dict:
    """The integrity check, for the Ledger strip.

    A check that cannot see something reports itself **unchecked**, and the
    strip must not paint that green: "cannot see" and "fine" are different,
    which is the whole lesson of the seven hours a ledger spent holding 21 of
    93 rows behind an all-green strip.
    """
    accounts = scope if scope is not None else load_accounts()
    if not accounts:
        return {"ok": None, "headline": "not configured", "checks": []}
    checks: list[service.Check] = []
    try:
        with _ledger() as store:
            for account in accounts:
                # Per account. One account's rows are missing from the other by
                # definition, so a single combined verdict would be noise; the
                # worst result across accounts is what the strip shows.
                verdict = service.verify_ledger(
                    store,
                    account,
                    current_app.config["ARCHIVE"],
                )
                prefix = f"{account.slug}: " if len(accounts) > 1 else ""
                checks.extend(
                    service.Check(f"{prefix}{c.name}", c.ok, c.detail) for c in verdict.checks
                )
    except LedgerError as exc:
        return {"ok": None, "headline": f"could not check: {exc}", "checks": []}
    combined = service.LedgerVerdict(checks)
    return {
        "ok": combined.ok,
        "headline": combined.headline,
        "failed": len(combined.failed),
        "unchecked": len(combined.unchecked),
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in combined.checks],
    }


def _run_config_backup() -> str:
    """Copy the config this container can actually reach.

    The database dump needs the Docker socket, which this container
    deliberately does not have (§15.3), so `make backup` stays a host action.
    """
    import tarfile

    backups = Path("backups")
    backups.mkdir(parents=True, exist_ok=True)
    target = backups / f"config-prereapply-{date.today():%Y-%m-%d}.tar.gz"
    with tarfile.open(target, "w:gz") as tar:
        for item in sorted(Path("config").glob("*")):
            tar.add(item, arcname=f"config/{item.name}")
    target.chmod(0o600)
    return str(target)
