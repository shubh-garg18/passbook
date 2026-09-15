# Backups and recovery

[← README](../README.md) · [What is this?](what-is-this.md) · [Setup](../SETUP.md) · [Usage](usage.md) · **Backups** · [Operations](operations.md) · [What changed](../CHANGELOG.md)

> **These four are the ones with no native-Windows equivalent** — they are shell
> scripts that source `.env` and drive `pg_dump`. Run them from WSL2, or from
> macOS or Linux. [Why](../SETUP.md#every-command-on-windows).

```bash
make backup           # dump + config tarball -> backups/
make verify-backup    # prove the newest one restores, in a throwaway container
make backup-remote    # encrypt both and push off-machine
make dr-drill         # rebuild from the encrypted archives alone
```

---

## Two artefacts

| File | Holds | Second copy? |
|---|---|---|
| `ledger-<date>.sql.gz` | every row, with its category and tags | no |
| `config-<date>.tar.gz` | `config/*.yaml`, the source as a git bundle | no |

The rows come back with the dump. **Your aliases and rules do not** — they are
applied when a row is written and never stored beside it, so
`payee_aliases.yaml` and `rules.yaml` are the only copies of that knowledge
anywhere.

`config/web-auth.json` is **excluded on purpose**, and `make backup` fails if it
ever ends up inside. The yaml is irreplaceable; the credential file carries no
information and takes two minutes to recreate. Including it would put a live
second factor beside the password hash it exists to be independent of.

## A dump nobody has restored is a file, not a backup

```bash
make verify-backup                                          # newest vs live
make verify-backup FILE=backups/ledger-2026-07-01.sql.gz    # an older one
```

Loads the dump into a throwaway Postgres container, checks the ledger
reconstructs — rows, distinct ids, balance, earnings — then tears it down. **It
never touches your live database.**

It is negative-tested against a corrupt gzip stream *and* an intact-looking
truncated dump. A verifier that cannot fail proves nothing.

The `FILE` form checks self-consistency instead of equality with live, since an
older backup legitimately holds fewer rows. Storage rots quietly; run it
occasionally.

## Off-site

`make backup-remote` encrypts both artefacts with GPG AES256 **before anything
leaves the machine**, then pushes them via rclone. The remote holds ciphertext
only. Each archive is decrypted and byte-compared before upload.

> ### Losing the passphrase loses the backups
>
> No recovery path, no key escrow, nobody to ask. **Put it in a password manager
> the moment you create it.**
>
> Overwriting it is as bad as losing it and much quieter — the archives just
> stop opening. Create it only with `make backup-passphrase`, which refuses to
> clobber an existing file. Never use a `>` redirect.
>
> It lives at `~/.config/passbook/backup-passphrase`, mode 600 — outside the
> repo and outside `.env`, because a passphrase stored next to the thing it
> protects is not protecting much.

Three steps need a human, once:

1. `make backup-passphrase` — prints it once; save it.
2. `rclone config` → new remote → `drive` → scope `3` (`drive.file`, so rclone
   only sees files it created) → browser OAuth consent, which cannot be
   scripted.
3. `PASSBOOK_RCLONE_REMOTE="gdrive:passbook-backups"` in `.env`.

Retention is bounded at both ends by `PASSBOOK_BACKUP_KEEP` (default 14).
`./scripts/backup_remote.sh --local-only` skips the upload — useful for a USB
stick.

---

## Recovery is drilled, not assumed

```bash
make dr-drill
```

Rebuilds the ledger from the encrypted archives and the passphrase alone, on its
own docker network, never touching the live stack. Expected figures come from
`archive/` and `.env`, so it is not specific to one machine.

### What you need if the machine is gone

| # | Input | Where | If lost |
|---|---|---|---|
| 1 | the two `.gpg` archives | your remote | the ledger is gone |
| 2 | the GPG passphrase | your password manager | the archives are unreadable, permanently |
| 3 | a machine with Docker | — | rebuildable in an hour |
| 4 | the source | GitHub **and inside archive 1** | nothing |

`make backup` puts `git bundle create --all` into the config tarball, so **GitHub
is a convenience, not a dependency**. That leaves 1 and 2 as the only inputs with
no second copy.

The bundle is verified twice, because once is not enough:

| Check | Catches |
|---|---|
| `git bundle verify` | a truncated or malformed bundle |
| a bare `git clone` | a corrupt **packfile** — every object is inflated |

Measured: after overwriting sixteen bytes mid-packfile, `git bundle verify` still
reported *"complete history"*. Only the clone caught it. Either failing aborts
the backup before anything is written, so the previous one stands.

### The runbook

```bash
# 1. tools — no repository needed yet, the source is in the archive
sudo apt install docker-ce docker-compose-plugin gnupg rclone git

# 2. pull the archives back
mkdir -p ~/recovery && cd ~/recovery
rclone config && rclone copy gdrive:passbook-backups .

# 3. decrypt with the OLD passphrase from your password manager
for f in *.gpg; do gpg --decrypt --output "${f%.gpg}" "$f"; done

# 4. unpack — this is where the source comes from
tar xzf config-<date>.tar.gz     # config/*.yaml, recovery/source.bundle

# 5. clone from the bundle, then repoint origin at GitHub
git clone recovery/source.bundle passbook && cd passbook
git remote set-url origin https://github.com/shubh-garg18/passbook.git

# 6. move the recovered files in
mkdir -p backups
cp ../ledger-*.sql.gz ../config-*.tar.gz backups/
cp ../config/*.yaml config/ && cp -r ../recovery .

# 7. environment
make env
#    then restore PASSBOOK_ACCOUNT_NUMBER and PASSBOOK_ASSET_ACCOUNT by hand

# 8. up, and load the ledger
make up
make restore FILE=backups/ledger-<date>.sql.gz CONFIRM=yes
#    the UI now says "Not set up yet" — correct, see step 9

# 9. web access is deliberately not in the backup
make web-password        # then sign in and enrol a new authenticator

# 10. verify
make verify-backup && uv run passbook doctor && uv run passbook verify-ledger
```

### There is no longer an irreversible mistake here

There used to be one, and it is worth knowing it is gone. The ledger lived in a
separate application that encrypted part of its own configuration with a key in
`.env`. Restore the dump, boot once on a generated key, and it silently deleted
and regenerated the keypair — your API token dead, permanently, with the right
key still sitting in the archive beside it. `make check` had to **refuse** to
start the stack to prevent it.

Nothing in these tables is encrypted. Restore the dump with whatever credentials
you like; the rows read back either way.

### What survives

| | |
|---|---|
| Transactions, balances, categories, tags, rules | **yes** |
| `payee_aliases.yaml`, `rules.yaml`, `accounts.yaml` | **yes** — the tarball is the only copy |
| Web password, TOTP, backup codes, remembered browsers | **no** — deliberate; re-run `make web-password` |
| `DB_PASSWORD` | **no** — regenerated, and nothing depends on the old one |
| Files in `inbox/` and `archive/` | **no** — re-download if needed |

### Last resort: only the dump

The money is still there, and reading it needs nothing from this project. The
dump is plain SQL over three tables:

```bash
docker run -d --name pg -e POSTGRES_USER=you -e POSTGRES_PASSWORD=x \
  -e POSTGRES_DB=passbook postgres:16-alpine
gunzip -c ledger-<date>.sql.gz | docker exec -i pg psql -U you -d passbook
docker exec -i pg psql -U you -d passbook -c \
  'select txn_date, description, kind, amount, category from passbook.transactions
     order by txn_date'
```

`passbook.transactions` is one row per transaction, `passbook.asset_accounts`
holds each account's opening balance, and `passbook.transaction_tags` the tags.
Any tool that speaks SQL can read all three. What you lose is the *pipeline*,
not the ledger.

> Worth a line in your password manager beside the passphrase:
> **`postgres:16-alpine`**. A newer image will read this dump; an older one may
> not.

**Archives taken before `--no-owner --no-privileges` was added** carry
`ALTER ... OWNER TO <user>` and need a role of that name to restore. Re-take a
backup you can rely on.
