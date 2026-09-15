"""passbook's own ledger. DECISIONS.md §36.

The ledger used to be a separate application reached over HTTP. That bought a
double-entry store, a rules engine and a restore story for free, and cost a
second container, a second login, a second set of field names to verify against
someone else's pinned tag, and a duplicate check that was looking at the wrong
thing.

**What this is not.** It is not a re-implementation of that application. passbook
never used most of it: fifteen methods across accounts, transactions and rules,
and the rules half was already mirrored here because §24 had to know what a row
*should* be in order to fix it. Making that mirror authoritative is a smaller
change than it sounds — the logic was already written, tested, and relied upon.

**Two stores, one shape.** `LedgerStore` is the interface every caller sees, and
there are two implementations: this one, over Postgres, and an in-memory one the
tests use. The tests-never-touch-the-network rule stands; the Postgres path gets
the same narrow, documented, auto-skipping integration test the stack already
has.

The method names are deliberately the ones the previous client used. Callers
were written against that vocabulary, it is a reasonable vocabulary, and a
rename would have put a diff on every call site for no gain.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

SCHEMA = Path(__file__).resolve().parent / "schema.sql"


class LedgerError(RuntimeError):
    """The ledger could not be read or written. Never swallowed.

    A read that failed and a read that returned nothing are different answers,
    and the second is the one that silently doubles an account: if a push cannot
    see what is already there, it must not post.
    """


@runtime_checkable
class LedgerStore(Protocol):
    """What passbook needs from a ledger: eight methods.

    The client it replaces exposed fifteen. The gap is not a feature that
    was dropped — it is the part passbook never called, plus the rules
    engine, which moves here as `predict_category`/`predict_tags` because
    those already had to know the answer in order to check it (§24).
    """

    def asset_accounts(self) -> list[dict]: ...
    def store_account(self, name: str, opening, on, currency: str) -> None: ...
    def account_transactions(
        self, account: str, start=None, end=None, *, limit: int | None = None
    ) -> list[dict]: ...
    def store_transaction(self, split: dict) -> None: ...
    def update_transaction(self, external_id: str, fields: dict) -> None: ...
    def delete_transaction(self, external_id: str) -> None: ...
    def search_transactions(
        self, accounts, *, start=None, end=None, kind: str | None = None,
        category: str | None = None, tag: str | None = None, minimum=None,
        maximum=None, query: str | None = None, order: str = "newest",
        limit: int = 100, offset: int = 0,
    ) -> tuple[list[dict], int]: ...
    def count_transactions(self, account: str) -> int: ...
    def identities(self, account: str) -> set[str]: ...
    def record_event(self, action: str, summary: str, detail: dict, affected) -> None: ...
    def events(self, limit: int = 200, action: str | None = None) -> list[dict]: ...
    def close(self) -> None: ...

    # A `with` block, because every call site already had one: the store it
    # replaces was a network client and closing it mattered. It still does.
    def __enter__(self) -> "LedgerStore": ...
    def __exit__(self, *exc) -> bool: ...


def open_ledger(settings=None, dsn: str | None = None) -> "LedgerStore":
    """The ledger this install writes to.

    One construction point, on purpose. The previous arrangement grew a second
    and then a third, each building its own client, and the cost was paid twice
    per request until they were collapsed back into one. It is also the seam
    tests patch: a name resolved in another module's globals cannot be reached
    by patching a package attribute, so there is exactly one place to reach.
    """
    from .postgres import PostgresLedger

    if dsn is None:
        if settings is None:
            from ..config import load_settings

            settings = load_settings()
        dsn = settings.ledger_dsn
    return PostgresLedger(dsn)
