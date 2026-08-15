# Contributing

Issues and pull requests are welcome. This page is what you need to know before
opening either.

The single most useful contribution is **a new bank**, and it has its own guide:
[`docs/adding-a-bank.md`](docs/adding-a-bank.md). You do not need a Firefly
instance or a Docker stack to write one.

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
  which are listed in `CLAUDE.md`. `make audit-docs` fails the build on a
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

There is a longer list in `CLAUDE.md` under "Non-negotiables". It is worth
reading once; most of the entries exist because something went silently wrong.

---

## Getting set up

```bash
git clone https://github.com/shubh-garg18/passbook.git
cd passbook
uv sync            # https://docs.astral.sh/uv/ — or python -m venv + pip install -e .
uv run pytest -q
```

**The test suite needs nothing else.** No Docker, no Firefly, no bank account.
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
  unconfirmed is worth more here than a confident guess. `CLAUDE.md` has a table
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
| `src/passbook/` | parser, Firefly client, service layer, CLI |
| `src/passbook/web/` | the JSON API; the UI is a front end over `service.py`, never a second implementation |
| `frontend/` | React 19 + Vite, build-time only — no Node in the runtime image |
| `scripts/` | fixture generation, screenshots, setup, backups, DR drill |
| `SPEC.md` | what gets built, and why each decision was made |
| `CLAUDE.md` | how to work here |

## Tests

```bash
make test          # everything
make audit-docs    # the documentation rule above
```

- **Tests use fixtures, never the network.**
- **A regression test must be able to fail.** Reintroduce the bug and watch it
  go red before you trust it. One test in this repo passed with the fix deleted,
  because the standard library was quietly covering for the app; it had to be
  rewritten to blind the library first.
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

## Commits and pull requests

- One change per PR. A bank, a bug, a doc fix.
- Say **what you measured**. "Tested against my HDFC savings export, 214 rows,
  0 continuity breaks" is the useful sentence.
- If you changed the shape of stored data, ship a migration
  (`docs/upgrading.md`) — users pull, and a migration nobody runs is a broken
  ledger.
- If reality contradicted `SPEC.md`, update `SPEC.md` in the same PR and say
  what moved. A stale spec is worse than no spec.

## Reporting a bug

Use the templates. The one thing to get right: **redact before pasting.** Logs
and error messages can carry an account number, a payee name, or the statement
path. Replace them with `****1111`-style placeholders. A traceback with a real
narration in it is a leak that lives in a public issue forever.

## Security

Do not open a public issue for a vulnerability. `SECURITY.md` has the private
route.

## Licence

By contributing you agree your work is licensed under **AGPL-3.0-or-later**, the
same as the rest of the project. See `LICENSE` and `NOTICE.md`.
