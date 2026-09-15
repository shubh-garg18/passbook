"""Naming and categorising, and the credit-card split. SPEC §16.1, §103."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path


from flask import current_app, jsonify, request, session

from ... import service
from ...config import (
    load_accounts,
    load_attribution,
    load_payee_aliases,
    load_settings,
)
from ...configwrite import (
    earnings_categories,
    plan_earnings,
    plan_settlement,
    plan_keep,
    known_categories,
    plan_aliases,
    plan_categories,
    plan_new_category,
    plan_remove_category,
)
from ...firefly.bootstrap import bootstrap as bootstrap_rules
from ...firefly.bootstrap import load_rules
from ...firefly.client import FireflyError
from ...service import is_namespaced
from .. import auth as A

from ._base import (
    _client,
    _fail,
    _money,
    _payee_row,
    _pending_password,
    api,
    log,
)
from ._reconcile import (
    _preview,
    _sync_now,
    _synced_summary,
)
from ._scope import (
    _account_scope,
    _date_scope,
    _within,
)


# --- payees ---------------------------------------------------------------


def _all_transactions(scope=None):
    """Every archived statement plus anything pending, for the accounts in scope.

    **Deduped per account, then concatenated** (§21.1). Deduping across accounts
    on the bank's transaction id is the silent data loss this phase exists to
    prevent: two accounts, 186 rows, 93 survive, no error. `account_transactions`
    narrows first and dedupes inside.
    """
    archive: Path = current_app.config["ARCHIVE"]
    accounts = scope if scope is not None else load_accounts()

    if not accounts:
        # Pre-registry: one unnamed ledger, deduped as it always was. Not worth
        # indexing — this path exists for an install that has never registered
        # an account, which by definition has almost nothing in it.
        seen: dict[str, object] = {}
        for statement in service.archived_statements(archive):
            for txn in statement.transactions:
                seen.setdefault(txn.txn_id, txn)
        out = list(seen.values())
    else:
        # §114. Out of the index rather than by loading every statement: this
        # is the read that grew with the whole history to answer a question
        # about one window. 6209ms -> 55ms at 800 statements.
        out = list(service.archived_transactions(accounts, archive))

    # The staged statement is NOT in the archive and must not be indexed — it
    # has not been pushed, and the index is a view of what has. Parsed and
    # appended here, which is where it always was.
    pending = session.get("pending")
    if pending and Path(pending).exists():
        try:
            staged = service.parse_statement(Path(pending), password=_pending_password())
            known = {t.txn_id for t in out}
            out.extend(t for t in staged.transactions if t.txn_id not in known)
        except Exception as exc:  # a bad staged file must not blank the page
            log.warning("skipping pending %s: %s", Path(pending).name, exc)
    return out


# --- the credit-card split. SPEC §103 ----------------------------------------


@api.get("/attribution")
@A.login_required
def attribution():
    """The settlements in scope, and how much of each is this month's own.

    > "Credit Card I say put in last month but give option to split some amount
    >  if any in this month"
    > "I want split option to be in dropdown category in Payees in another
    >  dropdown in just Credit Card"

    `Attribution.keep` has existed since §73 with no way to fill it in. This is
    the read half: which rows the shift actually applies to — the ones in a
    configured category, paid on or before `before_day` — with their current
    kept amount, so the operator picks from a list of their own bills instead of
    pasting an `external_id` into a YAML file.

    Rows come from the **archive**, not the ledger: the split is a reporting
    decision about a statement row, the archive is where those live, and asking
    the store for them would make a settings panel wait on a network call.
    """
    scope, selected = _account_scope()
    start, end, window = _date_scope()
    config = load_attribution()
    rules = load_rules()
    # Before anything is configured, offer the category the feature exists for
    # and say so — otherwise the picker is empty until a file exists that only
    # the picker can write. §103.
    configured = bool(config.categories)
    categories = set(config.categories) or set(service.DEFAULT_SETTLEMENT_CATEGORIES)

    rows = []
    for account in scope:
        mine = service.statements_for(account, service.archived_statements(
            current_app.config["ARCHIVE"]
        ))
        for txn in _within(service.dedupe_transactions(mine), start, end):
            # The same description the push builds and the rules match on:
            # `<alias or token> (<channel>)`, non-negotiable 13.
            name = txn.payee_alias or txn.payee or "(unparsed)"
            category = service.predict_category(f"{name} ({txn.channel})", txn.narration, rules)
            if category not in categories:
                continue
            if txn.txn_date.day > config.before_day:
                # Not a settlement. A payment made late in the month is this
                # month's own, and offering to split it would invite a shift
                # that never happens.
                continue
            if not txn.debit:
                # A refund or a reversal on the card. There is nothing to split
                # and offering a box for it would be a control that cannot act.
                continue
            external = account.external_id(txn.txn_id)
            rows.append(
                {
                    "externalId": external,
                    "account": account.display,
                    "date": txn.txn_date.isoformat(),
                    "amount": _money(txn.debit or Decimal(0)),
                    "payee": name,
                    "category": category,
                    "keep": _money(config.keep.get(external)) if external in config.keep else None,
                }
            )

    rows.sort(key=lambda r: r["date"], reverse=True)
    return jsonify(
        {
            "categories": sorted(categories),
            # False means these rows are NOT being shifted yet; the first split
            # turns it on. The panel says which, because "no effect" and "no
            # rows" look identical otherwise.
            "configured": configured,
            "beforeDay": config.before_day,
            "toDay": config.to_day,
            "rows": rows,
            "window": window,
            "selected": selected,
        }
    )


@api.put("/attribution")
@A.login_required
def set_attribution():
    """Set or clear how much of one settlement stays in the month it was paid.

    Keyed on the namespaced `external_id` and **refused for anything else**
    (non-negotiable 10): the bank sequences `txn_id` per account, so a bare id
    would silently split the wrong account's bill the day a second account is
    registered.
    """
    body = request.get_json(silent=True) or {}
    external = str(body.get("externalId") or "").strip()
    if not external:
        return _fail("Which row?", "invalid", 422)
    if not is_namespaced(external):
        return _fail(
            f"{external!r} is not a namespaced id. A split is keyed on "
            "`<account>-<transaction>`, because the bank numbers transactions "
            "per account and two accounts collide completely.",
            "invalid",
            422,
        )

    raw = body.get("keep")
    if raw in (None, "", "0"):
        amount = None
    else:
        try:
            amount = Decimal(str(raw))
        except InvalidOperation:
            return _fail(f"{raw!r} is not an amount.", "invalid", 422)
        if amount < 0:
            return _fail("A kept amount cannot be negative.", "invalid", 422)

    change = plan_keep(external, amount)
    if change.changed:
        change.apply()
    # Read back through the same loader the analysis uses, so the answer is
    # what the next chart will actually see rather than what was just written.
    kept = load_attribution().keep.get(external)
    return jsonify(
        {
            "ok": True,
            "externalId": external,
            "keep": _money(kept) if kept is not None else None,
            "diff": change.diff(),
        }
    )


@api.put("/attribution/settlement")
@A.login_required
def set_settlement():
    """Turn the previous-month shift on or off for one category. SPEC §103.3.

    Its own route because it is its own decision. It applies to **every**
    qualifying payment in the category, so switching it moves whole months at
    once — and it used to happen as a side effect of saving the first split,
    which meant typing 500 into one box moved three month buckets by tens of
    thousands.

    Refused for a category no rule knows, the same way `/categories` is: a
    settlement policy naming a category that cannot be assigned is a rule that
    can never fire.
    """
    body = request.get_json(silent=True) or {}
    category = str(body.get("category") or "").strip()
    if not category:
        return _fail("Which category?", "invalid", 422)
    known = known_categories()
    if category not in known:
        return _fail(
            f"{category!r} has no rule. Known: {', '.join(known)}.",
            "unknown_category",
            422,
        )

    change = plan_settlement(category, bool(body.get("settles")))
    if change.changed:
        change.apply()
    config = load_attribution()
    return jsonify(
        {
            "ok": True,
            "categories": sorted(config.categories),
            "configured": bool(config.categories),
            "diff": change.diff(),
        }
    )


# --- what counts as earnings. SPEC §112 --------------------------------------


@api.get("/earnings")
@A.login_required
def earnings():
    """Which categories money arrives under, and which of them count as earned.

    > "Earning definition or any other definition is different for anyone so we
    >  cant generalize instead give an option i guess"

    `not_earnings` is an allow-list — `earnings_only` names what counts and
    everything else arriving is tagged `not-earnings`. That is safe against
    over-counting and it means a freshly registered account reports **£0
    earned** against real deposits, because nobody has told the list about its
    payees yet. Measured on one: six deposits arrived and all six were excluded.

    So the list is offered rather than assumed. Only categories that have
    actually received money in this window are listed, because a category that
    has never taken a deposit is not a decision anybody needs to make.
    """
    scope, selected = _account_scope()
    start, end, window = _date_scope()
    counted = set(earnings_categories())

    arriving: dict[str, dict] = {}
    rules = load_rules()
    for txn in _within(_all_transactions(scope), start, end):
        if not txn.credit:
            continue
        name = txn.payee_alias or txn.payee or "(unparsed)"
        category = service.predict_category(f"{name} ({txn.channel})", txn.narration, rules)
        row = arriving.setdefault(
            category or "(no category)",
            {"category": category or "(no category)", "amount": Decimal(0), "count": 0},
        )
        row["amount"] += txn.credit
        row["count"] += 1

    rows = sorted(arriving.values(), key=lambda r: -r["amount"])
    return jsonify(
        {
            "rows": [
                {
                    "category": r["category"],
                    "amount": _money(r["amount"]),
                    "count": r["count"],
                    "counts": r["category"] in counted,
                }
                for r in rows
            ],
            "window": window,
            "selected": selected,
        }
    )


@api.put("/earnings")
@A.login_required
def set_earnings():
    """Add or remove one category from the earnings allow-list. §112."""
    body = request.get_json(silent=True) or {}
    category = str(body.get("category") or "").strip()
    if not category or category == "(no category)":
        return _fail(
            "Give the money a category first — an uncategorised deposit has "
            "nothing to count as.",
            "invalid",
            422,
        )
    change = plan_earnings(category, bool(body.get("counts")))
    if change.changed:
        change.apply()
    return jsonify({"ok": True, "counted": earnings_categories(), "diff": change.diff()})


@api.get("/payees")
@A.login_required
def payees():
    scope, selected = _account_scope()
    start, end, window = _date_scope()
    everything = _all_transactions(scope)
    # Narrowed AFTER the per-account dedup, never before: dedup keys on the
    # bank's id and a window that cut a duplicate's first appearance would let
    # the second one through as if it were a new row.
    transactions = _within(everything, start, end)
    rows = service.payee_inventory(transactions)

    # Hour-of-day per row, for the Day Rail at aggregate scale. This is the
    # analysis that split Day Canteen from Night Canteen by hand in Phase 4;
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
            "window": window,
            # What the window is hiding. A filtered list that does not say it is
            # filtered is how a token gets decided twice — or never.
            "outsideWindow": len(everything) - len(transactions),
        }
    )


@api.get("/categories")
@A.login_required
def categories():
    """Only categories that already have a rule. D10: the UI never invents one."""
    return jsonify({"categories": known_categories()})


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


@api.post("/categories")
@A.login_required
def categories_add():
    """Create a category. SPEC §26.

    D10 forbids *inferring* a category from a truncated token. Typing one is not
    inferring — it records a decision the operator has already made, and without
    this the dropdown could only ever offer what a hand-edited YAML file already
    contained.

    Created empty: no payee is assigned here. That still goes through
    diff-then-write like every other categorisation.
    """
    name = str((request.get_json(silent=True) or {}).get("name") or "")
    try:
        change = plan_new_category(name)
    except ValueError as exc:
        return _fail(str(exc), "invalid", 422)

    change.apply()
    log.info("category created: %r", name.strip())
    return jsonify({"ok": True, "categories": known_categories()})


@api.get("/categories/removable")
@A.login_required
def categories_removable():
    """Only the categories that can actually be removed — the empty ones.

    Offering all of them and answering 409 for most is an error to read; a list
    of what is possible is a non-choice. `not_spend` and the large-oneoff
    exclusions are honoured here too, so the button matches the server.
    """
    rules = load_rules()
    protected = {str(c) for c in (rules.get("not_spend") or [])}
    protected |= {
        str(c) for c in ((rules.get("large_oneoff") or {}).get("exclude_categories") or [])
    }
    removable = sorted(
        str(spec["category"])
        for spec in (rules.get("rules") or [])
        if spec.get("category")
        and not (spec.get("payees") or [])
        and str(spec["category"]) not in protected
    )
    return jsonify({"removable": removable})


@api.delete("/categories/<path:name>")
@A.login_required
def categories_remove(name: str):
    """Remove an empty category. SPEC §32.

    Symmetry with creating one — a typo was otherwise permanent. Refuses a
    category that still has payees, because deleting the rule does not delete
    them, it strands them.
    """
    try:
        change = plan_remove_category(name)
    except KeyError as exc:
        return _fail(str(exc), "unknown_category", 404)
    except ValueError as exc:
        return _fail(str(exc), "in_use", 409)

    change.apply()
    log.info("category removed: %r", name)
    return jsonify({"ok": True, "categories": known_categories()})


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
    merged = _merged_aliases(current, alias_changes)

    existing = service.rule_categories()
    category_changes = {
        t: v
        for t, v in categories_in.items()
        if existing.get((merged.get(t) or t)) != v and (v or existing.get(merged.get(t) or t))
    }

    renames = _display_renames(current, alias_changes)
    try:
        changes = [
            plan_aliases(alias_changes),
            plan_categories(category_changes, merged, renames=renames),
        ]
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
            "ledger": _ledger_impact(merged, category_changes),
            # Categories this submission would leave with no payees at all.
            # They keep existing — in the dropdown, and in Firefly — and can
            # never match anything again, so a report on one is permanently
            # empty. That is what happened to Day Canteen (§33).
            "emptied": _emptied_categories(merged, category_changes),
        }
    )


def _emptied_categories(aliases: dict[str, str], category_changes: dict[str, str]) -> list[str]:
    """Categories that would be left with no payees by this submission.

    Computed against the prospective rules, the same overlay the ledger impact
    uses, so the warning is about what the button will do rather than about
    what the file says now.
    """
    if not category_changes:
        return []
    after = _rules_with(load_rules(), aliases, category_changes)
    before = {
        str(spec["category"])
        for spec in (load_rules().get("rules") or [])
        if spec.get("category") and (spec.get("payees") or [])
    }
    return sorted(
        str(spec["category"])
        for spec in (after.get("rules") or [])
        if spec.get("category")
        and not (spec.get("payees") or [])
        and not spec.get("notes_contains")
        and not spec.get("notes_starts")
        and str(spec["category"]) in before
    )


def _ledger_impact(aliases: dict[str, str], category_changes: dict[str, str]) -> dict | None:
    """What this *unwritten* config would do to the rows already in Firefly.

    The count used to be knowable only after the write, so the page asked the
    operator to approve a config diff and then told them the ledger consequence
    afterwards. It is the same question and it can be answered first: the
    preview takes the prospective aliases and rules as an overlay rather than
    reading the two files off disk.

    `None` when Firefly cannot be asked. That is not an error on this route —
    writing config is what the operator requested, and it works whether or not
    the ledger is reachable.
    """
    st = load_settings()
    if not st.firefly_token or not st.passbook_asset_account:
        return None

    rules = load_rules()
    if category_changes:
        # Mirror what `plan_categories` will write, so the preview reflects the
        # submission rather than the file it is about to replace.
        rules = _rules_with(rules, aliases, category_changes)

    try:
        with _client(st.firefly_url, st.firefly_token) as client:
            changes, considered = service.reapply_preview(
                client, st, current_app.config["ARCHIVE"], aliases=aliases, rules=rules
            )
    except FireflyError as exc:
        log.info("ledger impact unavailable: %s", exc)
        return None
    return _preview(changes, considered)


def _rules_with(rules: dict, aliases: dict[str, str], category_changes: dict[str, str]) -> dict:
    """`rules.yaml` as it will read once these category decisions are written.

    Rules match on the DISPLAY name — the alias where one exists, the raw token
    otherwise — because that is what `description` carries at push time. Same
    resolution `plan_categories` uses; if the two ever disagree the preview is
    lying, which is the whole failure this route exists to avoid.
    """
    import copy

    out = copy.deepcopy(rules)
    specs = out.get("rules") or []
    by_category = {spec.get("category"): spec for spec in specs if spec.get("category")}

    for token, raw in category_changes.items():
        category = (raw or "").strip()
        display = (aliases.get(token) or token).strip()

        for spec in specs:
            payees = spec.get("payees") or []
            if display in payees and spec.get("category") != category:
                spec["payees"] = [p for p in payees if p != display]

        spec = by_category.get(category)
        # No invented rule shape, exactly as `plan_categories` refuses to invent
        # one (D10). An unknown category has already been rejected upstream with
        # a 422; if one reached here, the honest preview is to ignore it rather
        # than to show a consequence the write will refuse.
        if not category or spec is None:
            continue
        if display not in (spec.get("payees") or []):
            spec.setdefault("payees", []).append(display)
    return out


@api.post("/payees/apply")
@A.login_required
def payees_apply():
    body = request.get_json(silent=True) or {}
    aliases_in, categories_in = _split_submission(body)
    # Both read the map as it stands NOW — before `plan_aliases` rewrites it.
    current = load_payee_aliases()
    merged = _merged_aliases(current, aliases_in)
    renames = _display_renames(current, aliases_in)
    # The two remedies the confirm screen offers for §33's failures. Both are
    # opt-in and both are applied in the SAME plan as the categorisation, so the
    # tag can never be lost in the window between two writes.
    keep_tags = {str(k): str(v) for k, v in (body.get("keepTags") or {}).items()}
    remove_emptied = [str(name) for name in (body.get("removeEmptied") or [])]

    try:
        plan_aliases(aliases_in).apply()
        # Renames first, in the same plan: a rule matches the display name, so
        # relabelling a payee without following it through `rules.yaml` silently
        # de-categorises the payee (§23.4).
        plan_categories(categories_in, merged, renames=renames, tags=keep_tags).apply()
        for name in remove_emptied:
            # After the categorisation, or the category would still hold the
            # payees that are about to move out of it and the removal would
            # refuse — correctly, and confusingly.
            try:
                plan_remove_category(name).apply()
            except (KeyError, ValueError) as exc:
                log.warning("could not remove emptied category %r: %s", name, exc)
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
                # at all. Rules first, then the rows — same order as §15.2, and
                # for the same reason.
                if st.passbook_asset_account:
                    synced = _sync_now(client, st)
                    summary += _synced_summary(synced)
        except FireflyError as exc:
            summary = f"Config written, but the ledger store did not answer: {exc}"
    return jsonify({"ok": True, "summary": summary, "synced": synced})


