# What changed

[← README](README.md) · [What is this?](docs/what-is-this.md) · [Setup](SETUP.md) · [Usage](docs/usage.md) · [Backups](docs/backups.md) · [Operations](docs/operations.md) · **What changed**

Newest first. Every entry says what it means for you, not what the diff did.

**The app tells you when there is a new version** — it is on the Status page,
with this list — and `make update` applies it. On Windows that is a
double-click on `launchers\update-passbook.cmd`.

---

## 0.3.1 — the buttons on the Status page work now

Nothing about your ledger changed. Three things on the Status page could only
ever say no, and now they answer.

- **"Back up now" works.** It said *"Cannot back up from here"* on every
  install — the container was never given the folder to write into, or the
  database password to use. Both are wired up. The button writes the same
  ledger dump and config archive `make backup` writes.
- **That matters beyond backups.** Re-apply refuses to run without a backup
  from the last hour, so the most destructive thing in the app was only
  reachable if you opened a terminal first. Now it is not.
- **"Check GitHub now."** The update check answers from an hour-old cache so
  that opening the page is not a request to GitHub every time. The button asks
  straight away, for when you have just updated and want to see it land.
- **Commands read as commands.** Messages like *run `make up`* were showing you
  the backtick characters.
- **A missing space on "Add a bank."** The sentence ran two words together
  right where it tells you what to do if your bank is not one of the built-in
  ones.
- **Reports shows whole amounts on a phone.** Every figure in the category
  breakdown was losing its last character, and the category totals showed only
  their first few digits — the content was wider than the screen and the part
  that did not fit could not be scrolled to.
- **The account switcher fits on a phone.** With three or more accounts the
  chips wrap onto two rows instead of the last one being cut off at the edge.
- **`make dr-drill` runs again.** The disaster-recovery drill — the one that
  proves you could rebuild from your encrypted backups alone — had been
  stopping right after the decrypt step without saying why, since 0.3.0
  renamed the dump files. If you have never run it, now is a good time.

Upgrading is `make update` as always. On Windows, double-click
`launchers\update-passbook.cmd`.

---

## 0.3.0 — the ledger moved in-house

**If you are upgrading, one command does it:** `make update`. It backs up,
pulls, rebuilds, rebuilds your ledger from `archive/`, and checks every row
against your statements. [What happens →](docs/usage.md#coming-from-the-version-that-used-firefly-iii)

### Firefly III is gone

passbook keeps the ledger in its own tables now, on the database that was
already in the stack.

- **Setup asks two questions instead of six.** No second application to
  install, no account to register, no API token to create and paste, no
  currency to set. The stack is three containers instead of four.
- **A transaction cannot be stored twice.** Its identity is the table's primary
  key, not a check something could skip. The old store decided duplicates on a
  hash of what was *sent*, and passbook rewrites what it sends whenever you
  rename a payee — seven rows once went in twice with nothing raised anywhere.
- **An update cannot move money.** The ledger refuses a write that names an
  amount, a date or a direction. It used to be safe only because the other
  application's update happened to be sparse.
- **One irreversible recovery mistake is gone.** The old ledger encrypted part
  of its own configuration with a key in `.env`; restoring a backup with the
  wrong key silently destroyed every API token, and `make check` had to refuse
  to start the stack to prevent it. Nothing here is encrypted.
- **The time of day survives.** It used to be re-read from your statements on
  every page load, because the old store had nowhere to keep it.

### It stays fast with years in it

Every window, filter and search is a database query rather than a loop over
everything you have. Measured on a synthetic ten-year ledger:

| | before | after |
|---|---|---|
| this month's analysis | 1,039 ms | 140 ms |
| this month's transactions | 1,887 ms | 141 ms |
| searching everything | 1,890 ms | 164 ms |

It also fixed a bug: **any window but "everything" was reporting zero spend.**

### New

- **Activity** — *Account menu → What has changed*. Every import, rename,
  category, deletion, backup and update, newest first. It exists for the
  question a week later: *why does this read differently?*
- **Version** — Status says whether a newer passbook has been published and
  lists what changed in it.
- **A welcome on first run.** The Ledger page used to open, before you had done
  anything, with `BALANCE unavailable` in red and an instruction to edit a file
  on the host. It now says the one thing there is to do.

### Smaller

- Backups are named `ledger-<date>.sql.gz`. Old `firefly-*` dumps are still
  found, listed and pruned.
- An error no longer prints the same sentence twice.
- The screenshots in the README are a fifth of the size and a readable shape.

---

## 0.2.0 and earlier

Not kept here — the history starts with the release that changed how your data
is stored, because that is the first one where "what changed for you" needed an
answer longer than a commit subject. `git log` has the rest, and
[DECISIONS.md](DECISIONS.md) has every decision with the measurement behind it.
