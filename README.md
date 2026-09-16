# passbook

**Your bank tells you what left your account. It does not tell you what you
spent.**

Moving money into your own savings leaves your account. A refund arrives in it.
Neither is spending, and neither is income — but every app counts both, so the
figure you are shown is movement, not money gone. On a real three-month
statement that read several times the true number.

passbook separates them, on your own computer, and shows you exactly what it
set aside so you can disagree with it. Nothing is uploaded anywhere: no account
to make, no company holding your statement, no monthly fee.

And it gives the people back their names. Your bank writes
`UPI/9876543210/PAYTM...`; you call it **Mess** once, and it is Mess
everywhere — on the rows you already have, and on every one that arrives after.
That is the difference between a list you scroll past and a page that answers
where the money went.

⭐ **If that sounds useful, star it.** passbook never phones home — there is no
install counter and there is not going to be one — so a star is the only way
this project can tell whether it is worth continuing.

> **New here, or not a developer?** → **[What is this?](docs/what-is-this.md)**
> — the whole idea in plain language, two minutes.

[![tests](https://github.com/shubh-garg18/passbook/actions/workflows/ci.yml/badge.svg)](https://github.com/shubh-garg18/passbook/actions/workflows/ci.yml)

## Install

You need [Docker](https://docs.docker.com/get-docker/). Then open a terminal
and run this line — it copies passbook into a folder called `passbook`:

```bash
git clone https://github.com/shubh-garg18/passbook.git
```

That one line is the only terminal you need. **Copy it this way, not as a ZIP**
— updates arrive with `git pull`, and a ZIP cannot be updated.

Then open the new folder and double-click the launcher for your system:

| | Double-click |
|---|---|
| **Windows** | `passbook\launchers\start-passbook.cmd` |
| **Mac** | `passbook/launchers/start-passbook.command` — right-click → Open the first time |
| **Linux** | `passbook/launchers/start-passbook.sh`, or `make setup` |

It sets everything up, asks you for one statement and a password, and opens
**http://localhost:8081**. Everything after that happens in the app.

**[Step by step, for your system →](docs/SETUP.md)**

> **Using Claude Code?** Paste this and it does the whole thing:
> `Clone https://github.com/shubh-garg18/passbook.git and set it up by following its SETUP.md.`

![The Ledger page](docs/screenshots/ledger.png)

![Reports](docs/screenshots/reports.png)

![Transactions](docs/screenshots/transactions.png)

<sub>Every picture here comes from a made-up statement in `tests/fixtures/`. The
names and the numbers are invented.</sub>

## Day to day

Three files, in the `launchers` folder inside the `passbook` folder you
downloaded. None of them is a terminal:

| | |
|---|---|
| **Start it** | double-click `start-passbook` |
| **Stop it** | double-click `stop-passbook` |
| **Update it** | double-click `update-passbook` — the app tells you when there is one |

<sub>On Windows those end in `.cmd`, on a Mac `.command`, on Linux `.sh`.</sub>

An update backs up first, then pulls, rebuilds, and checks every row against
your statements. If a step fails it stops, and the version you had keeps
running.

## Will it work for me?

| | |
|---|---|
| ✅ | **Canara, SBI and Union Bank** work out of the box |
| ✅ | Another bank? Send the statement layout and it gets added. [Open an issue](https://github.com/shubh-garg18/passbook/issues) |
| ✅ | Password-protected PDFs — the page asks for the password |
| ✅ | More than one account, in one view or separately |
| ⚠️ | You need a PC — Windows, macOS or Linux. Not a phone |
| ❌ | It cannot sync from your bank automatically. India's Account Aggregator system is closed to individuals, so nobody self-hosted can do that |

## What you get

| | |
|---|---|
| **Upload** | drop a statement in; it checks the arithmetic and shows you what it found before saving anything |
| **Ledger** | your balance, what you spent, what you earned, and where it went |
| **Transactions** | every row, searchable by payee, category, amount or date |
| **Reports** | by category, by payee, by tag, over time |
| **Payees** | name someone once and the name sticks, including on rows you already have |
| **Accounts** | add, rename, remove; see any of them together |
| **Activity** | what changed your ledger and when |
| **Reminder** | a calendar invitation, so it reaches you when the laptop is shut |
| **Backups** | one button; checked restores from the terminal |
| **Status** | is everything healthy, does it still match your statements |

## Why trust the numbers

- **It checks itself against your statements**, and says *unverified* rather
  than showing a tick it has not earned.
- **It ships no guessed merchant rules.** Banks cut payee names short, so a
  guessed rule is usually wrong. You write them from your own data.
- **Money is never a float**, anywhere, including in the database.
- **The same row cannot be stored twice** — that is the shape of the table, not
  a check something could skip.
- **Backups are drilled, not assumed.** One command rebuilds the whole ledger
  from the encrypted archives.

Every one of those is there because something went wrong once.
[The long version, with the measurements →](docs/DECISIONS.md)

## Help

- **Say which bank you have.** Canara, SBI and Union Bank are read today. If
  yours is not one of them, say so in an issue — that is how the next one gets
  added.
- [Open an issue](https://github.com/shubh-garg18/passbook/issues) when a step
  is wrong on your machine.

## Docs

| | |
|---|---|
| [What is this?](docs/what-is-this.md) | the idea, in plain language |
| [Setup](docs/SETUP.md) | install, first run, fixing things |
| [Using it](docs/usage.md) | the weekly routine, names, more than one account |
| [Backups](docs/backups.md) | backing up, and getting it all back |
| [Decisions](docs/DECISIONS.md) | every decision and the measurement behind it |
