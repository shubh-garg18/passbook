# Backups and getting it back

[← Front page](../README.md) · [Using it](usage.md)

Your ledger is on one computer. If that computer dies, a backup is the only
thing between you and typing it all in again.

---

## Taking one

**Status → Back up now.** That is the whole thing.

It writes two files into the `backups` folder:

| | |
|---|---|
| `ledger-<date>.sql.gz` | every transaction and account |
| `config-<date>.tar.gz` | your payee names, categories and rules |

Both matter. The ledger can be rebuilt from your statements; the names and
categories cannot — they are months of decisions that exist nowhere else.

**Do it before anything that rewrites a lot of rows.** The app asks for a
recent one before it will let you do that anyway.

---

## Getting it back

This is the part that needs a terminal, because rebuilding a database needs
Docker.

```bash
make restore FILE=backups/ledger-2026-01-31.sql.gz CONFIRM=yes
```

It replaces what is there with what is in the file, which is why it makes you
type `CONFIRM=yes`.

---

## Checking a backup actually works

A file you have never restored is not a backup. It is a file.

```bash
make verify-backup    # restores the newest one into a scratch database
make dr-drill         # rebuilds everything from the encrypted copies alone
```

`verify-backup` restores the dump somewhere harmless and compares it against
what is live. `dr-drill` goes further: it pretends this machine is gone, and
rebuilds from the off-site archives and a passphrase — including the app
itself, which travels inside the config archive.

Run the drill once when you set up off-site copies, so you find out then
instead of on the day it matters.

---

## Off-site

A backup on the same machine does not survive the machine.

```bash
make backup-remote
```

It encrypts both files with a passphrase you choose and uploads them to your
own cloud storage through [rclone](https://rclone.org/). The passphrase never
leaves your machine, and nothing readable does either.

**Keep the passphrase in your password manager.** Without it the archives are
noise — that is the point of them, and there is no way around it.

---

## What is not in a backup

Your passbook password and second factor, deliberately. They are quick to set
again, and keeping them beside the data would put both halves of the lock in
one box.

After restoring, set a password again and you are in.
