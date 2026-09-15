"""Writing the current config onto rows already pushed. SPEC §23."""

from __future__ import annotations

from pathlib import Path


from flask import current_app, jsonify

from ... import ops, service
from ...config import (
    load_settings,
)
from ...firefly.bootstrap import bootstrap as bootstrap_rules
from ...firefly.bootstrap import load_rules
from ...firefly.client import FireflyError
from ...firefly.purge import find_candidates
from ...firefly.purge import purge as purge_transactions
from .. import auth as A

from ._base import (
    _client,
    _fail,
    _money,
    api,
    log,
)
from ._reconcile import (
    _dump_state,
    _ledger_verdict,
    _preview,
    _run_config_backup,
    _sync_now,
)


# --- re-apply -------------------------------------------------------------


@api.get("/reapply")
@A.login_required
def reapply_preview():
    st = load_settings()
    if not st.firefly_token or not st.passbook_asset_account:
        return _fail("FIREFLY_TOKEN or PASSBOOK_ASSET_ACCOUNT is not set.", "unconfigured", 503)
    try:
        with _client(st.firefly_url, st.firefly_token) as client:
            changes, considered = service.reapply_preview(
                client, st, current_app.config["ARCHIVE"]
            )
    except FireflyError as exc:
        return _fail(f"The ledger store did not answer: {exc}", "firefly", 502)

    return jsonify(_preview(changes, considered))


@api.post("/reapply/sync")
@A.login_required
def reapply_sync():
    """Write the current config onto the rows already in Firefly. SPEC §23.

    **This is what a payee edit was always supposed to do.** Renaming a payee
    used to write `config/` and sync the rules, and stop there: the ledger kept
    the names it was pushed with, and the only way to move them was
    `/reapply/run` — a purge and a full re-push, gated on a database dump the
    operator has to take on the host. A rename is not worth deleting a ledger
    for, so in practice it never happened.

    Three fields change on rows that already exist. Nothing is deleted, so
    there is no dump gate here: the operation is idempotent and config is the
    source of truth, which means a failed run is re-run rather than recovered.

    It cannot fix everything, and says so rather than implying otherwise. A row
    missing from Firefly, a wrong amount or a wrong date still need
    `/reapply/run`; §20's verdict is returned alongside so the difference is
    visible rather than assumed.
    """
    st = load_settings()
    if not st.firefly_token or not st.passbook_asset_account:
        return _fail("FIREFLY_TOKEN or PASSBOOK_ASSET_ACCOUNT is not set.", "unconfigured", 503)

    try:
        with _client(st.firefly_url, st.firefly_token) as client:
            synced = _sync_now(client, st)
    except FireflyError as exc:
        return _fail(f"The ledger store did not answer: {exc}", "firefly", 502)

    log.info(
        "in-place sync: %d updated, %d failed, %s still differ",
        synced["updated"],
        synced["failed"],
        "unknown" if synced["remaining"] is None else synced["remaining"],
    )
    return jsonify(
        {
            # `remaining is None` is unverified, not clean — it must not read as
            # ok (non-negotiable 11).
            "ok": synced["failed"] == 0 and synced["remaining"] == 0,
            **synced,
            "ledger": _ledger_verdict(st),
        }
    )


@api.post("/reapply/run")
@A.login_required
def reapply_run():
    """Back up, purge, sync rules, re-push, verify. SPEC §15.2.

    Order is load-bearing. The rules must reach Firefly *before* the re-push:
    they are applied at store time, so a rule the engine has not been told about
    cannot categorise anything. Skipping that step once produced six
    uncategorised rows while every other check still reported green.
    """
    st = load_settings()
    archive: Path = current_app.config["ARCHIVE"]
    steps: list[dict] = []

    # Enforced here, not only in the client. A disabled button is a courtesy; the
    # thing standing between a purge and an unrecoverable ledger has to be a
    # server-side refusal. Checked before anything is copied, deleted or pushed.
    state = _dump_state()
    if not state["fresh"]:
        log.warning("re-apply refused: newest dump is %s", state["ageMinutes"])
        return _fail(
            (
                "No database dump from the last "
                f"{state['maxAgeMinutes']} minutes. This deletes every row on the "
                "account and pushes them again, and the dump is the only way back. "
                "Run `make backup` on the host, then reload."
                + (
                    ""
                    if state["ageMinutes"] is None
                    else f" The newest is {state['name']}, {state['ageMinutes']} minutes old."
                )
            ),
            "stale_backup",
            409,
        )

    try:
        dump = _run_config_backup()
        steps.append({"state": "ok", "message": f"config backed up — {dump}"})
    except Exception as exc:
        return _fail(f"Backup failed, nothing was deleted: {exc}", "backup", 500)

    statements = sorted(
        p for p in archive.rglob("*") if p.is_file() and not p.name.startswith(".")
    )
    if not statements:
        return _fail("Nothing in archive/ to re-push.", "empty_archive", 409)

    with _client(st.firefly_url, st.firefly_token or "") as client:
        accounts = {a["attributes"]["name"]: a["id"] for a in client.asset_accounts()}
        account_id = accounts.get(st.passbook_asset_account)
        if account_id is None:
            return _fail(
                f"No asset account named {st.passbook_asset_account!r}.", "unconfigured", 503
            )

        candidates, protected = find_candidates(client, account_id)
        # Intent BEFORE the first delete (§19.7). If this request dies here — the
        # container restarts, the machine sleeps — the file is what makes the
        # half-finished state visible instead of merely coherent.
        result = purge_transactions(
            client,
            candidates,
            account=st.passbook_asset_account or "",
            statements=[str(p) for p in statements],
        )
        if not result.ok:
            return _fail(
                f"Purge failed ({result.failed} errors); nothing re-pushed. "
                f"Recorded as {result.intent.name if result.intent else 'no intent'} — "
                "run `passbook purge --resume` on the host.",
                "purge",
                500,
            )
        steps.append(
            {
                "state": "ok",
                "message": (
                    f"purged {result.deleted} row(s), {len(protected)} protected "
                    "(no external_id), trashed records force-deleted"
                ),
            }
        )

        boot = bootstrap_rules(client, load_rules(), st.large_txn_threshold)
        steps.append(
            {
                "state": "ok" if boot.ok else "bad",
                "message": (
                    f"rules synced — {len(boot.created)} created, "
                    f"{len(boot.updated)} updated, {len(boot.existing)} unchanged"
                ),
            }
        )

        if result.intent:
            ops.update_purge_intent(result.intent, stage="repushing")
        pushed = duplicates = failed = 0
        for path in statements:
            parsed = service.parse_statement(path)
            # Registry-aware (§21.2). The single-account form refused every
            # statement belonging to a second account — in the middle of a
            # rebuild, i.e. after the purge, which is the worst possible place
            # to stop. `allow_register=False`: a rebuild re-pushes what it just
            # deleted and must never invent an account while doing it.
            target = service.resolve_account(parsed.meta, st, client=client, allow_register=False)
            res = service.push_statement(parsed, st, client, account=target)
            pushed += res.pushed
            duplicates += res.skipped
            failed += res.failed
        steps.append(
            {
                "state": "ok" if not failed else "bad",
                "message": f"re-pushed {pushed}, {duplicates} already present, {failed} failed",
            }
        )

        balance = service.ledger_balance(st, client)

    expected = None
    try:
        newest = max(statements, key=lambda p: p.stat().st_mtime)
        expected = service.parse_statement(newest).meta.closing_balance
    except Exception:
        pass

    reconciles = expected is not None and balance is not None and balance == expected
    steps.append(
        {
            "state": "ok" if reconciles else "bad",
            "message": (
                f"balance {balance} vs statement closing {expected} — "
                + ("reconciles" if reconciles else "DOES NOT RECONCILE")
            ),
        }
    )

    # The intent is cleared only once the ledger itself verifies — not when the
    # last HTTP call returns. §19.7, and §20 is what does the verifying.
    verdict = _ledger_verdict(st)
    steps.append(
        {
            "state": "ok" if verdict["ok"] else "bad",
            "message": f"ledger integrity — {verdict['headline']}",
        }
    )
    if result.intent:
        if verdict["ok"] and reconciles and not failed:
            ops.clear_purge_intent(result.intent)
        else:
            steps.append(
                {
                    "state": "bad",
                    "message": (
                        f"{result.intent.name} kept: the cycle is unfinished, and the "
                        "record is what makes that visible. `passbook purge --resume`."
                    ),
                }
            )

    return jsonify(
        {
            "steps": steps,
            "balance": _money(balance),
            "expected": _money(expected),
            "reconciles": reconciles,
            "ledger": verdict,
        }
    )
