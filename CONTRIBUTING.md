# Contributing

Issues and pull requests are welcome. This page is what you need to know before
opening either.

**passbook reads your bank without anyone writing code for it.** If yours is
not one of the three that ship, open **Account menu → Add a bank**: it shows you
your own file, you say which column is which, and the balance chain tells you
whether you got it right. All of it on your machine, and nobody needs to see
your statement.

---

## The rules that do not bend

1. **Money is `Decimal`, never `float`.** Anywhere. No exceptions.
2. **Strip the trailing `DD/MM/YYYY HH:MM:SS` from UPI narrations before
   splitting on `/`.** The timestamp contains slashes and will otherwise produce
   four spurious tokens. SPEC §6.5.
3. **The balance-continuity invariant must pass before anything is pushed.** It
   is verified to hold cleanly on real data — 93 rows, 0 breaks — and on the
   committed fixture. So if it fails, the parser is wrong, not the data. Fail
   loudly with the row index. **Never soften or skip this check to make a test
   pass.**
4. **Never log or commit** the account number in full, the customer ID, or the
   database password. Mask account numbers to last 4.
5. **The customer ID is a credential** wherever it appears — including inside
   `PMSBY` narration strings.
6. **Nothing under `inbox/`, `archive/`, `backups/`, or `.env` gets committed.**
7. **Never suggest uploading a statement to an online converter.** Privacy-fatal.
8. **Do not ship guessed merchant rules.** Canara truncates UPI payee names to
   ~10 chars and the traffic is mostly person-to-person. A rule matching
   `"Swiggy"` will never fire and will create false confidence. Rules come from
   `passbook payees` output. SPEC D10.
9. **Never total the ledger by hand.** Every spend or earnings figure — a card,
   a chart, a CLI line — comes from `service.ledger_analysis`, which applies
   SPEC §8/§8.1. Counting every withdrawal as spend and every deposit as income
   *by type* read, on one real three-month ledger, **three times** the true
   spend and **1.6 times** the true earnings. Which categories are movement
   rather than spending is **`not_spend` in `config/rules.yaml`, and it is the
   operator's list, not a constant** — never quote it from a doc, because every
   version of every doc that wrote it down was wrong within a release.
   `not-earnings`-tagged deposits are money coming back, not earned. A naive sum
   is not approximately right, and it looks fine.
10. **Never key anything on `txn_id` across accounts.** The bank sequences it
    per account, so two Canara accounts emit identical ids — measured on the two
    committed fixtures: 93 of 93, and the same masked last four. `external_id`
    is `<slug>-<txn_id>` (SPEC §21.1); reads go through `service.txn_id_of`,
    writes through `Account.external_id`. A dict keyed on the bare id merged two
    accounts and kept 93 of 186 rows with no error at all.
11. **A green tick for something you did not check is a lie.** `Check.ok` is
    tri-state — `True`, `False`, `None` — and `None` renders as *unverified*, in
    ochre, never as a pass. A ledger once held 21 of 93 rows for seven hours
    behind an all-green strip (SPEC §19, §20).
12. **Never repair a ledger from a check.** `verify-ledger` reports, names the
    remedy, exits 7. Recovery starts with a verified backup (§19.5); a check
    that silently fixed things would be the most dangerous thing in this repo.
13. **A rule matches the display name, so a rename must carry its category.**
    `description` is pushed as `<alias or token> (<channel>)`, and rules are
    `description_starts` on that. Relabelling a payee without rewriting its
    entry in `rules.yaml` moves the row out from under its own rule and it
    silently loses the category — measured, `Canteen` → `Mess` gave `''`. This
    is not D10 being relaxed: following a rename infers nothing, it preserves a
    decision the operator already made. SPEC §24.4.
14. **An in-place ledger update writes only what config owns.** Description,
    category, counterparty and the *managed* tags. The store **refuses**
    `amount`, `txn_date`, `kind`, `external_id`, `notes` and `account` outright
    — an update that *could* move money is one that eventually does. It used to
    be safe only because the previous store's update happened to be sparse.
    `reversal` and `large-oneoff` are carried through a sync, never re-decided
    by it. DECISIONS.md §24.2, §36.
15. **Two palettes, and which one a mark uses is a question about the mark.**
    `--cat-1..8` encodes **identity** — a category, a payee, an account.
    `--ramp-1..5` encodes **rank or magnitude** — one series measured over
    time, where eight hues would claim eight different things.
    This replaced "the ramp and nothing else": ten categories separated only by
    density are ten shades of the same answer. What did **not** change, because
    it was never aesthetics:
    * **`--stamp` acts, `--ochre` asks, `--verdigris` reconciles, `--alarm`
      failed. No chart mark may use those tokens, and no bank theme may
      redefine them** — tests enforce the second half.
    * **Money is never coloured by sign.** Indigo/cyan on the month pair is
      series identity; red/green would be a verdict. SPEC §16.4.
    * Every hue is **measured**, not chosen: the graphical floor is 3:1 (WCAG
      1.4.11) against its card, 4.5:1 for anything with text on it, in both
      themes. **Contrast is not saturation, and both get measured** — a set can
      clear the ratio and still look washed out, which is what happens when
      only the first is ever checked. SPEC §28.
16. **Tracked documentation cites FIXTURE values, never live ledger values.**
    This one gets its own section, because it is the rule most easily broken by
    accident.

---

## The two rules that are not negotiable

Everything else here is a preference. These two are not.

### 1. No real financial data in a commit. Ever.

Not yours, and certainly not anyone else's. That means:

- **no real statement files** — `tests/fixtures/` is built by
  `scripts/redact.py`, which rewrites the metadata block, every narration and
  every amount, then recomputes the running balance so the continuity invariant
  still holds;
- **no real balances, payee tokens, account numbers or UTRs in documentation,
  comments, tests or example config.** Tracked files cite the fixture's values,
  which are listed above. `make audit-docs` fails the build on a
  figure that is not one of them;
- **no screenshots of a real ledger.** `docs/shots/` is gitignored for exactly
  this reason. Published screenshots are generated from the fixture by
  `scripts/demo_ledger.py`.

`make audit-docs` catches what has a machine-checkable shape — an amount, an
account number, a UTR. It cannot catch a payee's name or a category. That half
is yours to hold, and it is the half that matters most, because several of the
tokens in a real statement are people.

If you are unsure whether something is safe to commit, it is not. Ask in the
issue first.

### 2. Never soften a check to make a test pass.

The balance-continuity invariant is the one that comes up:

```
abs(balance[i] - (balance[i-1] - debit[i] + credit[i])) < Decimal("0.01")
```

It has held cleanly on every real statement this project has seen and on both
committed fixtures. **If it fails, the parser is wrong, not the data.** A PR
that widens the tolerance, skips the check, or catches the exception will not be
merged. The same goes for `verify-ledger`, which reports and exits rather than
repairing — a check that silently fixed a ledger would be the most dangerous
thing in this repository.

The full list is at the top of this page. It is worth
reading once; most of the entries exist because something went silently wrong.

---

## Getting set up

```bash
git clone https://github.com/shubh-garg18/passbook.git
cd passbook
uv sync            # https://docs.astral.sh/uv/ — or python -m venv + pip install -e .
uv run pytest -q
```

**The test suite needs nothing else.** No Docker, no database, no bank account.
Fixtures only; `test_stack.py` is the one module that talks to a running stack
and it auto-skips when there is none.

For UI work you will also want:

```bash
make web-build     # builds the React bundle into src/passbook/web/dist/
```

---

## House style

The code in this repository is deliberately plain, and the comments are
deliberately long. Both are on purpose: this is maintained about once a month,
so readability beats cleverness and the reasoning has to survive being forgotten.

- **Small, boring modules.** No metaclasses, no clever dispatch, no framework.
- **Money is `Decimal`, never `float`.** Anywhere. There are no exceptions and
  there is no rounding you can get away with.
- **Comments say *why*, and say what was measured.** The useful ones in this
  repo look like *"measured: eight codes plus two devices reported as 10, in the
  direction that suppresses the warning"* — not *"count the backup codes"*.
- **If you could not verify something, say so.** A sentence admitting a fact is
  unconfirmed is worth more here than a confident guess. `DECISIONS.md` has a table
  of six times a plausible guess turned out to be wrong.
- **No absolute paths anywhere**, in code or tests. Anchor on
  `conftest.REPO_ROOT`. Four tests once hardcoded one checkout and passed on
  exactly one machine in the world.
- **Ask before adding a dependency.** The runtime image is a plain
  `python:slim` with no compiler in it, and the six-face font budget for the
  whole UI is 46 KB. A new package needs a reason.

## Where things live, and where to extend them

Each of these is a registry, so extending it is a new file rather than an edit
to a `if/elif` chain:

| To add… | Write | Registered by |
|---|---|---|
| a bank's statement dialect | `src/passbook/banks/<bank>.py` | `register(Bank(...))` at import |
| a schema migration | `src/passbook/migrations/mNNN_*.py` | discovered by version number |
| a container format | `src/passbook/loaders/<format>.py` | the magic-byte sniffer |

The rest:

| | |
|---|---|
| `src/passbook/` | parser, service layer, CLI |
| `src/passbook/store/` | the ledger: one interface, a Postgres implementation and an in-memory one the tests hold to the same invariants |
| `src/passbook/web/` | the JSON API; the UI is a front end over `service.py`, never a second implementation |
| `frontend/` | React 19 + Vite, build-time only — no Node in the runtime image |
| `scripts/` | fixture generation, screenshots, setup, backups, DR drill |
| `DECISIONS.md` | what gets built, and why each decision was made |

The user-facing docs are [`SETUP.md`](SETUP.md) and, under `docs/`,
[`usage.md`](docs/usage.md), [`backups.md`](docs/backups.md) and
[`operations.md`](docs/operations.md). Each carries a nav line linking the
others — keep that intact, and think hard before adding a fifth: the set was
consolidated once already because it had grown past what a new user will read.

## Tests

```bash
make test          # everything
make audit-docs    # the documentation rule above
```

- **Tests use fixtures, never the network — and it is enforced, not
  remembered.** `conftest.py` replaces `open_ledger` with one that refuses
  immediately, naming the fixture you should have used. Without that guard a
  test that forgot its fake fell through to a real connection attempt, which
  does not fail fast: it waits out the driver's timeout. A suite of those looks
  exactly like a slow suite, and this one was read as one for months — thirty
  minutes, against under two now.
  The two deliberate exceptions are `test_stack.py` and
  `test_store_postgres.py`, both documented in their own docstrings, both
  auto-skipping when nothing is up.
- **A regression test must be able to fail.** Reintroduce the bug and watch it
  go red before you trust it. One test in this repo passed with the fix deleted,
  because the standard library was quietly covering for the app; it had to be
  rewritten to blind the library first.
- **A double is held to what the database enforces.** `MemoryLedger` refuses
  everything `schema.sql` refuses, because a double that accepts what the
  database would refuse makes the tests pass and production fail. It was found
  missing one — `kind` — the first time the real schema was tested.
- **New behaviour needs a test that would have caught the bug**, not one that
  restates the implementation.

## Anything that renders has to be looked at

Not asserted about — looked at. Phase 10 shipped with 226 passing tests and five
visible design failures, three of them obvious within ten seconds of opening the
page.

```bash
make web-build
uv run --with playwright --with pyotp python scripts/shoot.py mychange
```

That writes every page, in both themes, at desktop and mobile widths, into
`docs/shots/mychange/`. Open the PNGs.

**A screenshot cannot see motion.** It shows the settled state, and `shoot.py`
actively waits for loading to finish, so it excludes every transition, skeleton,
progress bar and toast by construction. For those, use
`scripts/motion.py`, and in the PR say *what you observed* — what the toast
said, how long it stayed, what the button read before it.

## Writing a migration

You need one whenever a change alters **the shape of data already stored** — in
The ledger, in `config/`, or in `archive/`. A new chart does not need one. A new
`external_id` format does.

Create `src/passbook/migrations/mNNN_short_name.py`:

```python
"""One paragraph: what changes, why, and what the operator will see."""

VERSION = 2
NAME = "short-name"
DESCRIPTION = "One sentence, printed to someone deciding whether to run this."


def pending(ctx) -> str | None:
    """Why this needs to run — read from the LEDGER, never from a marker.

    Return None when there is nothing to do. On a fresh install that is the
    normal answer, and `make upgrade` becomes a no-op that stamps the version.
    """


def run(ctx) -> None:
    """Do it. Delegate anything destructive to ctx."""


def verify(ctx) -> str | None:
    """What is still wrong, or None. Usually `pending()` again."""
```

`migrate.all_migrations()` discovers it by filename and orders it by `VERSION`;
`SCHEMA_VERSION` is the highest one found, so there is no second list to update
and duplicate versions are a hard error.

### `ctx`

| | |
|---|---|
| `ctx.settings` | loaded settings |
| `ctx.store` | an open `LedgerStore` |
| `ctx.registry` | every registered account |
| `ctx.say(message)` | progress, indented under the migration's name |
| `ctx.purge_and_repush(account)` | delete and re-push one account, through the proven path |
| `ctx.statement_paths(account)` | every archived statement, in a stable order |

**Use `ctx.purge_and_repush` rather than writing your own delete.** It is the
same code `passbook purge --confirm --yes` runs, followed by the same write
`passbook sync` runs, and it refuses to delete rows this machine cannot rebuild
from `archive/`. A second copy of the most dangerous path in this project is the
last thing a migration should be.

### Rules

- **Detect from the data.** If your `pending()` reads a config value or a
  version file, it can be wrong while claiming to be right.
- **Be idempotent.** `pending()` returns None the second time, because the
  situation it detects is gone.
- **Never delete rows this machine cannot rebuild.** If `archive/` is empty and
  The ledger holds rows, raise — that is data loss with a progress bar. The
  baseline migration does exactly this.
- **Write a test.** `tests/test_migrate.py` has the shape: a fake ledger in the
  old state, and an assertion that a recorded version does not make it look
  clean.

### Then tell people

Note it in the release notes, and if the migration is unusual — long, or
requiring a re-download from the bank — say so in `README.md` too. The whole
point of this machinery is that nobody has to find out from a broken ledger.

## Commits and pull requests

- One change per PR. A bank, a bug, a doc fix.
- Say **what you measured**. "Tested against my HDFC savings export, 214 rows,
  0 continuity breaks" is the useful sentence.
- If you changed the shape of stored data, ship a migration
  (see below) — users pull, and a migration
  nobody runs is a broken ledger.
- If reality contradicted `DECISIONS.md`, update `DECISIONS.md` in the same PR and say
  what moved. A stale spec is worse than no spec.

## Reporting a bug

Use the templates. The one thing to get right: **redact before pasting.** Logs
and error messages can carry an account number, a payee name, or the statement
path. Replace them with `****1111`-style placeholders. A traceback with a real
narration in it is a leak that lives in a public issue forever.

## Security

Do not open a public issue for a vulnerability —
[open a draft advisory](https://github.com/shubh-garg18/passbook/security/advisories/new)
instead. This handles bank statements, so a public write-up is a working exploit
anyone can copy before there is a fix.

## Licence

passbook currently ships under **no open-source licence** — default copyright,
all rights reserved (SPEC §22.3).

That has a consequence worth stating before you spend an afternoon on a pull
request: there is no licence granting you the right to redistribute a modified
copy, and no standing terms covering what happens to a contribution. **If you
want to contribute, open an issue first** and it can be sorted out — either by
agreeing terms for your patch, or by settling the project's licence properly.

Nobody should be asked to hand over work under unclear terms, so this is the
honest state rather than a formality to skip past.
