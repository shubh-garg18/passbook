-- passbook's own ledger. DECISIONS.md §36.
--
-- One table of transactions, keyed on the identity passbook already had.
-- Everything here is a decision that was made somewhere else first and is now
-- enforced by the database instead of by discipline.

-- passbook keeps its tables in a schema of its own rather than in `public`.
-- A schema, not a second database: creating one needs no superuser and no
-- fresh volume, so it lands cleanly inside a database that already exists and
-- already has somebody else's tables sitting in it. An install upgrading off
-- the old ledger therefore needs no database surgery — the old tables are
-- simply not ours, and dropping them is a separate decision made later.
CREATE SCHEMA IF NOT EXISTS passbook;
SET search_path TO passbook;

CREATE TABLE IF NOT EXISTS asset_accounts (
    name             TEXT PRIMARY KEY,
    -- The statement's OPENING balance, not the current one. Seeding with the
    -- closing figure double-counts every row that follows it and leaves the
    -- account negative — the mistake the previous store's own setup invited.
    opening_balance  NUMERIC(18, 2) NOT NULL,
    opening_on       DATE NOT NULL,
    currency         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    -- `<slug>-<txn_id>`. The bank sequences its id PER ACCOUNT, so two accounts
    -- at one bank emit identical ones; the slug is what makes this unique.
    --
    -- **PRIMARY KEY, which is the point.** The previous store decided duplicates
    -- on a hash of the submitted payload, and passbook rewrites that payload
    -- for a living — an alias, a tag, a rename. Seven rows once posted twice
    -- behind 133 identities with nothing raised. Here a second insert of the
    -- same identity cannot happen: it is not a check that can be bypassed, it
    -- is the shape of the table.
    external_id   TEXT PRIMARY KEY,
    account       TEXT NOT NULL REFERENCES asset_accounts(name) ON DELETE RESTRICT,
    -- `withdrawal` | `deposit`. Taken from which column the bank filled, never
    -- from the narration: the narration has been on the wrong row.
    kind          TEXT NOT NULL CHECK (kind IN ('withdrawal', 'deposit')),
    txn_date      DATE NOT NULL,
    -- NUMERIC, never a float, and positive — direction lives in `kind`.
    amount        NUMERIC(18, 2) NOT NULL CHECK (amount >= 0),
    description   TEXT NOT NULL,
    counterparty  TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT '',
    -- The bank's own words, verbatim and never rewritten. Everything derived
    -- from a statement can be recomputed from this plus the archive.
    notes         TEXT NOT NULL DEFAULT '',
    -- Parsed out of the narration, so it exists here and nowhere upstream.
    txn_time      TIME,
    currency      TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every read is "this account, this window", in that order.
CREATE INDEX IF NOT EXISTS transactions_account_date
    ON transactions (account, txn_date);

CREATE TABLE IF NOT EXISTS transaction_tags (
    external_id  TEXT NOT NULL REFERENCES transactions(external_id) ON DELETE CASCADE,
    tag          TEXT NOT NULL,
    PRIMARY KEY (external_id, tag)
);

CREATE INDEX IF NOT EXISTS transaction_tags_tag ON transaction_tags (tag);

-- What happened, and when. Append-only by convention and by the absence of any
-- code that updates or deletes a row here.
--
-- **The gap it closes is config, not money.** Every statement is in `archive/`
-- and `verify-ledger` compares the ledger against it, so the rows have a paper
-- trail. Renaming a payee, moving a category, deleting one, removing an
-- account — those change what the ledger *says* and left no trace anywhere:
-- `config/` is gitignored because it names real counterparties, so there is no
-- history of it, and the operator's own question a week later ("why does this
-- read differently?") had no answer.
--
-- Deliberately small. It records the action, a short human sentence, and a
-- JSON detail for the things worth naming. It is **not** a second copy of the
-- ledger and must never become one: nothing here is read back to compute a
-- figure, and `verify-ledger` remains the authority on whether the rows are
-- right.
CREATE TABLE IF NOT EXISTS audit (
    id         BIGSERIAL PRIMARY KEY,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A short slug: `import`, `rename`, `categorise`, `purge`, `backup`,
    -- `upgrade`, `account`. Matched on by the page's filter, so it is a
    -- vocabulary rather than free text.
    action     TEXT NOT NULL,
    -- One sentence, already written for a person to read. Composed where the
    -- thing happened, because that is the only place that knows what it was.
    summary    TEXT NOT NULL,
    -- Whatever is worth keeping and is not in the sentence. Never a full row,
    -- never a narration, never an account number.
    detail     JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- How many ledger rows it touched, where that is a meaningful number.
    affected   INTEGER
);

-- The page reads "most recent first", always.
CREATE INDEX IF NOT EXISTS audit_at ON audit (at DESC);

-- Free-text search over the narration and the payee.
--
-- `ILIKE '%…%'` cannot use an ordinary index — there is no prefix to seek on —
-- so a search of a long history scans every row. A trigram index can serve it,
-- and `pg_trgm` is a trusted extension, so the database owner can create it
-- without superuser.
--
-- **Both statements tolerate not working.** An operator running a Postgres
-- built without contrib gets a slower search, not a broken install, and that
-- is the right trade for a personal ledger: correctness does not depend on
-- either line. Wrapped in a DO block so a failure is caught rather than
-- aborting the schema.
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pg_trgm unavailable; text search will scan. %', SQLERRM;
END $$;

DO $$
BEGIN
    CREATE INDEX IF NOT EXISTS transactions_notes_trgm
        ON transactions USING gin (notes gin_trgm_ops);
    CREATE INDEX IF NOT EXISTS transactions_description_trgm
        ON transactions USING gin (description gin_trgm_ops);
    CREATE INDEX IF NOT EXISTS transactions_counterparty_trgm
        ON transactions USING gin (counterparty gin_trgm_ops);
    -- `category` too, and it is not an afterthought: the search is an OR over
    -- four columns, and ONE unindexed arm makes the planner scan the table for
    -- all of them. Measured — with three of the four indexed, a search still
    -- read every row.
    CREATE INDEX IF NOT EXISTS transactions_category_trgm
        ON transactions USING gin (category gin_trgm_ops);
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'trigram indexes not created; text search will scan. %', SQLERRM;
END $$;
