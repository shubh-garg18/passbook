"""The ledger, in Postgres. DECISIONS.md §36.

The same database that was already in the stack, with passbook's own tables in
it rather than another application's. Nothing here reaches the network: it is a
TCP connection to a service on the compose network, which is what the previous
arrangement did too — one fewer hop, and one fewer set of field names defined by
somebody else's release cycle.

**Money is read back as `Decimal`.** `NUMERIC` maps to `Decimal` in psycopg, and
that is load-bearing rather than incidental: a driver that handed back floats
would put non-negotiable 1 at the mercy of a dependency.

**Failures raise.** A read that could not happen and a read that found nothing
are different answers, and conflating them is how an account gets doubled.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from . import SCHEMA, LedgerError

log = logging.getLogger(__name__)

#: Only what config owns. The rest comes from the statement and an update that
#: could reach it is an update that eventually does.
UPDATABLE = ("description", "category", "counterparty")

#: Seconds to wait for a connection before giving up. Short on purpose: this is
#: a database on the same machine, so a slow connect is a broken one, and the
#: caller has a 502 to render.
CONNECT_TIMEOUT = 5


class PostgresLedger:
    """passbook's ledger over psycopg 3."""

    def __init__(self, dsn: str) -> None:
        import psycopg

        try:
            # `search_path`: the schema script sets it too, but only for the
            # session that runs it. Setting it on the connection means a
            # reconnect cannot quietly start resolving `transactions` to
            # somebody else's table in `public` — an upgraded install has one
            # sitting there.
            #
            # `connect_timeout`: **measured, and not optional.** libpq's default
            # is no timeout at all, and a TCP connect to a port nothing is
            # listening on does not always come back refused — under WSL's
            # mirrored networking it simply hangs. A ledger that is down would
            # then hang every page instead of erroring on it, which is the
            # worse failure by a wide margin: an error names the problem and a
            # hang looks like slowness.
            self._conn = psycopg.connect(
                dsn,
                autocommit=True,
                options="-c search_path=passbook",
                connect_timeout=CONNECT_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 — every failure is the same answer
            raise LedgerError(f"could not connect to the ledger: {exc}") from exc
        self._migrate()

    def _migrate(self) -> None:
        """Apply the schema. `CREATE TABLE IF NOT EXISTS` throughout, so this is
        safe on every start and is how a fresh install gets its tables."""
        with self._conn.cursor() as cur:
            cur.execute(SCHEMA.read_text(encoding="utf-8"))

    # --- accounts ------------------------------------------------------------

    def asset_accounts(self) -> list[dict]:
        """Every asset account, with its balance derived from its rows.

        `current_balance` is computed here rather than stored, so it cannot
        drift from the transactions it is a sum of. A stored balance is a second
        copy of the truth, and the ledger already learned what a second copy of
        the truth costs.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT a.name, a.opening_balance, a.opening_on, a.currency,"
                "       a.opening_balance + COALESCE(SUM("
                "           CASE WHEN t.kind = 'deposit' THEN t.amount ELSE -t.amount END"
                "       ), 0)"
                "  FROM asset_accounts a"
                "  LEFT JOIN transactions t ON t.account = a.name"
                " GROUP BY a.name, a.opening_balance, a.opening_on, a.currency"
                " ORDER BY a.name"
            )
            return [
                {
                    "name": n,
                    "opening_balance": b,
                    "opening_on": o,
                    "currency": c,
                    "current_balance": live,
                }
                for n, b, o, c, live in cur.fetchall()
            ]

    def store_account(self, name: str, opening, on, currency: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO asset_accounts (name, opening_balance, opening_on, currency)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO NOTHING",
                (name, Decimal(str(opening)), on, currency),
            )

    # --- transactions --------------------------------------------------------

    def account_transactions(self, account: str) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT t.external_id, t.account, t.kind, t.txn_date, t.amount,"
                "       t.description, t.counterparty, t.category, t.notes,"
                "       t.txn_time, t.currency,"
                "       COALESCE(ARRAY_AGG(g.tag ORDER BY g.tag)"
                "                FILTER (WHERE g.tag IS NOT NULL), '{}')"
                "  FROM transactions t"
                "  LEFT JOIN transaction_tags g ON g.external_id = t.external_id"
                " WHERE t.account = %s"
                " GROUP BY t.external_id"
                " ORDER BY t.txn_date, t.external_id",
                (account,),
            )
            keys = ("external_id", "account", "kind", "txn_date", "amount", "description",
                    "counterparty", "category", "notes", "txn_time", "currency", "tags")
            return [dict(zip(keys, r)) for r in cur.fetchall()]

    def identities(self, account: str) -> set[str]:
        """What a push asks before it posts anything.

        Read straight from the table rather than derived from a listing, so it
        cannot be stale. If this raises, the push must not continue.
        """
        with self._conn.cursor() as cur:
            cur.execute("SELECT external_id FROM transactions WHERE account = %s", (account,))
            return {r[0] for r in cur.fetchall()}

    def store_transaction(self, split: dict) -> None:
        external_id = str(split.get("external_id") or "")
        if not external_id:
            raise LedgerError("a transaction with no external_id cannot be stored")
        amount = Decimal(str(split["amount"]))
        if amount < 0:
            raise LedgerError("amount is positive; direction belongs in `kind`")
        try:
            with self._conn.transaction(), self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO transactions (external_id, account, kind, txn_date,"
                    " amount, description, counterparty, category, notes, txn_time,"
                    " currency) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (external_id, split["account"], split["kind"], split["txn_date"],
                     amount, split.get("description", ""), split.get("counterparty", ""),
                     split.get("category", ""), split.get("notes", ""),
                     split.get("txn_time"), split.get("currency", "")),
                )
                for tag in sorted(set(split.get("tags") or [])):
                    cur.execute(
                        "INSERT INTO transaction_tags (external_id, tag) VALUES (%s, %s)",
                        (external_id, tag),
                    )
        except Exception as exc:  # noqa: BLE001
            # The unique violation is the interesting one, and it is the check
            # the previous store could not make: the identity is the key.
            raise LedgerError(f"could not store {external_id}: {exc}") from exc

    def update_transaction(self, external_id: str, fields: dict) -> None:
        forbidden = {"amount", "txn_date", "kind", "external_id", "notes", "account"}
        overreach = forbidden & set(fields)
        if overreach:
            raise LedgerError(
                f"an update may not touch {sorted(overreach)} — those come from "
                "the statement"
            )
        columns = {k: v for k, v in fields.items() if k in UPDATABLE}
        with self._conn.transaction(), self._conn.cursor() as cur:
            if columns:
                sets = ", ".join(f"{k} = %s" for k in columns)
                cur.execute(
                    f"UPDATE transactions SET {sets} WHERE external_id = %s",
                    (*columns.values(), external_id),
                )
            if "tags" in fields:
                # Replace, never append: an omitted tag is a deleted tag, which
                # is what lets a stale `not-earnings` be cleared at all.
                cur.execute("DELETE FROM transaction_tags WHERE external_id = %s",
                            (external_id,))
                for tag in sorted(set(fields["tags"] or [])):
                    cur.execute(
                        "INSERT INTO transaction_tags (external_id, tag) VALUES (%s, %s)",
                        (external_id, tag),
                    )

    def delete_transaction(self, external_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM transactions WHERE external_id = %s", (external_id,))

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 — closing twice is not an error worth raising
            pass
