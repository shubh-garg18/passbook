# Contributing

Issues and pull requests are welcome, and so is a one-line report that your
bank worked.

**You do not need to write code to help.** If your bank is not one of the three
that ship, open **Account menu → Add a bank**: it shows you your own file, you
say which column is which, and the balance chain tells you whether you got it
right. All of it on your machine — nobody needs to see your statement. If it
parsed, the profile it wrote is worth sharing.

## Running it

```bash
git clone https://github.com/shubh-garg18/passbook.git
cd passbook
uv sync            # https://docs.astral.sh/uv/ — or python -m venv + pip install -e .
uv run pytest -q   # about a minute
```

**The tests need nothing else.** No Docker, no database, no bank account —
fixtures only. Two files are the deliberate exceptions and both skip themselves
when there is nothing to talk to: `test_stack.py` (a running stack) and
`test_store_postgres.py` (a real database).

For UI work you also want `npm --prefix frontend install`, then
`make web-build` before you look at anything.

---

## The two rules that are not negotiable

Everything else on this page is a preference. These two are not, and a pull
request that breaks either will be declined however good the rest of it is.

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

There is a longer list of rules like these — twenty of them, each one a scar —
in [DECISIONS.md §40](DECISIONS.md). You do not need to read it to open a pull
request. Read it if one of yours gets a comment you did not expect.

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
the ledger, in `config/`, or in `archive/`. A new chart does not need one; a new
`external_id` format does.

Most changes do not need one, so the reference lives with the other long-form
material: **[DECISIONS.md §41](DECISIONS.md)** has the file shape, what `ctx`
gives you, and the four rules that matter. The short version:

- **Detect from the data**, never from a version file. A marker can be wrong
  while claiming to be right.
- **Be idempotent** — running it twice is running it once.
- **Never delete rows this machine cannot rebuild** from `archive/`. That is
  data loss with a progress bar.
- **Write a test.** `tests/test_migrate.py` has the shape.

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
