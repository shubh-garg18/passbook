# Using it

[← README](../README.md) · [What is this?](what-is-this.md) · [Setup](../SETUP.md) · **Usage** · [Backups](backups.md) · [Operations](operations.md)

> **On Windows?** `make` is a Unix tool and Windows does not ship it. Every
> `make …` below has a one-line equivalent —
> [the table is in SETUP.md](../SETUP.md#every-command-on-windows). The
> `uv run passbook …` commands work everywhere as written.

---

## What the figures mean

Firefly counts **every withdrawal as spend and every deposit as income, by
type**. Measured on one real three-month ledger, read that way it said **three
times** the true spend and **1.6 times** the true earnings. That is not a
rounding error, and a chart drawn on the naive numbers looks entirely reasonable.

Two rules fix it, and both live in your own `config/rules.yaml`:

- **Money moving is not money leaving.** Investments, transfers between your own
  accounts, credit-card payments and penny-drop verifications are excluded from
  spend. That list is `not_spend` — your category names, so yours to edit. A
  category named there that does not exist simply excludes nothing.
- **Money coming back is not money earned.** Family support, repayments, refunds
  and verifications carry a `not-earnings` tag, so earnings are salary and
  interest and nothing else. It is expressed as an inversion rather than a payee
  list, so a counterparty appearing for the first time next week is excluded
  automatically — a payee list defaults the wrong way, and nobody notices until
  the earnings figure is quietly too high.

Every figure on every page goes through **one function** that applies both rules,
and each chart **draws what it excluded** — hatched, beside what it counted —
rather than leaving you to wonder where the rest went.

The naive reading is never shown as if it were the answer. If a card or a chart
ever reads close to the gross withdrawal total, something has bypassed that
function.

---

## The weekly cycle

**Browser** — http://localhost:8081 → Upload → check the preview → Push.

**Terminal:**

```bash
# 1. download the statement from net banking into inbox/   (manual)
uv run passbook doctor          # 2. token alive? right account?
uv run passbook sync            # 3. push, then archive on success
```

Both call the same code: one parser, one push path, one balance check.

`sync` is safely re-runnable. A file is archived **only** after a successful
push; a failure leaves it in `inbox/` and exits non-zero. Weekly downloads
overlap, so most rows on a second run are rejected as duplicates — that is
deduplication working. `pushed 0, duplicates 93` is a correct outcome, and the
two are counted separately so you can tell at a glance.

### Remembering to do it

**Set a weekly calendar reminder.** There is no cron container: nothing scheduled
is reliable on a machine that sleeps, and a scheduler that silently stops is
worse than none.

passbook does not remind you — it *checks whether the reminder worked*, on
`make up`, `make sync` and `passbook doctor`:

```
ok     last sync 3 days ago (statement.xls)
warn   last successful sync was 14 days ago      >10 days
STALE  last successful sync was 30 days ago      >21 days
```

It escalates because **a gap is data loss, not lateness.** Banks only serve
statements so far back; rows that age out are gone from every copy you hold, and
no backup brings them back — only the bank has them, and not forever.

> **No email, deliberately.** SMTP means an app password in `.env` forever, for a
> job a calendar entry does better and for free.

---

## Is the ledger still right?

```bash
uv run passbook verify-ledger      # exit 7 if not; `doctor` runs the same checks
```

Five checks: the balance equals the newest statement's closing figure; the live
transaction ids equal what is in `archive/`; no soft-deleted rows; no unfinished
purge; the opening balance is present and carries no id. Failures name the figure
and the drift, not just "mismatch".

**Why it exists.** A purge plus an interrupted re-push once left the ledger
holding **21 of 93 rows** with a *self-consistent* balance, and every check that
existed passed for seven hours. The continuity invariant validates a statement
*file*; nothing validated Firefly. This catches that corruption whatever caused
it — an interrupted purge, a row deleted by hand, a restore of the wrong dump.
Full incident in SPEC §19, the checks in §20.

One check needs a database query the API cannot answer, so the web UI shows it as
**unverified** in amber rather than green. A tick for something never checked is
a lie.

**If it reports an unfinished purge**, that is a *stated* state rather than a
mystery — intent is recorded before the first delete:

```bash
uv run passbook purge --resume     # finishes, and clears the record only once §20 passes
```

---

## Writing categorisation rules

```bash
make payees FILE=archive/canara-1111/2026-08/statement.xls
```

Ranks payee tokens by frequency and value. Write `config/rules.yaml` against what
it prints.

**Do not match full merchant names.** Canara truncates the UPI counterparty to
about ten characters, so even a real merchant arrives as a nine-character stub
and a rule matching `Google Pay` silently never fires. Match prefixes.

passbook ships **no merchant rules**, deliberately: of ten tokens read from the
fragment alone, **four were wrong**. A shipped guess that never fires creates
false confidence that categorisation is working. The three that do ship are bank
charges, interest income and a large-one-off tag.

Most traffic is person-to-person UPI, so most of your rules will be about people.

### Aliases

`config/payee_aliases.yaml` maps a truncated token to a display name. Keys must
be the token **exactly as the bank emits it**, double spaces included — quote
those or yaml eats them and the alias silently never fires.

An alias changes the display name only. The raw narration still reaches Firefly's
notes verbatim, and `payee` keeps the bank's token, so nothing rewrites source
data.

### Re-applying after an edit

Rules and aliases apply **at push time**, so editing them does not reach rows
already in Firefly. Renaming a payee on the Payees page now writes those rows in
the same action; **Re-apply** shows you what would move before it does.

From a terminal:

```bash
uv run passbook resync            # dry run: what config would change
uv run passbook resync --confirm  # write it, in place
```

**`resync` deletes nothing and needs no backup.** It sends the description, the
category, the payee account on the other side, and the tags your rules derive —
and nothing else. Amount, date and the raw narration are not in the request, so
they cannot move. Running it twice is the same as running it once.

It also cannot fix everything, and says so instead of implying otherwise: a row
that is **missing** from the ledger, or one whose amount is wrong, comes from
the statement. Those need a rebuild:

```bash
make backup                     # always — the next step is a delete
uv run passbook purge           # dry run: what would go
uv run passbook purge --confirm # prompts first
uv run passbook purge --resume  # pushes every archived statement back
```

`purge` only removes rows carrying an `external_id`, which is exactly what
passbook pushed — your opening balance has none, so it is never a candidate.
That is structural, not a date guard. There is deliberately no `make purge`.

> **"All 0 transactions already match" is not a pass.** Zero *compared* and zero
> *differing* are different answers, and a version of this once reported the
> first as the second, in green, for every row. If you see nothing was compared,
> check `passbook verify-ledger` and that `PASSBOOK_ASSET_ACCOUNT` names the
> account your rows went into.

---

## Updating

```bash
git pull && make upgrade
```

`make upgrade` asks every migration whether it has work — **reading the live
ledger, not a version file** — then takes a database dump, applies what is
pending in order, and runs `verify-ledger`. It records the new version only if
that passed. A no-op when nothing is pending, so run it after every pull.

The backup is a precondition, not advice: a migration re-pushes rows, which
starts with a delete. Without a dump newer than an hour it refuses.

```bash
uv run passbook upgrade --check    # what is pending and why; exits 3 if any
```

**The version marker is a record, not the authority.** `config/schema-version`
holds what this install last recorded, and nothing trusts it — every migration
decides for itself by looking at the data. Delete the file and the worst that
happens is one extra check. A migration that *cannot answer* — Firefly
unreachable, say — is reported as pending with the reason, never as "nothing to
do".

If something goes wrong nothing is lost: the dump is in `backups/` and every
statement is in `archive/`. `uv run passbook verify-ledger` says what is
actually wrong.

Writing a migration is in [CONTRIBUTING.md](../CONTRIBUTING.md#writing-a-migration).

---

## Looking at it

Four pages, and they answer different questions.

**Ledger** is the overview: the balance, what you actually spent, what you
actually earned, and where it went. Every figure on it respects the exclusions
described above — nothing here is a raw by-type total.

**Rows** is every transaction, searchable. Payee, category, amount, or the
bank's own narration; filter by direction, category, tag, a size band, or the
window. It deliberately has **no running balance column**: a balance over a
filtered, reordered view asserts a continuity that is not there, and the balance
chain is the one thing this project will not fudge. The statement sheet keeps
its balance; a search result does not get one.

**Reports** is the same analysis cut five ways — by category, by payee, by tag,
over time, and the rhythm of the week and the hour. Firefly ships these as
separate screens; they are one shape, so they are one page with a control.

**Payees** is where decisions get made, and where the three config edits live
that used to need a text editor: creating a category, removing an empty one, and
saying what counts as earned.

### The window

Every page takes a date range — this month, last month, 3 or 6 months, this year,
everything, or a custom pair. It is resolved on the server and echoed back, so
what you see is the scope the server used rather than the one the page asked for.
It lives in the URL, so a reload or a shared link keeps it.

## A reminder that arrives when the laptop is shut

Nothing here is scheduled, because the machine this runs on sleeps. A cron entry
that fires with the lid closed is a reminder that never arrives.

So passbook does not send the reminder — **your calendar does**. The page builds
the event and hands it over three ways:

- **Open in Google Calendar** — needs nothing set up, which is why it is first.
- **Download .ics** — for Apple Calendar, Outlook, anything.
- **Email me the invite** — needs a mail server, and the button says so rather
  than failing when you press it.

Mail settings, if you want them, live in `config/reminder.yaml`, owner-only, and
are never shown back to you.

## Taking a backup

```bash
make backup          # from a terminal
```

or press **Take a backup** on the Status strip. Either way it is a real
`pg_dump`, and the purge refuses to run without one from the last hour.

> The client in the image is pinned to the database's own major version. A
> newer `pg_dump` will happily dump an older server and emit output that server
> cannot read back — so the backup looks perfect until the day you need it.

## More than one account

The first statement registers its own account, so a single-account setup needs no
configuration and never mentions the feature. A second is deliberate:

```bash
uv run passbook accounts add inbox/second-statement.xls
uv run passbook accounts list
```

**Why deliberate:** Canara's transaction id is `YYYYMMDD` plus a per-date
ordinal, **sequenced per account** — so a second Canara account emits the *same
ids*. Every pushed row carries `<slug>-<id>` instead. Aliases and rules stay
shared, because the same person's payees are the same whichever account paid.
Statements archive per account, because the bank names every export for a date
range identically and the second would overwrite the first.

A statement is routed by **the account number inside it**, never by which folder
it sits in or which account the switcher shows. One from an unregistered account
is refused, never silently imported.

**"All accounts" combines everything except the balance.** Spend, categories,
months and the Day Rail are sums over transactions, and exclusions are decided
per transaction, so combining changes no meaning. The balance is different: summed
it is true, but it reconciles against no statement's closing figure — so it reads
*"Balance, summed"* and lists the per-account figures underneath. Staleness shows
the **worst** age, never an average, and the integrity verdict is the worst across
accounts, each check prefixed by its slug.

---

## Reading a statement without pushing

```bash
make parse FILE=inbox/statement.xls
```

Validates before printing. Four things must hold, and any failure exits non-zero:
the running balance reconciles row by row and lands exactly on the Closing
Balance sentinel; exactly one of Withdrawals/Deposits per row; transaction ids
unique; the account number matches a registered account.

**If the balance check fails, the parser is wrong, not the statement.** It holds
cleanly on real data and on both fixtures. The error names the sheet row and both
balances. Do not soften it.

Softer things — a transaction id whose date prefix disagrees with its row, dates
going backwards, a narration whose direction disagrees with the columns — are
warnings and do not stop the run.

---

## The CLI

```bash
uv run passbook doctor                             # check before pushing anything
uv run passbook parse  inbox/statement.xls --json  # normalised JSON
uv run passbook payees inbox/statement.xls --top 40
uv run passbook push   inbox/statement.xls --dry-run
uv run passbook sync                               # process inbox/, archive on success
uv run passbook purge [--confirm|--resume]
uv run passbook verify-ledger
uv run passbook upgrade --check
uv run passbook accounts list | add <statement>
```

`make help` lists the make targets, which are thin wrappers over these.
