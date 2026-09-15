"""Which accounts and which dates a request is about. SPEC §21.9."""

from __future__ import annotations

from datetime import date, timedelta


from flask import request

from ...config import (
    load_accounts,
)



# --- account scope. SPEC §21.9 -----------------------------------------------
# Every read endpoint takes `?account=<slug>` or `?account=all`. The default is
# the first registered account, so a single-account install behaves exactly as it
# did before this phase and never sees a switcher (§21.3).

ALL_ACCOUNTS = "all"

# Long enough for "Joint account — household", short enough that a tab strip of
# four does not wrap on a phone.
ACCOUNT_LABEL_MAX = 40

#: How many banded rows the "try it" preview hands back. Enough to see the
#: header, a sentinel and several transactions; not the whole statement.
BANDED_PREVIEW = 14

#: How many lines the shareable shape dump covers. Enough to reach the
#: transaction header past a details table, which is what needs looking at.
SHAPE_LINES = 40


def _account_scope(default_to_first: bool = True):
    """Return `(accounts_in_scope, selected)` for this request.

    `?account=` takes a slug, `all`, or **a comma-separated subset** —
    `canara-1111,hdfc-9012`. The subset is the case that needed adding: with
    four accounts, "these two together" is a real question and neither "one" nor
    "all" answers it. §21.9 already established that every figure on these pages
    is additive over transactions, so any subset combines the same way `all`
    does; the only figure that does not is the balance, which is summed and
    labelled as a sum with its parts.

    `selected` echoes what was actually applied, in registry order, so the client
    renders the scope the server used rather than the one it asked for.

    Unknown slugs are dropped rather than 404'd, and a request naming only
    unknown slugs falls back to the first account: a stale selection in
    someone's browser must not break the page it is stored for.
    """
    registry = load_accounts()
    wanted = (request.args.get("account") or "").strip()
    if not registry:
        return [], None
    if wanted == ALL_ACCOUNTS and len(registry) > 1:
        return registry, ALL_ACCOUNTS

    asked = {part.strip() for part in wanted.split(",") if part.strip()}
    # Registry order, not request order: two URLs naming the same accounts must
    # produce the same scope and the same cache key.
    chosen = [a for a in registry if a.slug in asked]
    if len(chosen) > 1:
        # Naming every account is `all` by another route; say so, so the label
        # and the switcher agree.
        if len(chosen) == len(registry):
            return registry, ALL_ACCOUNTS
        return chosen, ",".join(a.slug for a in chosen)
    if len(chosen) == 1:
        return chosen, chosen[0].slug
    fallback = registry[0] if default_to_first else None
    return ([fallback] if fallback else []), (fallback.slug if fallback else None)


# --- the date range -------------------------------------------------------
# SPEC §25. the ledger has one and the operator reads the two side by side; more
# to the point, a payee list is a per-period job. Deciding on the eleven tokens
# that appeared last month is a task; scrolling the same 59 every week looking
# for the new ones is not.

# Named ranges, resolved on the server so the page and the API cannot disagree
# about where a month starts.
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
    """The same window, over the ledger splits rather than parsed transactions.

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


def _account_summary(account, selected: str | None) -> dict:
    return {
        "slug": account.slug,
        "bank": account.bank,
        "bankName": account.bank_name,
        "account": account.masked,
        "assetAccount": account.asset_account,
        # What a person is shown: the operator's own name if they set one, else
        # `Canara ****1111`. §40.
        "label": account.display,
        # Whether that name is a decision or a default — the rename field needs
        # to show the current name without pre-filling one nobody chose.
        "renamed": bool(account.label),
        "selected": account.slug == selected,
    }
