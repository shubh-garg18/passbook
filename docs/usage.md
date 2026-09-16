# Using it

[← Front page](../README.md) · [What is this?](what-is-this.md) · [Set it up](SETUP.md) · [Backups](backups.md)

Everything here happens in the browser at **http://localhost:8081**.

---

## The weekly routine

**1. Download your statement** from your bank, the same file you would download
anyway.

**2. Drop it on Upload.** If the PDF has a password, it asks for it.

**3. Look at what it found.** It shows you every row it read and whether the
bank's own running balance adds up. Nothing is saved yet.

**4. Press the button.** Now it is saved.

Rows you already have are skipped, so overlapping downloads are fine — you
never have to be careful about dates.

---

## What the figures mean

This is the part worth reading once.

| | |
|---|---|
| **Spent** | money that actually left you |
| **Earned** | money that is actually yours |
| **Movement** | money that changed place but not owner — savings, investments, card bills, transfers between your own accounts |
| **Net change** | everything in, less everything out |

Moving money to your own savings is not spending. A refund is not income.
passbook takes those out of Spent and Earned, and shows the total it took out
so you can check it.

**Which categories count as movement is your decision**, not a built-in list.
It lives in `config/rules.yaml`, and the Ledger page names the categories it
excluded right under the figure.

---

## Naming people

Banks write payees like `UPI/9876543210/PAYTM...`. **Payees** turns that into a
name you choose.

Name it once and it stays named — including on rows already saved. Give it a
category too and future rows get it automatically.

passbook ships **no guessed merchant rules**. Banks cut payee names short, so a
guess is usually wrong, and a wrong rule is worse than none: it is confidently
wrong and invisible. You name what you actually see.

If you rename something later, the app updates the rows you already have, in
place. It only ever touches the name, the category and the tags — never an
amount, a date or a direction.

---

## More than one account

**Accounts → Add an account**, with a statement from that account.

Each keeps its own ledger. The tabs at the top switch between them, and
**Combine** shows any of them together. Adding one never touches another.

---

## Is it still right?

**Status** answers this. It compares what is saved against the statements you
have uploaded and says so plainly — and if it cannot check something, it says
*unverified* rather than showing a tick it has not earned.

If a figure ever looks wrong, that page is where to look first.

---

## Activity

**Activity** is the list of everything that has changed your ledger: every
import, rename, category change and deletion, with what it affected and when.

It exists for the question you ask a week later — *why does this read
differently?*

---

## A weekly reminder

**Reminder** makes a real calendar invitation. It arrives through your normal
calendar, so it still reaches you when the laptop is shut.

---

## Backups

**Status → Back up now** takes one. It saves your ledger and your settings.

Getting everything back — and checking that a backup really restores — needs a
few commands. [Backups and recovery →](backups.md)

---

## Updating

The app tells you when there is a new version, on **Status**.

To apply it, double-click **`update-passbook`** in the `launchers` folder. It
backs up first, then updates, then checks every row against your statements. If
anything fails it stops, and the version you had keeps running.

---

## If a statement is rejected

The message says why. The common ones:

**"The balance does not add up"** — a row was misread. Nothing is saved. Send
the message in an [issue](https://github.com/shubh-garg18/passbook/issues); it
names the row.

**"No reader understood this file"** — a bank passbook does not read yet, or a
different export from one it does. Say which bank in an issue.

**"This account is not set up"** — the statement belongs to an account you have
not added. **Accounts → Add an account**.

---

## The command line

There is one, and you never have to use it. Everything above is in the app.

If you want it: `uv run passbook --help`.
