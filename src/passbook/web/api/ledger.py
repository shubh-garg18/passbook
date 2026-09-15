"""The figures: overview, analysis, and the transaction browser."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path


from flask import current_app, jsonify, request, session

from ... import service
from ...config import (
    load_attribution,
)
from ...store import LedgerError
from .. import auth as A

from ._base import (
    _ledger,
    _fail,
    _money,
    _sync,
    api,
    log,
)
from ._scope import (
    _account_scope,
    _as_date,
    _date_scope,
    _splits_within,
)


@api.get("/overview")
@A.login_required
def overview():
    """Balance, sync age and recent statements, scoped to the selected account.

    **"All accounts" sums the balances and shows the parts** (§21.9). The sum is a
    true figure — it is what those accounts hold together — but unlike a
    single-account balance it cannot be reconciled against any one statement's
    closing figure, which is what this card has implied since Phase 7. So it is
    labelled as a sum and the per-account figures travel with it.
    """
    scope, selected = _account_scope()
    error = None
    parts: list[dict] = []
    total: Decimal | None = None

    try:
        with _ledger() as store:
            live = {
                a["name"]: Decimal(str(a["current_balance"]))
                for a in store.asset_accounts()
            }
        for account in scope:
            amount = live.get(account.asset_account)
            parts.append(
                {
                    "slug": account.slug,
                    "label": account.display,
                    "account": account.masked,
                    "balance": _money(amount),
                }
            )
            if amount is not None:
                total = (total or Decimal(0)) + amount
    except LedgerError as exc:
        error = str(exc)

    return jsonify(
        {
            "balance": _money(total),
            "ledgerError": error,
            "account": scope[0].display if len(scope) == 1 else None,
            "selected": selected,
            # Only meaningful for "all"; the client shows the breakdown then.
            "parts": parts if len(parts) > 1 else [],
            # Staleness aggregates to the WORST, not the average: a warning must
            # not be diluted by a fresher account.
            # Both scoped to the accounts on screen. §104 — a Canara download
            # warning on the Union tab is a warning about somebody else's bank.
            "sync": _sync(service.sync_status(scope, current_app.config["ARCHIVE"])),
            "history": service.sync_history(current_app.config["ARCHIVE"], accounts=scope),
            # **The file, not the session key.** SPEC §100.
            #
            # `pending` is a path held in the session, and the file it names
            # lives in a temporary directory — a container restart, a tmp sweep
            # or a discarded upload takes the file and leaves the key. The
            # banner then showed on every load, for the life of the session,
            # linking to a Preview that answered "Nothing pending — upload a
            # statement first". `/statement/pending` has always checked the
            # file exists; this one did not, so the two disagreed about whether
            # there was anything to push.
            #
            # Cleared rather than merely hidden: a key naming a file that is
            # gone is not state worth keeping, and leaving it would make every
            # later read pay the same stat call to reach the same answer.
            "pending": _has_pending(scope),
        }
    )


def _has_pending(scope=None) -> bool:
    """Whether there is a staged statement that still exists, for this scope.

    `scope` is the accounts on screen. A staged statement belongs to exactly one
    of them — it was routed by what the file says, at staging time (§21.9) — so
    the banner shows on that account's page and nowhere else (§104). A statement
    staged before any account was registered belongs to none of them and shows
    on all, which is right: it is the thing standing between the operator and a
    registered account.
    """
    pending = session.get("pending")
    if not pending:
        return False
    if Path(pending).exists():
        slug = session.get("pending_slug")
        if slug and scope is not None:
            return any(account.slug == slug for account in scope)
        return True
    # The same three keys `discard_pending` drops, for the same reason (§30): a
    # password left behind is a credential kept for a file that no longer
    # exists, and a stale `pending_encrypted` offers a retry for nothing.
    for key in ("pending", "pending_password", "pending_encrypted", "pending_slug"):
        session.pop(key, None)
    log.info("staged statement is gone from disk; cleared the session keys")
    return False


def _slice(s) -> dict:
    return {"name": s.name, "amount": _money(s.amount), "count": s.count}


def _breakdown(b) -> dict:
    return {
        "name": b.name,
        "amount": _money(b.amount),
        "count": b.count,
        "parts": [_slice(p) for p in b.parts],
    }


@api.get("/analysis")
@A.login_required
def analysis():
    """The Ledger page's charts. SPEC §18.

    Separate from `/overview` on purpose: this reads every transaction on the
    account and parses the archive, so folding it in would make the balance and
    the sync age — the two things the operator opens the page for — wait behind
    it.

    **Two sources, each authoritative for what it carries.** Money and category
    come from the ledger, because the rules engine assigns the category at store
    time (D5) and re-deriving it here would be a second implementation. The
    clock comes from the statement, because `txn_time` is parsed out of the
    narration (§6.5) and never pushed — the ledger has no idea what time of day
    anything happened.
    """
    scope, selected = _account_scope()
    start, end, window = _date_scope()
    if not scope:
        return _fail("No account is registered.", "unconfigured", 503)

    # **Everything on this page is additive over transactions, so "all accounts"
    # combines** (§21.9): spend, income, the category breakdown, the roll-ups, the
    # month buckets and the Day Rail. Each is a sum over rows, and the exclusion
    # semantics (§8/§8.1) are per row, so combining cannot change what any figure
    # means. Time of day is a property of the person, not the account, which makes
    # the combined Day Rail the more useful of the two readings.
    #
    # The clock map is still built PER ACCOUNT and merged by external_id, never by
    # the bank's id: those collide between accounts (§21.1), and joining them
    # naively would attach one account's clock to the other's transaction.
    splits: list[dict] = []
    times: dict[str, object] = {}
    coverages = []
    # One line per account, never a sum. Summing balances across accounts needs
    # a last-known figure carried forward for every account on every date, and
    # before the earliest account starts that "total" is one account wearing the
    # word total. Separate lines say the same thing without the caveat (§57).
    balances: list[dict] = []
    try:
        with _ledger() as store:
            live = {a["name"] for a in store.asset_accounts()}
            archive = current_app.config["ARCHIVE"]
            for account in scope:
                if account.asset_account not in live:
                    return _fail(
                        f"No asset account named {account.asset_account!r}.",
                        "unconfigured",
                        503,
                    )
                splits.extend(store.account_transactions(account.asset_account))
                # §114.2. Out of the index. Everything below needs the account's
                # deduped rows and the period its statements cover, and both are
                # a query now rather than a re-read of the whole archive.
                mine = service.archived_transactions([account], archive)
                for txn in mine:
                    times[account.external_id(txn.txn_id)] = txn.txn_time
                    # Tolerated for a pre-migration ledger, where the pushed id is
                    # the bank's bare one.
                    times.setdefault(txn.txn_id, txn.txn_time)
                span = service.archived_coverage([account], archive)
                if span:
                    coverages.append(span)

                points, opening = service.balance_series(mine, start=start, end=end)
                if points or opening:
                    balances.append(
                        {
                            "slug": account.slug,
                            "label": account.display,
                            "points": [
                                {"day": p.day, "balance": _money(p.balance)} for p in points
                            ],
                            "opening": (
                                {"day": opening.day, "balance": _money(opening.balance)}
                                if opening
                                else None
                            ),
                        }
                    )
    except LedgerError as exc:
        return _fail(f"The ledger store did not answer: {exc}", "ledger", 502)

    total_rows = len(splits)
    splits = _splits_within(splits, start, end)

    coverage = (
        (min(c[0] for c in coverages), max(c[1] for c in coverages)) if coverages else None
    )
    # Clipped to the window as well, or every month in a narrowed view is
    # reported as partial — the coverage is what decides that flag (§18), and
    # an uncut coverage says the ledger spans months the chart is not showing.
    if coverage and (start or end):
        coverage = (max(coverage[0], start or coverage[0]), min(coverage[1], end or coverage[1]))
        if coverage[0] > coverage[1]:
            coverage = None

    # §73. Read per request rather than cached: it is a small YAML file and a
    # stale attribution would move money between months invisibly.
    result = service.ledger_analysis(
        splits, times=times, coverage=coverage, attribution=load_attribution()
    )

    return jsonify(
        {
            "spend": _money(result.spend),
            "grossSpend": _money(result.gross_spend),
            "income": _money(result.income),
            "grossIncome": _money(result.gross_income),
            "net": _money(result.net),
            "withdrawals": result.withdrawals,
            "deposits": result.deposits,
            "categories": [_slice(s) for s in result.categories],
            "payees": [_slice(s) for s in result.payees],
            "sources": [_slice(s) for s in result.sources],
            # §64. the ledger's Category, Double and Tag reports — three screens
            # there, one shape here, and all three carry §8/§8.1 because they
            # are computed inside `ledger_analysis` rather than beside it.
            "payeesByCategory": [_breakdown(b) for b in result.payees_by_category],
            "categoriesByPayee": [_breakdown(b) for b in result.categories_by_payee],
            "categoriesByTag": [_breakdown(b) for b in result.categories_by_tag],
            "sourcesByCategory": [_breakdown(b) for b in result.sources_by_category],
            "spread": [
                {
                    "name": x.name, "count": x.count, "low": _money(x.low),
                    "q1": _money(x.q1), "median": _money(x.median),
                    "q3": _money(x.q3), "high": _money(x.high),
                }
                for x in result.spread
            ],
            "categoriesBySource": [_breakdown(b) for b in result.categories_by_source],
            "excludedSpend": [_slice(s) for s in result.excluded_spend],
            # Sent as a total rather than left to the client to subtract: money
            # crosses this boundary as a decimal string and must not be put
            # through a float on the way to being displayed (§16.1).
            "excludedSpendTotal": _money(
                sum((s.amount for s in result.excluded_spend), Decimal(0))
            ),
            "excludedIncome": _slice(result.excluded_income),
            "refunds": _slice(result.refunds),
            "rollups": [
                {
                    "tag": r.tag,
                    "amount": _money(r.amount),
                    "count": r.count,
                    "parts": [_slice(p) for p in r.parts],
                }
                for r in result.rollups
            ],
            "months": [
                {
                    "month": m.month,
                    "spend": _money(m.spend),
                    "income": _money(m.income),
                    "partial": m.partial,
                }
                for m in result.months
            ],
            # Aligned with `months` by position and zero-padded, so the client
            # never has to guess which month an amount belongs to (§57).
            "categoryMonths": [
                {"name": c.name, "amounts": [_money(a) for a in c.amounts], "total": _money(c.total)}
                for c in result.category_months
            ],
            "balances": balances,
            "hours": result.hours,
            "weekdays": result.weekdays,
            "weekdaySpend": [_money(v) for v in result.weekday_spend],
            "clocked": result.clocked,
            "counted": result.counted,
            "uncategorised": _slice(result.uncategorised),
            "notSpend": result.not_spend,
            "window": window,
            "outsideWindow": total_rows - len(splits),
            "selected": selected,
            "accounts": [a.slug for a in scope],
            "coverage": (
                {"from": coverage[0].isoformat(), "to": coverage[1].isoformat()}
                if coverage
                else None
            ),
        }
    )


@api.get("/transactions")
@A.login_required
def transactions():
    """Every row in the ledger, searchable. SPEC §61.

    The page passbook never had, and the last routine reason to open the ledger.
    Elsewhere this is called an audit report; here it is just the list.

    **Two sources, each authoritative for what it carries** — the same split as
    `/analysis`. Money, category and tags come from the ledger, because the rules
    engine assigns the category at store time (D5). The clock and the raw
    narration come from the statement, because `txn_time` is parsed out of the
    narration (§6.5) and never pushed.

    **There is deliberately no running balance.** §16.4 refuses one on any view
    that can be filtered or reordered, and this view is nothing but filtering
    and reordering: a Balance column over a search result asserts a continuity
    that is not there, and §6.6 is the spine of this project. The statement
    sheet keeps its balance column; this does not get one.
    """
    scope, selected = _account_scope()
    start, end, window = _date_scope()
    if not scope:
        return _fail("No account is registered.", "unconfigured", 503)

    query = (request.args.get("q") or "").strip().lower()
    want_category = (request.args.get("category") or "").strip()
    want_tag = (request.args.get("tag") or "").strip()
    direction = (request.args.get("direction") or "").strip().lower()
    # §83. The filters a fintech analyst actually reaches for, and the two the
    # page could not express: a size band and a sort. Both are applied AFTER
    # the text search, so `matched` is what the caption reports either way.
    want_min = _as_amount(request.args.get("min"))
    want_max = _as_amount(request.args.get("max"))
    sort = (request.args.get("sort") or "date").strip().lower()
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    limit = 100

    rows: list[dict] = []
    try:
        with _ledger() as store:
            live = {a["name"] for a in store.asset_accounts()}
            archive = current_app.config["ARCHIVE"]
            for account in scope:
                if account.asset_account not in live:
                    return _fail(
                        f"No asset account named {account.asset_account!r}.",
                        "unconfigured",
                        503,
                    )
                mine = service.archived_transactions([account], archive)
                # Keyed on the namespaced external_id, never the bare txn_id:
                # two accounts at one bank emit identical ids (non-negotiable
                # 10), and this loop is building ONE list across accounts.
                clocks: dict[str, object] = {}
                narrations: dict[str, str] = {}
                for txn in mine:
                    key = account.external_id(txn.txn_id)
                    clocks[key] = txn.txn_time
                    narrations[key] = txn.narration
                    # **Both forms, exactly as `/analysis` does it.** The map
                    # was keyed on the namespaced id only and the fallback
                    # below stripped the namespace off the LOOKUP — probing a
                    # namespaced dict with a bare key, which misses by
                    # construction. That is §23.1's join bug turned around: a
                    # pre-migration row whose the ledger external_id is the bare
                    # `20260509000001` silently lost its time of day. Safe
                    # because `mine` is one account's statements, so the bare
                    # id is unambiguous here (§21.1).
                    clocks.setdefault(txn.txn_id, txn.txn_time)
                    narrations.setdefault(txn.txn_id, txn.narration)

                for split in store.account_transactions(account.asset_account):
                    # Withdrawals and deposits, which is everything the table
                    # can hold — `kind` has no third value. The previous store
                    # also returned the opening balance as a row, and it
                    # rendered with no payee and no Out or In value, making the
                    # page count one more than `verify-ledger` did. The opening
                    # balance is a column on the account here.
                    external = str(split.get("external_id") or "")
                    moment = clocks.get(external)
                    if moment is None and external:
                        moment = clocks.get(service.txn_id_of(external))
                    narration = narrations.get(external) or narrations.get(
                        service.txn_id_of(external), ""
                    )
                    day = split.get("txn_date")
                    rows.append(
                        {
                            "id": external,
                            "account": account.slug,
                            "accountLabel": account.display,
                            "date": day.isoformat() if day else "",
                            "time": moment.isoformat() if moment else None,
                            "description": str(split.get("description") or ""),
                            "category": str(split.get("category") or ""),
                            "counterparty": service._counterparty(split),
                            "tags": sorted(str(t) for t in (split.get("tags") or [])),
                            "kind": str(split.get("kind") or ""),
                            "amount": _money(service._split_amount(split)),
                            "narration": narration,
                        }
                    )
    except LedgerError as exc:
        return _fail(f"The ledger store did not answer: {exc}", "ledger", 502)

    total = len(rows)
    rows = [r for r in rows if _row_in_window(r, start, end)]
    outside = total - len(rows)

    if direction in ("in", "out"):
        wanted = "deposit" if direction == "in" else "withdrawal"
        rows = [r for r in rows if r["kind"] == wanted]
    if want_category:
        # `(no category)` is how the analysis names the empty one, so the same
        # string has to reach a row whose category really is empty.
        rows = [
            r
            for r in rows
            if (r["category"] or "(no category)") == want_category
        ]
    if want_tag:
        rows = [r for r in rows if want_tag in r["tags"]]
    if want_min is not None:
        rows = [r for r in rows if Decimal(r["amount"]) >= want_min]
    if want_max is not None:
        rows = [r for r in rows if Decimal(r["amount"]) <= want_max]
    if query:
        rows = [r for r in rows if _row_matches(r, query)]

    matched = len(rows)
    # Newest first by default. A ledger browser is opened to see what just
    # happened, which is the opposite of the statement sheet's order — and the
    # reason reordering is allowed here at all is that this view carries no
    # running balance to invalidate (§16.4).
    #
    # Amount sorts compare Decimals, never the display strings: `"9.00"` sorts
    # above `"10000.00"` lexically, which is the kind of wrong that looks fine.
    if sort == "amount":
        rows.sort(key=lambda r: (Decimal(r["amount"]), r["date"]), reverse=True)
    elif sort == "amount-asc":
        rows.sort(key=lambda r: (Decimal(r["amount"]), r["date"]))
    elif sort == "oldest":
        rows.sort(key=lambda r: (r["date"], r["id"]))
    else:
        rows.sort(key=lambda r: (r["date"], r["id"]), reverse=True)
    start_at = (page - 1) * limit
    return jsonify(
        {
            "rows": rows[start_at : start_at + limit],
            "matched": matched,
            "total": total,
            "outsideWindow": outside,
            "page": page,
            "pages": max(1, (matched + limit - 1) // limit),
            "window": window,
            "selected": selected,
            "accounts": [a.slug for a in scope],
            "sort": sort,
            # Every tag present in scope, so the tag filter can be a real
            # dropdown rather than something you have to already know.
            "tags": sorted({t for r in rows for t in r["tags"]}),
        }
    )


def _as_amount(raw: str | None) -> "Decimal | None":
    """A money bound from the query string, or None. Decimal, never float."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        # A hand-edited URL should show the ledger, not an error page — the
        # same call `_as_date` makes for a malformed window.
        return None


def _row_in_window(row: dict, start: date | None, end: date | None) -> bool:
    if start is None and end is None:
        return True
    day = _as_date(row["date"])
    if day is None:
        return False
    return (start is None or day >= start) and (end is None or day <= end)


def _row_matches(row: dict, query: str) -> bool:
    """Free text over everything a person would type.

    Includes the RAW narration, which is not rendered in the table: searching
    for a UTR or a bank reference is exactly the case where the display name is
    no help, and it is the reason this page can replace the ledger's search.
    """
    haystack = (
        f"{row['description']} {row['category']} {row['counterparty']} "
        f"{row['narration']} {row['amount']} {' '.join(row['tags'])}"
    )
    return query in haystack.lower()
