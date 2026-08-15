# Upgrading

```bash
git pull
make upgrade
```

That is the whole thing. `make upgrade` reports what is pending, takes a
database dump before touching anything, applies migrations in order, and refuses
to record success unless the ledger verifies afterwards.

**Run it after every `git pull`.** It is fast and it is a no-op when nothing is
pending.

---

## Why this exists

`git pull` is silent.

Phase 14 changed the shape of every `external_id` in the ledger — from the
bank's bare transaction id to `<slug>-<txn_id>`. The correct response was a
backup, a purge and a re-push. Someone who pulled that change and simply ran
`make sync` would have got a ledger holding two incompatible id forms, a
`verify-ledger` reporting rows missing, and **nothing on screen ever having said
a migration existed**.

That is the failure this project keeps running into and keeps writing down: not
an error, a *silence*. A ledger once held 21 of 93 rows for seven hours behind an
all-green status strip.

## What it does, in order

1. **Asks every migration whether it has work** — reading the live ledger, not a
   version file. See below.
2. **Stops if nothing is pending**, records the version, exits 0.
3. **Takes a database dump** (`make backup`). This is a precondition, not
   advice: a migration re-pushes rows, which starts with a delete. Without a
   dump newer than an hour it refuses.
4. **Runs each pending migration in version order**, verifying each before
   starting the next.
5. **Runs `verify-ledger`** — the same five checks `passbook doctor` runs.
6. **Records the new version only if that passed.**

If step 5 fails, the version is *not* recorded and the output names the dump to
recover from. A migration that marked itself done on the strength of a function
returning would be the same lie as a green tick for something never checked.

## Checking without changing anything

```bash
uv run passbook upgrade --check
```

Prints what is pending and why, changes nothing, and exits **3** when there is
outstanding work — so you can gate a script on it.

## The version marker is a record, not the authority

`config/schema-version` holds the last version this installation recorded. It is
gitignored, because it is state about *your ledger*, not about the repository.

**Nothing trusts it.** Every migration decides for itself whether it has work by
looking at the actual data — the baseline one counts rows still carrying a bare
transaction id. Delete the file and the worst that happens is one extra check.

The reason is the same one that made `Check.ok` tri-state: a stored claim that
something is fine, which nobody re-checks, is exactly how a broken ledger reads
as a healthy one.

A migration whose check *cannot answer* — Firefly unreachable, say — is reported
as **pending with the reason**, never as "nothing to do". "Could not determine"
must never render as a pass.

## If something goes wrong

Nothing is lost. The dump from step 3 is in `backups/`, and every statement is
in `archive/`.

```bash
uv run passbook verify-ledger          # what is actually wrong
make verify-backup                     # prove the dump restores, in a scratch container
make restore FILE=backups/firefly-<date>.sql.gz CONFIRM=yes
```

If a purge ran and the re-push stopped partway, that is a *recorded* state, not
a mystery: there is a `backups/purge-intent-*.json` naming what was deleted and
what has to go back.

```bash
uv run passbook purge --resume
```

It finishes the cycle and clears the record only once the ledger verifies.

---

## Writing a migration

You need one whenever a change alters **the shape of data already stored** — in
Firefly, in `config/`, or in `archive/`. A new chart does not need one. A new
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
| `ctx.client` | an open `FireflyClient` |
| `ctx.registry` | every registered account |
| `ctx.say(message)` | progress, indented under the migration's name |
| `ctx.purge_and_repush(account)` | delete and re-push one account, through the proven path |
| `ctx.statement_paths(account)` | every archived statement, in a stable order |

**Use `ctx.purge_and_repush` rather than writing your own delete.** It is the
same code `passbook purge --confirm --yes` and `--resume` run: intent recorded
before the first delete, tombstones force-purged so the re-push is not refused
as duplicates, and the record cleared only once the ledger verifies. A second
copy of the most dangerous path in this project is the last thing a migration
should be.

### Rules

- **Detect from the data.** If your `pending()` reads a config value or a
  version file, it can be wrong while claiming to be right.
- **Be idempotent.** `pending()` returns None the second time, because the
  situation it detects is gone.
- **Never delete rows this machine cannot rebuild.** If `archive/` is empty and
  Firefly holds rows, raise — that is data loss with a progress bar. The
  baseline migration does exactly this.
- **Write a test.** `tests/test_migrate.py` has the shape: a fake client, a
  ledger in the old state, and an assertion that a recorded version does not
  make it look clean.

### Then tell people

Note it in the release notes, and if the migration is unusual — long, or
requiring a re-download from the bank — say so in `README.md` too. The whole
point of this machinery is that nobody has to find out from a broken ledger.
