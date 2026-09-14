"""A queryable index over the archive. SPEC §114.

**Measured, and it is the thing the memo could not fix.** §110 stopped every
cold start re-parsing the archive; it did not stop every *request* loading all
of it. Synthetic, from the real fixture:

     statements    rows    parse     memo read
              4     372     122ms         28ms
             25    2325     588ms        210ms
            100    9300    2477ms        829ms
            400   37200    8279ms       3060ms
            800   74400   10685ms       5987ms

Two years of weekly downloads across three accounts is around 300 statements.
Reading every one of them to answer "what did I spend in August" is work that
grows with the whole history for a question about one month, and no amount of
caching the *parse* changes that. Deduping is not the cost — 20ms at 74,000
rows — reading is.

## It is an INDEX, never a source of truth

`archive/` is what `verify-ledger` compares the ledger against (§20). A store
that claimed to hold the same rows would be a third place for them to disagree,
so this one holds no claim of its own:

* every row is keyed to the **content hash** of the statement it came from, the
  same hash `parsecache` uses;
* a statement whose hash is not on disk has its rows deleted on the next sync;
* the whole file can be deleted at any moment and rebuilt from `archive/`, and
  `make clean` does exactly that.

If the index and the archive ever disagree, the archive is right and the index
is stale by one sync.

## Why SQLite

No daemon, no schema migration service, no port, one file, and it is in the
standard library. On a laptop that stops when Windows sleeps (D7), a database
server is a thing to start before the app works. What was needed is a query —
`WHERE account = ? AND date BETWEEN ? AND ?` — and this is the smallest thing
that is one.

**Money is stored as the transaction's own JSON**, not as columns. Splitting a
`Transaction` into typed columns is a second serialisation to keep in step with
the model, and the first `REAL` column anybody adds puts a float in a ledger
(non-negotiable 1). The row comes back through the same `model_validate_json`
the memo uses, so what the index returns is what the file returns.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date
from pathlib import Path

from .models import Transaction

log = logging.getLogger(__name__)

#: Beside the parse memo, and gitignored for the same reason: it holds real
#: payees and real amounts.
INDEX_PATH = Path(".cache/index.sqlite3")

#: What the tables must look like. An index whose shape differs is **dropped
#: rather than migrated** — it is a view over `archive/`, so the cost is one
#: re-read and the alternative is migration code nobody exercises.
EXPECTED_SHAPE = {
    "statements": ["hash", "path", "account", "period_from", "period_to"],
    "txns": ["hash", "account", "txn_id", "day", "row"],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS statements (
    hash    TEXT PRIMARY KEY,
    path    TEXT NOT NULL,
    account TEXT NOT NULL,
    -- The statement's declared period, for `coverage`. Not derivable from the
    -- rows: a quiet fortnight at the start of a range is covered, not missing,
    -- and using transaction dates would silently shorten the month (§18).
    period_from TEXT NOT NULL DEFAULT '',
    period_to   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS txns (
    hash    TEXT NOT NULL,
    account TEXT NOT NULL,
    txn_id  TEXT NOT NULL,
    day     TEXT NOT NULL,
    row     TEXT NOT NULL,
    PRIMARY KEY (hash, txn_id)
);
CREATE INDEX IF NOT EXISTS txns_scope ON txns (account, day);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or INDEX_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    # **Ask the tables, not a version number.** SPEC §114.2.
    #
    # This was `PRAGMA user_version` and it went wrong the first time it was
    # used: the stamp was written whether or not the rebuild happened, so one
    # bad run left a file claiming version 2 with version 1 tables in it. Every
    # read then failed on a missing column and fell through to the archive —
    # correct answers, silently, at full cost.
    #
    # A version number is a promise about the schema. The schema is right there
    # and cannot lie about itself, so it is what gets asked.
    if _shape(conn) not in ({}, EXPECTED_SHAPE):
        log.info("index: schema has drifted, rebuilding from the archive")
        conn.executescript("DROP TABLE IF EXISTS txns; DROP TABLE IF EXISTS statements;")
    conn.executescript(SCHEMA)
    return conn


def _shape(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """The columns of each table, or `{}` for a file with no tables yet."""
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('statements','txns')"
        )
    ]
    return {
        name: [row[1] for row in conn.execute(f"PRAGMA table_info({name})")]
        for name in sorted(tables)
    }


def sync(archive: Path, conn: sqlite3.Connection, parse) -> int:
    """Bring the index level with `archive/`. Returns rows added.

    **It hashes; it does not parse.** That is the whole point: a warm sync reads
    every file's bytes to hash them and parses none of them, so the cost of
    "is the index current" is bounded by the archive's size on disk rather than
    by its contents. Measured, sha256 of a 28 KB statement is 0.04ms and of the
    1.7 MB PDF about 3ms — roughly 35ms across 800 statements, against 6209ms
    to load them.

    `parse` is passed in rather than imported, because `service` imports this
    module and a parse is what `service` does.
    """
    from .parsecache import _key

    files = [
        path
        for path in sorted(archive.rglob("*"))
        if path.is_file() and not path.name.startswith(".")
    ]
    on_disk: dict[str, Path] = {}
    for path in files:
        key = _key(path)
        if key:
            on_disk[key] = path

    known = {row[0] for row in conn.execute("SELECT hash FROM statements")}
    added = 0
    for key, path in on_disk.items():
        if key in known:
            continue
        try:
            statement = parse(path)
        except Exception as exc:
            # One unreadable archive must not blank a page — the same rule
            # `archived_statements` has always had. It simply is not indexed,
            # and the next sync tries again.
            log.warning("index: skipping %s: %s", path.name, exc)
            continue
        account = statement.meta.account_number.strip()
        conn.execute(
            "INSERT OR REPLACE INTO statements "
            "(hash, path, account, period_from, period_to) VALUES (?, ?, ?, ?, ?)",
            (
                key,
                str(path),
                account,
                statement.meta.period_from.isoformat(),
                statement.meta.period_to.isoformat(),
            ),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO txns (hash, account, txn_id, day, row) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (key, account, t.txn_id, t.txn_date.isoformat(), t.model_dump_json())
                for t in statement.transactions
            ],
        )
        added += len(statement.transactions)

    # A statement that is no longer in `archive/` — renamed, replaced, removed —
    # takes its rows with it. The archive is the truth; this is a view of it.
    stale = known - on_disk.keys()
    if stale:
        marks = ",".join("?" * len(stale))
        conn.execute(f"DELETE FROM txns WHERE hash IN ({marks})", tuple(stale))
        conn.execute(f"DELETE FROM statements WHERE hash IN ({marks})", tuple(stale))
        log.info("index: dropped %d statement(s) no longer in the archive", len(stale))

    conn.commit()
    if added:
        log.info("index: added %d row(s)", added)

    # §117.1. The archive's hashes are exactly what a memo is still worth
    # keeping for, and this is the one place they are already known.
    from . import parsecache

    parsecache.prune(set(on_disk))
    return added


def transactions(
    conn: sqlite3.Connection,
    accounts: list[str],
    start=None,
    end=None,
) -> list[Transaction]:
    """The deduped rows for these account numbers, within the window.

    **Reproduces `dedupe_transactions` exactly**, which is first-occurrence-wins
    in the archive's own sorted-path order: statements overlap by design (a
    weekly download re-covers earlier weeks), so the same `txn_id` appears in
    several files and the rule decides which copy is read. `ROW_NUMBER` over
    `ORDER BY s.path` is that rule in SQL, and a test asserts the two agree row
    for row on the real archive rather than trusting the translation.
    """
    if not accounts:
        return []
    marks = ",".join("?" * len(accounts))
    where = [f"t.account IN ({marks})"]
    params: list[object] = list(accounts)
    if start is not None:
        where.append("t.day >= ?")
        params.append(start.isoformat())
    if end is not None:
        where.append("t.day <= ?")
        params.append(end.isoformat())

    rows = conn.execute(
        f"""
        SELECT row FROM (
            SELECT t.row AS row,
                   ROW_NUMBER() OVER (
                       PARTITION BY t.account, t.txn_id ORDER BY s.path
                   ) AS rn
            FROM txns t JOIN statements s ON s.hash = t.hash
            WHERE {" AND ".join(where)}
        ) WHERE rn = 1
        """,
        params,
    ).fetchall()
    return [Transaction.model_validate(json.loads(row[0])) for row in rows]


def paths(conn: sqlite3.Connection, accounts: list[str]) -> set[Path]:
    """Which archived FILES belong to these accounts. SPEC §118.

    The attribution is the one §104 settled — by what the statement says, not by
    where it sits — and it was made once, at index time. `scoped_paths` was
    re-deriving it on every request by parsing the whole archive, which is what
    made `/overview` and `/status` the last two pages that grew with the
    operator's history.
    """
    if not accounts:
        return set()
    marks = ",".join("?" * len(accounts))
    return {
        Path(row[0])
        for row in conn.execute(
            f"SELECT path FROM statements WHERE account IN ({marks})", accounts
        )
    }


def coverage(conn: sqlite3.Connection, accounts: list[str]):
    """What these accounts' statements between them can speak about. §18.

    The union of the declared PERIODS, not of the transaction dates — a quiet
    fortnight at the start of a range is covered, not missing, and reading it
    off the rows would silently turn a full month into a partial one.

    None when nothing is indexed for them, which is the same answer
    `statement_coverage` gives for an empty list.
    """
    if not accounts:
        return None
    marks = ",".join("?" * len(accounts))
    row = conn.execute(
        f"SELECT MIN(period_from), MAX(period_to) FROM statements "
        f"WHERE account IN ({marks}) AND period_from != ''",
        accounts,
    ).fetchone()
    if not row or not row[0]:
        return None
    return date.fromisoformat(row[0]), date.fromisoformat(row[1])


def forget_everything(path: Path | None = None) -> None:
    """Drop the index. It rebuilds from `archive/` on the next sync."""
    (path or INDEX_PATH).unlink(missing_ok=True)
