"""The blueprint, the constants, and the serialisers every module shares.

**A package attribute is not a patch seam.** A route resolves a name in its
own module's globals, so a test that wants a fake store client patches
`_base.FireflyClient` — the one place a client is ever constructed, which is
why there is exactly one name to know.
"""

import logging
from decimal import Decimal
from flask import Blueprint, g, has_app_context, jsonify
from contextlib import contextmanager
# Re-exported so a caller outside this package reaches them by name without
# knowing which module they live in.
from ... import ops, service, webauth  # noqa: F401
from ...firefly.client import (  # noqa: F401
    FireflyClient,
    FireflyError,
    ValidationFailed,
)


log = logging.getLogger(__name__)

api = Blueprint("api", __name__, url_prefix="/api")

api = Blueprint("api", __name__, url_prefix="/api")

# A Canara three-month export is ~30 KB. Ten megabytes is already absurd, and
# refusing early keeps a mistake from becoming a disk problem.
#: How long an account's own name may be. Long enough for "Canara joint —
#: household", short enough that the switcher stays a strip rather than a wall.
ACCOUNT_LABEL_MAX = 40

MAX_UPLOAD_BYTES = 10 * 1024 * 1024

#: How many banded rows the "try it" preview hands back. Enough to see the
#: header, a sentinel and several transactions; not the whole statement.
BANDED_PREVIEW = 14

#: How many lines the shareable shape dump covers. Enough to reach the
#: transaction header past a details table, which is what needs looking at.
SHAPE_LINES = 40

# §6.2 dispatches on magic bytes, never on the extension.
ACCEPTED_SNIFF = {"xls", "xlsx", "html_table", "delimited", "pdf"}

MIN_PASSWORD_LENGTH = 12


# --- one store client per request. SPEC §101 ---------------------------------


@contextmanager
def _client(url: str, token: str):
    """The ledger store client for THIS request, shared by everything in it.

    **Measured.** A page load asks three endpoints at once and each built its
    own client; `/status` built two by itself, one for the version and one
    inside the integrity check. Opening a client costs ~90ms and
    `asset_accounts()` — memoised *for the life of a client* — costs ~230ms, so
    every extra client paid both again for an answer it already had. On this
    machine, against two registered accounts:

        /api/overview      398ms   1 client   1 round trip
        /api/analysis      887ms   1 client   2 round trips
        /api/status       1034ms   2 clients  3 round trips

    Sharing per request is not a cache with a timeout on it — there is nothing
    to go stale, because a request cannot outlive itself. It is the same
    lifetime `asset_accounts` already documents; it just was not being kept.

    Yields rather than returns so the call sites keep their `with` shape. The
    client is closed by the app-context teardown, not here: closing it at the
    end of the first block would give the second block a dead connection pool.

    Outside an app context — a script importing this module — there is no `g`
    to hang it on, so one is built and closed the old way.
    """
    if not has_app_context():
        with FireflyClient(url, token) as client:
            yield client
        return

    cache = getattr(g, "_store_clients", None)
    if cache is None:
        cache = g._store_clients = {}
    key = (url, token)
    if key not in cache:
        cache[key] = FireflyClient(url, token)
    yield cache[key]


# --- serialisation --------------------------------------------------------


def _money(value) -> str | None:
    """Decimal -> exact decimal string. Never a float, never a JSON number."""
    if value is None:
        return None
    return f"{Decimal(value):.2f}"


def _txn(t) -> dict:
    return {
        "id": t.txn_id,
        "date": t.txn_date.isoformat(),
        # The Day Rail's whole input. None for the rows whose narration carries
        # no clock — rendered as an explicit absence, never as midnight.
        "time": t.txn_time.strftime("%H:%M:%S") if t.txn_time else None,
        "channel": t.channel,
        "payee": t.payee,
        "alias": t.payee_alias,
        "display": t.payee_alias or t.payee,
        "debit": _money(t.debit),
        "credit": _money(t.credit),
        "balance": _money(t.balance),
        "reversal": t.is_reversal,
    }


def _parsed(parsed, *, filename: str | None = None) -> dict:
    """A statement, ready to render.

    Rows are emitted in sheet order and complete — the Preview table shows a
    Balance column, and a Balance column over a filtered or reordered subset
    asserts a continuity that is not there. §6.6 is the spine of this project;
    a view that appears to break it teaches the operator to distrust the check.
    """
    meta = parsed.meta
    return {
        "filename": filename or parsed.path.name,
        "meta": {
            # masked_account is last-4 only. The full number never leaves here.
            "account": meta.masked_account,
            "periodFrom": meta.period_from.isoformat(),
            "periodTo": meta.period_to.isoformat(),
            "openingBalance": _money(meta.opening_balance),
            "closingBalance": _money(meta.closing_balance),
        },
        "count": len(parsed.transactions),
        "withdrawn": _money(parsed.debits),
        "deposited": _money(parsed.credits),
        "warnings": parsed.warnings,
        "transactions": [_txn(t) for t in parsed.transactions],
    }


def _payee_row(r) -> dict:
    return {
        "token": r.token,
        "alias": r.alias,
        "category": r.category,
        "channel": r.channel,
        "count": r.count,
        "withdrawn": _money(r.withdrawn),
        "deposited": _money(r.deposited),
        "total": _money(r.total),
        "first": r.first,
        "last": r.last,
        "needsDecision": r.needs_decision,
    }


def _artefact(a) -> dict:
    return {
        "name": a.name,
        "size": a.size,
        "humanSize": a.human_size,
        "modified": a.modified,
        "ageDays": a.age_days,
    }


def _sync(status) -> dict:
    return {
        "state": status.state,
        "age": status.age,
        "filename": status.filename,
        "headline": status.headline,
        "detail": status.detail,
    }


def _fail(message: str, code: str = "error", status: int = 400):
    return jsonify({"error": message, "code": code}), status


