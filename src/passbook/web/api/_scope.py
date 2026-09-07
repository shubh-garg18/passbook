"""Which rows a request is about: the account scope and the date window."""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from flask import request
from ...config import load_accounts


# --- account scope. SPEC §21.9 -----------------------------------------------
# Every read endpoint takes `?account=<slug>` or `?account=all`. The default is
# the first registered account, so a single-account install behaves exactly as it
# did before this phase and never sees a switcher (§21.3).

ALL_ACCOUNTS = "all"


def _account_scope(default_to_first: bool = True):
    """Return `(accounts_in_scope, selected)` for this request.

    `selected` is a slug, `"all"`, or None when nothing is registered. A slug the
    registry does not know falls back to the first account rather than 404ing: a
    stale selection in someone's browser must not break the page it is stored for.
    """
    registry = load_accounts()
    wanted = (request.args.get("account") or "").strip()
    if not registry:
        return [], None
    if wanted == ALL_ACCOUNTS and len(registry) > 1:
        return registry, ALL_ACCOUNTS
    chosen = next((a for a in registry if a.slug == wanted), None)
    if chosen is None:
        chosen = registry[0] if default_to_first else None
    return ([chosen] if chosen else []), (chosen.slug if chosen else None)


# -- the window --------------------------------------------------------------
# SPEC §26. Every figure on every page answers "over what period", and until
# this existed the answer was always "everything ever archived" — which is the
# one window nobody asks about. A named range or an explicit from/to, resolved
# server-side and echoed back, so the client renders the scope the server used
# rather than the one it asked for.

RANGES = ("month", "last-month", "3m", "6m", "year", "all")
DEFAULT_RANGE = "all"

def _range_bounds(name: str, today: date) -> tuple[date | None, date | None]:
    """`(from, to)` inclusive, or `(None, None)` for everything."""
    first = today.replace(day=1)
    if name == "month":
        return first, today
    if name == "last-month":
        end = first - timedelta(days=1)
        return end.replace(day=1), end
    if name == "3m":
        return _months_back(first, 2), today
    if name == "6m":
        return _months_back(first, 5), today
    if name == "year":
        return today.replace(month=1, day=1), today
    return None, None

def _months_back(first_of_month: date, months: int) -> date:
    """N whole months before this one, staying on the 1st.

    Arithmetic on the 1st only, so there is no end-of-month case to get wrong —
    the bug that would otherwise show up once a year, in March.
    """
    month = first_of_month.month - months
    year = first_of_month.year
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)

def _date_scope() -> tuple[date | None, date | None, dict]:
    """The requested window, plus what to echo back to the client.

    An explicit `from`/`to` beats a named range, so a custom window survives a
    reload. An unparseable date is ignored rather than 400ing: a hand-edited URL
    should show the ledger, not an error page.
    """
    name = (request.args.get("range") or "").strip().lower()
    raw_from = (request.args.get("from") or "").strip()
    raw_to = (request.args.get("to") or "").strip()

    start = _as_date(raw_from)
    end = _as_date(raw_to)
    if start or end:
        # Swapped by hand or by a date picker that allows it. Ordering them is
        # kinder than refusing, and an empty result would look like no data.
        if start and end and start > end:
            start, end = end, start
        return start, end, {"range": "custom", "from": _iso(start), "to": _iso(end)}

    if name not in RANGES:
        name = DEFAULT_RANGE
    start, end = _range_bounds(name, date.today())
    return start, end, {"range": name, "from": _iso(start), "to": _iso(end)}

def _as_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None

def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None

def _splits_within(splits: list[dict], start: date | None, end: date | None) -> list[dict]:
    """The same window, over Firefly splits rather than parsed transactions.

    A split's `date` is an ISO-8601 *datetime* with an offset
    (`2026-08-24T00:00:00+05:30`), so it is cut at the `T` rather than parsed:
    the window is a range of calendar days in `Asia/Kolkata`, and converting to
    a datetime only to drop the time again invites a timezone shift that would
    silently move a midnight transaction into the previous day.
    """
    if start is None and end is None:
        return splits
    out = []
    for split in splits:
        day = _as_date(str(split.get("date") or "")[:10])
        if day is None:
            continue
        if (start is None or day >= start) and (end is None or day <= end):
            out.append(split)
    return out

def _within(transactions, start: date | None, end: date | None):
    if start is None and end is None:
        return list(transactions)
    return [
        t
        for t in transactions
        if (start is None or t.txn_date >= start) and (end is None or t.txn_date <= end)
    ]

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


def _account_summary(account, selected: str | None) -> dict:
    return {
        "slug": account.slug,
        "bank": account.bank,
        "account": account.masked,
        "assetAccount": account.asset_account,
        "label": account.display,
        "selected": account.slug == selected,
    }


