"""What happened to this ledger, and when.

**The gap this closes is config, not money.** Every statement is in `archive/`
and `verify-ledger` compares the ledger against it, so the rows already have a
paper trail — that is the whole §20 apparatus. What had no trail at all was the
half that changes what the ledger *says*: renaming a payee, moving a category,
deleting one, removing an account. `config/` is gitignored because it names
real counterparties, so there is no history of it anywhere, and a week later
"why does this read differently?" had no answer but memory.

**It is a record, never a source.** Nothing reads this back to compute a
figure. `service.ledger_analysis` remains the only thing that says what was
spent and `verify-ledger` the only thing that says whether the rows are right —
an audit log that started answering those questions would be a second copy of
the truth, which is the mistake this project keeps not making.

**Writing it can never fail the thing it records.** A backup that worked and an
audit row that did not is a backup that worked. Every write here is wrapped:
the failure is logged and swallowed, deliberately and in one place.
"""

from __future__ import annotations

import logging
from contextlib import suppress

log = logging.getLogger(__name__)

#: The vocabulary. A closed set so the page can filter on it, and so a typo
#: shows up as an unknown action rather than as a category of one.
ACTIONS = (
    "import",      # a statement was written into the ledger
    "rename",      # a payee's display name changed
    "categorise",  # a category was assigned, created or removed
    "resync",      # config was applied to rows already stored
    "purge",       # rows were deleted
    "account",     # an account was registered, renamed or removed
    "backup",      # a dump was taken
    "upgrade",     # a migration ran
    "rebuild",     # the ledger was rebuilt from archive/
)


def record(store, action: str, summary: str, *, affected: int | None = None, **detail) -> None:
    """Note that something happened. Never raises, never blocks the caller.

    `store` may be None — a code path that has no ledger open is a code path
    that writes no audit row, rather than one that opens a connection to write
    one.
    """
    if store is None:
        return
    if action not in ACTIONS:
        # Loud in the log, silent to the caller: an unknown action is a bug in
        # this repository, not a reason to fail an import.
        log.warning("unknown audit action %r (%s)", action, summary)
    with suppress(Exception):
        store.record_event(action, summary, detail or {}, affected)
        return
    log.debug("could not record %s: %s", action, summary, exc_info=True)


def history(store, limit: int = 200, action: str | None = None) -> list[dict]:
    """The most recent entries, newest first. Never raises."""
    if store is None:
        return []
    try:
        return store.events(limit=limit, action=action)
    except Exception:  # noqa: BLE001 — a page that cannot show history still renders
        log.debug("could not read the audit log", exc_info=True)
        return []
