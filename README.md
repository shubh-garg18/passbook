# passbook

[![tests](https://github.com/shubh-garg18/passbook/actions/workflows/ci.yml/badge.svg)](https://github.com/shubh-garg18/passbook/actions/workflows/ci.yml)
[![stars](https://img.shields.io/github/stars/shubh-garg18/passbook?style=flat&color=blue)](https://github.com/shubh-garg18/passbook/stargazers)
[![forks](https://img.shields.io/github/forks/shubh-garg18/passbook?style=flat&color=blue)](https://github.com/shubh-garg18/passbook/network/members)
[![any bank](https://img.shields.io/badge/any%20bank-no%20code-brightgreen)](#is-it-for-you)

**Your bank statement, turned into a ledger that tells you the truth about your
spending.** Self-hosted, offline, one command to install.

You download a statement once a week and drop it on a page. passbook reads it,
checks the arithmetic, sorts it, and shows you where the money actually went.
Nothing is uploaded anywhere — there is no account to make and no server to
trust.

> **New here, or not a developer?** → **[What is this?](docs/what-is-this.md)**
> — the whole idea in plain language, no jargon, two minutes.

Most tools count every withdrawal as spending. On a real three-month statement
that read **three times** what was actually spent — because moving money to your
own savings is not spending, and a refund is not income. passbook fixes that, and
shows you what it excluded instead of hiding it.

<sub>**Spent** is not the same as **money out**, and the difference is named rather than hidden:</sub>

![The Ledger page](docs/screenshots/ledger.png)

<sub>Reports cuts the same analysis five ways — by category, by payee, by tag, over time, and the rhythm of your week:</sub>

![Reports](docs/screenshots/reports.png)

<sub>Every row, searchable — by payee, category, amount band, tag, or the bank's own narration:</sub>

![Transactions](docs/screenshots/transactions.png)

<sub>Every picture here is generated from `tests/fixtures/statement.xls`. Never from a real ledger — the payee names are invented and so are the categories.</sub>

## Install

```bash
git clone https://github.com/shubh-garg18/passbook.git
cd passbook
make setup
```

**On Windows** there is no `make`, so the last line is
`python scripts\setup.py` instead — or double-click
`launchers\start-passbook.cmd`. Every other `make …` in the docs has a
one-line Windows equivalent, [listed here](SETUP.md#every-command-on-windows).

That is it. The wizard checks what Docker needs on *your* machine and offers to
fix it, generates every secret, starts the stack, and reads your account number
and opening balance out of the statement itself.

It asks you two things: a statement to read, and a password.
**[Full instructions →](SETUP.md)**

> **Using Claude Code?** Paste this and it does the whole thing:
> `Clone https://github.com/shubh-garg18/passbook.git and set it up by following its SETUP.md.`

Then everything happens at **http://localhost:8081** — upload, categories,
reports, backups. No terminal.

## Keeping it current

passbook tells you when there is a new version, on its own Status page, with the
list of what changed. Applying it is one command, or on Windows one double-click:

```bash
make update          # or: double-click launchers/update-passbook.cmd
```

It backs up first, then pulls, rebuilds, applies any data migrations, and checks
every row against the statements in your archive. If a step fails it stops there
— the version you had is still the one running.

> **There is deliberately no "update" button in the app.** Updating rebuilds the
> container, which needs control of Docker, and the one process that listens on a
> port and reads uploaded files is the last place that belongs.

> **Already running the version that used Firefly III?** Same command.
> `make upgrade` rebuilds your ledger from `archive/` — the files your bank
> produced, which is the only source that cannot have inherited a mistake.
> [What happens, and what to delete afterwards →](docs/usage.md#coming-from-the-version-that-used-firefly-iii)

## Is it for you?

| | |
|---|---|
| ✅ | You want your own numbers, on your own machine, with nothing uploaded anywhere |
| ✅ | You are happy downloading a statement once a week |
| ✅ | **Canara, SBI and Union Bank** out of the box — and if yours is not one of them, *Account menu → Add a bank* reads your own file and takes the column mapping, on your laptop, with nothing sent anywhere |
| ⚠️ | Needs a PC (Windows, macOS or Linux). Not a phone |
| ❌ | You want automatic bank sync — India's Account Aggregator framework is closed to individuals, so nobody self-hosted can offer it |

## What it does

Everything below happens in the browser. There is a CLI and you never have to
open it.

| | |
|---|---|
| **Upload** | drop the statement in; it parses, walks the balance chain, and shows you what it found before anything is written |
| **Ledger** | balance, what you spent, what you earned, where it went, and the rhythm of your week |
| **Rows** | every transaction, searchable — by payee, category, amount band, tag or date |
| **Reports** | by category, by payee, by tag, over time — one page with a control, not five screens |
| **Payees** | name them once; the naming sticks, and edits reach rows already in the ledger |
| **Accounts** | add, rename and remove accounts; combine any subset in one view |
| **Add a bank** | not one of the three that ship? Map your own columns in about ten minutes |
| **Activity** | what changed your ledger and when — every import, rename, category and deletion |
| **Reminder** | a real calendar invitation, so it reaches you when the laptop is shut |
| **Backups** | take a database dump from a button; verified, off-site archives from the host |
| **Status** | is the ledger reachable, does it still match your statements, is there a new version |

<sub>Activity answers the question a week later — *why does this read differently?*</sub>

![Activity](docs/screenshots/activity.png)

## What makes it different

Most expense trackers show you a number. This one tries hard not to show you a
wrong number.

- **It checks itself against your statements.** A purge plus an interrupted
  re-push once left the ledger holding **21 of 93 rows with a self-consistent
  balance — and every check passed for seven hours.**
  [`verify-ledger`](docs/usage.md#is-the-ledger-still-right) exists because of
  that day.
- **It ships no merchant rules.** Banks truncate payee names to ~10 characters;
  of ten guessed from the fragment alone, **four were wrong**. A rule that never
  fires is worse than no rule, so
  [you write them from your real data](docs/usage.md#writing-categorisation-rules).
- **Money is `Decimal` everywhere**, including in the database — the amount
  column is `NUMERIC`, not a float, so nothing can round your balance.
- **A row cannot be stored twice.** Its identity is the primary key, not a
  check something could skip. That is not theoretical: the previous ledger
  decided duplicates on a hash of what was sent, passbook rewrites what it
  sends whenever you rename a payee, and seven rows once went in twice with
  nothing raised anywhere.
- **Backups are drilled, not assumed.** `make dr-drill` rebuilds the whole ledger
  from the encrypted archives on every run.
- **It stays fast with years in it.** Every window, filter and search is a
  database query, not a Python loop over everything you have — measured on a
  synthetic ten-year ledger, where a page that took nearly two seconds now
  takes a seventh of one.
- **A rename reaches the rows you already have.** Editing a payee used to
  change only what *future* imports produced; now it updates the ledger in
  place, touching only the four fields config owns. The ledger **refuses** an
  update that names an amount, a date or a direction — so a rename cannot
  corrupt anything, by construction rather than by care.

## Help wanted

Small, self-contained, and genuinely useful:

- **Say whether your bank worked.** *Account menu → Add a bank* maps your own
  file on your own machine — if it parsed, the profile it wrote is worth sharing
  so the next person gets it for free; if it did not, that is the most useful
  bug there is.
- **Try it and say what broke.** [Open an
  issue](https://github.com/shubh-garg18/passbook/issues) — a setup step that was
  wrong on your machine is the most useful bug there is.
- **Tell us it worked** in
  [Discussions](https://github.com/shubh-garg18/passbook/discussions), especially
  on macOS or a distro this has never run on.

⭐ **If this is useful, star it.** A star is the only signal this project can
receive: it runs entirely on your machine, it has never phoned home, and it never
will. There is no install counter and there is not going to be one.

## Docs

| | |
|---|---|
| [What changed](CHANGELOG.md) | what is new, and what it means for you |
| [What is this?](docs/what-is-this.md) | the idea in plain language — start here if you are not a developer |
| [SETUP.md](SETUP.md) | install, first run, signing in, when it breaks |
| [Usage](docs/usage.md) | the weekly cycle, rules, reports, reminders, more than one account |
| [Backups](docs/backups.md) | `make backup`, off-site, the recovery runbook |
| [Operations](docs/operations.md) | what runs, the threat model, tests |
| [Contributing](CONTRIBUTING.md) | how to work on it, if you want to |
| [Decisions](DECISIONS.md) | every decision, with the measurement behind it. Long, and meant to be |

## Licence

**None yet — all rights reserved.** No open-source licence is granted, so you may
read this code but not legally copy, modify, fork or redistribute it. If you want
to use it or build on it, [open an
issue](https://github.com/shubh-garg18/passbook/issues) and ask.

The three bundled fonts are separate: they are SIL Open Font License 1.1, and
their licence ships beside them in
[`frontend/src/fonts/OFL.txt`](frontend/src/fonts/OFL.txt) as that licence
requires.

---

Built by **Shubh Garg** ([Bitwise](https://github.com/shubh-garg18)) ·
[github.com/shubh-garg18/passbook](https://github.com/shubh-garg18/passbook)
