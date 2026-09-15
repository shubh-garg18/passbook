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

    #: Every column a caller reads back, in the order the query selects them.
    COLUMNS = (
        "external_id", "account", "kind", "txn_date", "amount", "description",
        "counterparty", "category", "notes", "txn_time", "currency", "tags",
    )

    #: The row, without its tags. **Deliberately without.**
    #:
    #: Joining `transaction_tags` and grouping put the aggregate *below* the
    #: LIMIT in the plan, so asking for a hundred rows aggregated **every row
    #: the account had** first — measured on a synthetic ten-year ledger, a
    #: `HashAggregate` over the whole table feeding a `Limit` of 100. Tags are
    #: fetched for the page that came back instead, which is one small query
    #: keyed on the primary key.
    _SELECT = (
        "SELECT t.external_id, t.account, t.kind, t.txn_date, t.amount,"
        "       t.description, t.counterparty, t.category, t.notes,"
        "       t.txn_time, t.currency"
        "  FROM transactions t"
    )

    #: Every column except the tags, which arrive separately.
    _PLAIN = (
        "external_id", "account", "kind", "txn_date", "amount", "description",
        "counterparty", "category", "notes", "txn_time", "currency",
    )

    def _with_tags(self, rows: list[dict], where: str = "", args: tuple = ()) -> list[dict]:
        """Attach each row's tags. Every row gets a list, empty where it has none.

        Two shapes, and which one is right depends on how many rows there are.

        For a **page**, the ids go to the server: a hundred of them is a small
        parameter and an exact primary-key lookup.

        For a **bulk read**, they must not. Sending a ten-year ledger's ids as
        one array spent an eighth of the whole request just dumping the
        parameter — measured, in `psycopg.types.array.dump_list`, before the
        query had even been sent. So the caller passes the predicate it already
        used and the server re-derives the set itself.
        """
        for row in rows:
            row["tags"] = []
        if not rows:
            return rows
        index = {row["external_id"]: row for row in rows}

        if where:
            sql = (
                "SELECT g.external_id, g.tag FROM transaction_tags g"
                " WHERE g.external_id IN"
                f" (SELECT t.external_id FROM transactions t WHERE {where})"
                " ORDER BY g.tag"
            )
            params: tuple = args
        else:
            sql = (
                "SELECT external_id, tag FROM transaction_tags"
                " WHERE external_id = ANY(%s) ORDER BY tag"
            )
            params = (list(index),)

        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            for external_id, tag in cur.fetchall():
                row = index.get(external_id)
                if row is not None:
                    row["tags"].append(tag)
        return rows

    def account_transactions(
        self, account: str, start=None, end=None, *, limit: int | None = None
    ) -> list[dict]:
        """One account's rows, optionally within a window.

        **The window is a parameter because it has to be served by the index,
        not by the caller.** `transactions_account_date` is on
        `(account, txn_date)` precisely so that "this month" reads a month.
        Filtering afterwards in Python costs the same as asking for everything,
        and it was measured costing exactly that: on a synthetic ten-year
        ledger, one month took as long as ten years did.

        `limit` is for a caller that wants the newest few — the most recent
        rows come back last, so it trims from the front.
        """
        where = ["t.account = %s"]
        args: list = [account]
        if start is not None:
            where.append("t.txn_date >= %s")
            args.append(start)
        if end is not None:
            where.append("t.txn_date <= %s")
            args.append(end)

        clause = " AND ".join(where)
        sql = self._SELECT + " WHERE " + clause + " ORDER BY t.txn_date, t.external_id"
        predicate: tuple = (clause, tuple(args))
        if limit is not None:
            sql += " LIMIT %s"
            args.append(int(limit))
            predicate = ("", ())  # a page: the ids are few, send them

        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(args))
            rows = [dict(zip(self._PLAIN, r)) for r in cur.fetchall()]
        return self._with_tags(rows, *predicate)

    #: How the table may be ordered. A closed set, because the value reaches
    #: SQL as text — a caller-supplied ORDER BY is an injection waiting to be
    #: written, and there are only four orders a person wants.
    ORDERS = {
        "newest": "t.txn_date DESC, t.external_id DESC",
        "oldest": "t.txn_date ASC, t.external_id ASC",
        "amount": "t.amount DESC, t.txn_date DESC",
        "amount-asc": "t.amount ASC, t.txn_date ASC",
    }

    def _tags_like(self, pattern: str) -> list[str]:
        """Tag names matching a pattern. A handful of rows, off an index."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT tag FROM transaction_tags WHERE tag ILIKE %s", (pattern,)
            )
            return [r[0] for r in cur.fetchall()]

    def search_transactions(
        self,
        accounts,
        *,
        start=None,
        end=None,
        kind: str | None = None,
        category: str | None = None,
        tag: str | None = None,
        minimum=None,
        maximum=None,
        query: str | None = None,
        order: str = "newest",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """One page of rows, and how many matched. `(rows, matched)`.

        **Every filter is a WHERE clause on purpose.** This used to read every
        row an account held, build a dict for each, filter, sort and then keep
        a hundred — so a search of one month over ten years of history cost the
        same as a search of the ten years, twice over: once in the database
        handing out rows nobody wanted, once in Python discarding them. It took
        **fourteen times** as long as it does now, to return a single page.

        `matched` is a second query over the same predicate rather than the
        length of the page, because the caption says "N matched" and a page is
        a hundred of them.

        The text search covers the raw narration as well as the display name.
        Searching for a UTR or a bank reference is exactly the case where the
        payee's name is no help, and it is why this page can replace a
        general-purpose search.
        """
        where = ["t.account = ANY(%s)"]
        args: list = [list(accounts)]
        if start is not None:
            where.append("t.txn_date >= %s")
            args.append(start)
        if end is not None:
            where.append("t.txn_date <= %s")
            args.append(end)
        if kind:
            where.append("t.kind = %s")
            args.append(kind)
        if category is not None:
            # `(no category)` is what the analysis calls the empty one, so the
            # same string has to reach a row whose category really is empty.
            where.append("t.category = %s")
            args.append("" if category == "(no category)" else category)
        if tag:
            where.append(
                "EXISTS (SELECT 1 FROM transaction_tags g2"
                "         WHERE g2.external_id = t.external_id AND g2.tag = %s)"
            )
            args.append(tag)
        if minimum is not None:
            where.append("t.amount >= %s")
            args.append(Decimal(str(minimum)))
        if maximum is not None:
            where.append("t.amount <= %s")
            args.append(Decimal(str(maximum)))
        if query:
            # The amount arm is included **only when the query could be an
            # amount**. `t.amount::text ILIKE …` is a cast in a predicate, so
            # no index can serve it, and OR-ing it in unconditionally forced a
            # sequential scan over every other arm — which is what the trigram
            # indexes exist to avoid. Typing a figure to find a row still works;
            # typing a payee no longer pays for the possibility — and a payee
            # token with digits in it, which is most of them, is a payee.
            arms = [
                "t.description ILIKE %s",
                "t.counterparty ILIKE %s",
                "t.category ILIKE %s",
                "t.notes ILIKE %s",
            ]
            like = f"%{query}%"
            values = [like] * len(arms)

            # **Tags are resolved to names first, and only then joined.**
            #
            # A subquery arm inside the OR — correlated or not — gives the
            # planner nothing to estimate, so it abandons the bitmap OR over
            # the four indexed columns and scans the table. Measured both ways:
            # `EXISTS (… g3.external_id = t.external_id …)` and
            # `IN (SELECT … WHERE tag ILIKE …)` both produced a `Seq Scan` over
            # every row to find a couple of hundred.
            #
            # The vocabulary is small — the tags in `rules.yaml` plus
            # `reversal` and `large-oneoff` — so naming them costs one cheap
            # query against an index that already exists. When nothing matches,
            # which is the usual case, the arm is left out entirely and the
            # bitmap survives. When something does, the join is on an exact
            # list rather than a pattern.
            matching_tags = self._tags_like(like)
            if matching_tags:
                arms.append(
                    "t.external_id IN (SELECT g3.external_id FROM transaction_tags g3"
                    "                   WHERE g3.tag = ANY(%s))"
                )
                values.append(matching_tags)
            if query.strip(" 0123456789.,-") == "":
                arms.append("t.amount::text ILIKE %s")
                values.append(like)
            where.append("(" + " OR ".join(arms) + ")")
            args.extend(values)

        clause = " WHERE " + " AND ".join(where)
        ordering = self.ORDERS.get(order, self.ORDERS["newest"])

        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM transactions t" + clause, tuple(args)
            )
            matched = int(cur.fetchone()[0])

            cur.execute(
                self._SELECT + clause + f" ORDER BY {ordering} LIMIT %s OFFSET %s",
                tuple(args) + (int(limit), int(offset)),
            )
            rows = [dict(zip(self._PLAIN, r)) for r in cur.fetchall()]
        return self._with_tags(rows), matched

    def count_transactions(self, account: str) -> int:
        """How many rows this account holds, all of them.

        A count, not a length: the page says "N outside the window" beside a
        windowed read, and computing it by fetching every row to measure the
        list would undo the point of windowing the read.
        """
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM transactions WHERE account = %s", (account,))
            return int(cur.fetchone()[0])

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

    # --- what happened -------------------------------------------------------

    def record_event(self, action: str, summary: str, detail: dict, affected) -> None:
        """Append one entry. Never updates, never deletes — there is no code
        here that can, which is what makes the table a record rather than a
        second opinion."""
        import json

        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit (action, summary, detail, affected)"
                " VALUES (%s, %s, %s::jsonb, %s)",
                (action, summary, json.dumps(detail, default=str), affected),
            )

    def events(self, limit: int = 200, action: str | None = None) -> list[dict]:
        where, args = "", []
        if action:
            where = " WHERE action = %s"
            args.append(action)
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT at, action, summary, detail, affected FROM audit"
                + where
                + " ORDER BY at DESC, id DESC LIMIT %s",
                tuple(args) + (int(limit),),
            )
            return [
                {
                    "at": at.isoformat(),
                    "action": act,
                    "summary": summary,
                    "detail": detail or {},
                    "affected": affected,
                }
                for at, act, summary, detail, affected in cur.fetchall()
            ]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 — closing twice is not an error worth raising
            pass
