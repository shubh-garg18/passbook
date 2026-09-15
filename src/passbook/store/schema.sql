-- passbook's own ledger. DECISIONS.md §36.
--
-- One table of transactions, keyed on the identity passbook already had.
-- Everything here is a decision that was made somewhere else first and is now
-- enforced by the database instead of by discipline.

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
