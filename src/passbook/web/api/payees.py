"""Naming payees and choosing their categories."""

from pathlib import Path
from flask import current_app, jsonify, request, session
from ... import service
from ...config import load_accounts, load_payee_aliases, load_settings
from ...configwrite import known_categories, plan_aliases, plan_categories
from ...firefly.bootstrap import bootstrap as bootstrap_rules
from ...firefly.bootstrap import load_rules
from ...firefly.client import FireflyError
from .. import auth as A
from ._base import _client, _fail, _payee_row, api, log
from ._scope import _account_scope
from ._reconcile import _sync_now, _synced_summary


# --- payees ---------------------------------------------------------------


def _all_transactions(scope=None):
    """Every archived statement plus anything pending, for the accounts in scope.

    **Deduped per account, then concatenated** (§21.1). Deduping across accounts
    on the bank's transaction id is the silent data loss this phase exists to
    prevent: two accounts, 186 rows, 93 survive, no error. `account_transactions`
    narrows first and dedupes inside.
    """
    archive: Path = current_app.config["ARCHIVE"]
    statements = service.archived_statements(archive)
    pending = session.get("pending")
    if pending and Path(pending).exists():
        try:
            statements.append(service.parse_statement(Path(pending)))
        except Exception as exc:  # a bad staged file must not blank the page
            log.warning("skipping pending %s: %s", Path(pending).name, exc)

    accounts = scope if scope is not None else load_accounts()
    if not accounts:
        # Pre-registry: one unnamed ledger, deduped as it always was.
        seen: dict[str, object] = {}
        for statement in statements:
            for txn in statement.transactions:
                seen.setdefault(txn.txn_id, txn)
        return list(seen.values())

    out: list[object] = []
    for account in accounts:
        mine = service.statements_for(account, statements)
        seen = {}
        for statement in mine:
            for txn in statement.transactions:
                seen.setdefault(txn.txn_id, txn)
        out.extend(seen.values())
    return out


@api.get("/payees")
@A.login_required
def payees():
    scope, selected = _account_scope()
    transactions = _all_transactions(scope)
    rows = service.payee_inventory(transactions)

    # Hour-of-day per row, for the Day Rail at aggregate scale. This is the
    # analysis that split Morning Stall from Late Counter by hand in Phase 4;
    # it belongs in the page rather than in a one-off script.
    #
    # Keyed on (token, channel) to match how `payee_inventory` groups rows. On
    # token alone, a token appearing under two channels would hand both rows
    # the same combined histogram while their counts differed — a chart
    # disagreeing with the number beside it. No token spans channels in the
    # current data, which is exactly why this would have gone unnoticed.
    hours: dict[tuple[str, str], list[int]] = {}
    clocked: dict[tuple[str, str], int] = {}
    for txn in transactions:
        key = (txn.payee or "(unparsed)", txn.channel)
        bucket = hours.setdefault(key, [0] * 24)
        clocked.setdefault(key, 0)
        if txn.txn_time:
            bucket[txn.txn_time.hour] += 1
            clocked[key] += 1

    return jsonify(
        {
            "rows": [
                {
                    **_payee_row(r),
                    "hours": hours.get((r.token, r.channel), [0] * 24),
                    # Stated separately because it is NOT r.count: NEFT, CHG,
                    # SCHEME and INT narrations carry no clock. A histogram
                    # labelled with the wrong denominator misinforms exactly
                    # the person who cannot see the chart.
                    "clocked": clocked.get((r.token, r.channel), 0),
                }
                for r in rows
            ],
            "categories": known_categories(),
            "selected": selected,
            "total": len(transactions),
            "totalClocked": sum(clocked.values()),
        }
    )


@api.get("/categories")
@A.login_required
def categories():
    """Only categories that already have a rule. D10: the UI never invents one."""
    return jsonify({"categories": known_categories()})


def _split_submission(body: dict) -> tuple[dict, dict]:
    aliases = {str(k): str(v) for k, v in (body.get("aliases") or {}).items()}
    categories_in = {str(k): str(v) for k, v in (body.get("categories") or {}).items()}
    return aliases, categories_in


@api.post("/payees/diff")
@A.login_required
def payees_diff():
    """Exactly what would change. Writes nothing."""
    aliases_in, categories_in = _split_submission(request.get_json(silent=True) or {})

    current = load_payee_aliases()
    alias_changes = {
        t: v for t, v in aliases_in.items() if (current.get(t) or "") != v.strip()
    }
    merged = dict(current)
    merged.update({t: v.strip() for t, v in alias_changes.items() if v.strip()})

    existing = service.rule_categories()
    category_changes = {
        t: v
        for t, v in categories_in.items()
        if existing.get((merged.get(t) or t)) != v and (v or existing.get(merged.get(t) or t))
    }

    try:
        changes = [plan_aliases(alias_changes), plan_categories(category_changes, merged)]
    except KeyError as exc:
        # D10 in force: an unknown category is refused with the known list,
        # never created on the operator's behalf.
        return _fail(str(exc), "unknown_category", 422)

    return jsonify(
        {
            "changes": [
                {"path": str(c.path), "diff": c.diff()} for c in changes if c.changed
            ],
            "aliasChanges": alias_changes,
            "categoryChanges": category_changes,
        }
    )


@api.post("/payees/apply")
@A.login_required
def payees_apply():
    aliases_in, categories_in = _split_submission(request.get_json(silent=True) or {})
    # Both read the map as it stands NOW — before `plan_aliases` rewrites it.
    current = load_payee_aliases()
    merged = _merged_aliases(current, aliases_in)
    renames = _display_renames(current, aliases_in)

    try:
        plan_aliases(aliases_in).apply()
        # Renames first, in the same plan: a rule matches the display name, so
        # relabelling a payee without following it through `rules.yaml` silently
        # de-categorises the payee (§24.4).
        plan_categories(categories_in, merged, renames=renames).apply()
    except KeyError as exc:
        return _fail(str(exc), "unknown_category", 422)

    st = load_settings()
    summary = "Config written."
    synced: dict | None = None
    if st.firefly_token:
        try:
            with _client(st.firefly_url, st.firefly_token) as client:
                res = bootstrap_rules(client, load_rules(), st.large_txn_threshold)
                summary = (
                    f"Config written. Rules: {len(res.created)} created, "
                    f"{len(res.updated)} updated, {len(res.existing)} unchanged."
                )
                # The second half of editing a payee, in the same request.
                # Config alone reaches only FUTURE pushes; the rows already in
                # Firefly are what the operator is looking at, and leaving them
                # for a separate destructive step meant they were never moved
                # at all. Rules first, then the rows.
                if st.passbook_asset_account:
                    synced = _sync_now(client, st)
                    summary += _synced_summary(synced)
        except FireflyError as exc:
            summary = f"Config written, but bootstrap failed: {exc}"
    return jsonify({"ok": True, "summary": summary, "synced": synced})


def _merged_aliases(current: dict[str, str], submitted: dict[str, str]) -> dict[str, str]:
    """The alias map as it will read after the write. Clearing one **removes** it.

    The removal is the point. `plan_aliases` deletes an entry whose new value is
    blank, but the merged map used to be built with `if v.strip()`, so a cleared
    alias survived here and nowhere else. `plan_categories` resolves a token to
    its display name through this map, so clearing an alias and choosing a
    category in the same submission filed the category under the alias that had
    just been deleted — while the row would be pushed under its raw token. The
    rule then matched nothing, silently, and the payee looked categorised on the
    page that had just written it.
    """
    merged = dict(current)
    for token, value in submitted.items():
        alias = (value or "").strip()
        if alias:
            merged[token] = alias
        else:
            merged.pop(token, None)
    return merged

def _display_renames(current: dict[str, str], alias_changes: dict[str, str]) -> dict[str, str]:
    """old display name -> new display name, for every token whose alias moved.

    The display name is what `description` carries and therefore what every
    rule matches on, so a rename has to be followed through `rules.yaml` or the
    category is silently orphaned. See `plan_rule_renames`.

    Renames onto themselves and empty names are dropped: a rule payee of `""`
    would match every description in the ledger.
    """
    renames: dict[str, str] = {}
    for token, value in alias_changes.items():
        was = (current.get(token) or token).strip()
        now = ((value or "").strip() or token).strip()
        if was and now and was != now:
            renames[was] = now
    return renames



