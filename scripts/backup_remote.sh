#!/usr/bin/env bash
# Encrypt the local backups and push them off-machine. SPEC §11.
#
# Everything else in this project lives on one laptop disk: the Postgres volume,
# backups/, config/payee_aliases.yaml, .env. A single disk failure loses all of
# it at once. This is the only copy that survives that.
#
# The archives are encrypted BEFORE they leave, with GPG symmetric AES256, so
# the remote holds ciphertext only. Google Drive never sees a balance, a
# counterparty name, or an account number.
#
#   make backup-remote                  encrypt, upload, prune
#   make backup-passphrase              create the passphrase (refuses to clobber)
#   ./scripts/backup_remote.sh --local-only    encrypt and verify, no upload
#
# THE PASSPHRASE IS NOT RECOVERABLE. It is deliberately not in the repo and not
# in .env — .env is read by `make` and by docker compose, and a passphrase that
# sits next to the thing it protects is not protecting much. If you lose it,
# every archive on the remote is permanently unreadable. Put it in a password
# manager now, not later.

set -euo pipefail
cd "$(dirname "$0")/.."

LOCAL_ONLY=0
INIT_PASS=0
case "${1:-}" in
    --local-only)      LOCAL_ONLY=1 ;;
    --init-passphrase) INIT_PASS=1 ;;
esac

set -a; [ -f .env ] && . ./.env; set +a

PASSPHRASE_FILE="${PASSBOOK_PASSPHRASE_FILE:-$HOME/.config/passbook/backup-passphrase}"
REMOTE="${PASSBOOK_RCLONE_REMOTE:-}"
KEEP="${PASSBOOK_BACKUP_KEEP:-14}"
BACKUPS=backups

die() { echo "  FAIL  $*" >&2; exit 1; }

# --- guarded passphrase creation --------------------------------------------
# Overwriting an existing passphrase is the most destructive thing in this
# repo: every archive already uploaded becomes permanently undecryptable,
# silently, with no error at the time and nothing to fall back on. So the only
# sanctioned way to create one refuses outright if the file exists, and the
# instructions below point here instead of at a `>` redirect, which clobbers
# without a word.
if [ "$INIT_PASS" -eq 1 ]; then
    if [ -e "$PASSPHRASE_FILE" ]; then
        echo "  REFUSING to overwrite $PASSPHRASE_FILE" >&2
        echo >&2
        echo "  A passphrase already exists there ($(stat -c '%s' "$PASSPHRASE_FILE") bytes," >&2
        echo "  mode $(stat -c '%a' "$PASSPHRASE_FILE"), modified $(stat -c '%y' "$PASSPHRASE_FILE" | cut -d. -f1))." >&2
        echo >&2
        echo "  Replacing it would make every archive already uploaded permanently" >&2
        echo "  undecryptable. Nothing would report an error - the old ciphertext simply" >&2
        echo "  stops opening, and no copy of the old passphrase would remain." >&2
        echo >&2
        echo "  To rotate deliberately: move the old file aside yourself, keep it until" >&2
        echo "  every old archive is re-encrypted or discarded, then re-run this." >&2
        exit 1
    fi
    mkdir -p "$(dirname "$PASSPHRASE_FILE")"
    ( umask 077; head -c 1024 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 40 > "$PASSPHRASE_FILE" )
    chmod 600 "$PASSPHRASE_FILE"
    echo "created $PASSPHRASE_FILE (mode 600)"
    echo
    echo "  COPY THIS INTO YOUR PASSWORD MANAGER NOW. Losing it makes every"
    echo "  uploaded archive permanently unreadable."
    echo
    echo "    $(cat "$PASSPHRASE_FILE")"
    exit 0
fi

echo "== preflight =="

# --- passphrase -------------------------------------------------------------
if [ ! -f "$PASSPHRASE_FILE" ]; then
    cat >&2 <<EOF
  FAIL  no passphrase file at $PASSPHRASE_FILE

  Create one with:

      make backup-passphrase

  It refuses to overwrite an existing file. Do NOT generate it with a plain
  `> $PASSPHRASE_FILE` redirect: if a passphrase is already there, that destroys
  it without a word and every archive in Drive becomes permanently
  undecryptable.

  Losing it makes every uploaded archive permanently unreadable. There is no
  recovery path and no one to ask.
EOF
    exit 1
fi
case "$(realpath "$PASSPHRASE_FILE")" in
    "$(realpath .)"/*) die "passphrase file is inside the repo — move it out" ;;
esac
[ "$(stat -c '%a' "$PASSPHRASE_FILE")" = "600" ] \
    || die "passphrase file is mode $(stat -c '%a' "$PASSPHRASE_FILE"); needs 600"
[ -s "$PASSPHRASE_FILE" ] || die "passphrase file is empty"
echo "  ok    passphrase file present, mode 600, outside the repo"

command -v gpg >/dev/null || die "gpg not installed (apt install gnupg)"
echo "  ok    gpg $(gpg --version | head -1 | awk '{print $3}')"

if [ "$LOCAL_ONLY" -eq 0 ]; then
    command -v rclone >/dev/null || cat >&2 <<'EOF'
  FAIL  rclone not installed.

  Manual steps, both of which need you at a browser:

      1. sudo apt install rclone          (or: curl https://rclone.org/install.sh | sudo bash)
      2. rclone config
           n) new remote
           name> gdrive
           storage> drive                 (Google Drive)
           client_id / client_secret> blank is fine for personal use
           scope> 1  (full access) or 3 (drive.file — only files rclone creates)
           Use auto config? > Y, which opens a browser for the Google OAuth consent
      3. Put the remote in .env:
           PASSBOOK_RCLONE_REMOTE="gdrive:passbook-backups"

  Then re-run `make backup-remote`. Nothing here can be automated — the OAuth
  consent is interactive by design.
EOF
    command -v rclone >/dev/null || exit 1
    echo "  ok    rclone $(rclone version | head -1 | awk '{print $2}')"
    [ -n "$REMOTE" ] || die "PASSBOOK_RCLONE_REMOTE is not set (e.g. gdrive:passbook-backups)"
    rclone lsd "${REMOTE%%:*}:" >/dev/null 2>&1 \
        || die "rclone cannot reach '${REMOTE%%:*}:' — run: rclone config"
    echo "  ok    remote $REMOTE reachable"
fi

# --- fresh backup -----------------------------------------------------------
echo
echo "== take a fresh backup =="
make --no-print-directory backup | sed 's/^/  /'
stamp="$(date +%F)"
dump="$BACKUPS/firefly-$stamp.sql.gz"
cfg="$BACKUPS/config-$stamp.tar.gz"
[ -f "$dump" ] || die "expected $dump"

# --- encrypt ----------------------------------------------------------------
echo
echo "== encrypt (AES256, symmetric) =="
encrypted=()
for f in "$dump" "$cfg"; do
    [ -f "$f" ] || continue
    out="$f.gpg"
    gpg --batch --yes --quiet --symmetric --cipher-algo AES256 \
        --pinentry-mode loopback --passphrase-file "$PASSPHRASE_FILE" \
        -o "$out" "$f"
    chmod 600 "$out"

    # An archive that has never been decrypted is not a backup either. Prove the
    # round trip now, while the plaintext is still here to compare against.
    if ! gpg --batch --quiet --decrypt --pinentry-mode loopback \
             --passphrase-file "$PASSPHRASE_FILE" "$out" 2>/dev/null \
             | cmp -s - "$f"; then
        die "$out did not decrypt back to $f"
    fi
    echo "  ok    $(basename "$out")  ($(du -h "$out" | cut -f1), decrypts identically)"
    encrypted+=("$out")
done

# --- upload -----------------------------------------------------------------
if [ "$LOCAL_ONLY" -eq 1 ]; then
    echo
    echo "== upload skipped (--local-only) =="
else
    echo
    echo "== upload =="
    for f in "${encrypted[@]}"; do
        rclone copy --no-traverse "$f" "$REMOTE/"
        echo "  ok    uploaded $(basename "$f")"
    done
    echo "  remote now holds:"
    rclone ls "$REMOTE/" | tail -6 | sed 's/^/    /'
fi

# --- retention --------------------------------------------------------------
# Bounded on purpose: a daily backup of a ~70 KB dump is small, but nothing
# here ever deletes anything otherwise and free Drive tiers are finite.
echo
echo "== retention (keep $KEEP of each) =="
prune() {
    local pattern="$1" n=0
    while IFS= read -r old; do
        rm -f "$old"; n=$((n + 1))
    done < <(ls -1t $pattern 2>/dev/null | tail -n +$((KEEP + 1)))
    [ "$n" -gt 0 ] && echo "  pruned $n local $(basename "$pattern")" || true
}
prune "$BACKUPS/firefly-*.sql.gz"
prune "$BACKUPS/config-*.tar.gz"
prune "$BACKUPS/firefly-*.sql.gz.gpg"
prune "$BACKUPS/config-*.tar.gz.gpg"
prune "$BACKUPS/config-replaced-*.tar.gz"
echo "  local backups: $(ls -1 "$BACKUPS" 2>/dev/null | wc -l) file(s)"

if [ "$LOCAL_ONLY" -eq 0 ]; then
    # Same bound remotely. rclone's min-age is the simplest expression of it
    # that does not need a second listing pass.
    rclone delete --min-age "${KEEP}d" "$REMOTE/" 2>/dev/null || true
    echo "  remote: anything older than ${KEEP}d removed"
fi

echo
echo "done. Remote holds ciphertext only — the passphrase never leaves this machine."
