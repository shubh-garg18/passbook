#!/usr/bin/env bash
# Prove a backup actually restores. SPEC §11.
#
# A dump that has never been restored is not a backup, it is a file. This
# restores the newest one into a THROWAWAY Postgres container and checks the
# ledger reconstructs. The live database is never touched — no `make restore`,
# no writes to the running stack, nothing shared but the dump file itself.
#
# The expected figures are read from the LIVE database rather than hardcoded, so
# the check keeps working as the ledger grows. It is asserting "the restored
# copy is indistinguishable from what is running", which is the property that
# matters. The absolute numbers are printed too, so a wrong-but-consistent pair
# is still visible to a human.
#
#   make verify-backup

set -euo pipefail

SCRATCH=passbook_verify_backup
PGIMAGE=postgres:16-alpine
BACKUPS=backups
EXPECTED_CONFIG=(
    config/payee_aliases.example.yaml
    config/payee_aliases.yaml
    config/rules.example.yaml
    config/rules.yaml
    recovery/app-key.env
)

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

tmpdir="$(mktemp -d)"
cleanup() {
    docker rm -f "$SCRATCH" >/dev/null 2>&1 || true
    rm -rf "$tmpdir"
}
trap cleanup EXIT

# The postgres image runs a TEMPORARY server on a unix socket while initdb
# bootstraps, then shuts it down and starts the real one. pg_isready succeeds
# against that temporary server, so a naive readiness loop races the restart and
# the next command dies with "the database system is shutting down". Require
# several consecutive successful queries instead.
wait_for_pg() {
    local container="$1" user="$2" db="$3" streak=0
    for _ in $(seq 1 120); do
        if docker exec "$container" psql -qtAX -U "$user" -d "$db" -c 'select 1' >/dev/null 2>&1; then
            streak=$((streak + 1))
            [ "$streak" -ge 3 ] && return 0
        else
            streak=0
        fi
        sleep 1
    done
    return 1
}

fail() { echo "  FAIL  $*"; failures=$((failures + 1)); }
ok()   { echo "  ok    $*"; }
failures=0

# --- the queries. Same text against live and restored, so a difference can
#     only come from the data. -------------------------------------------------
# The account name is interpolated here by bash, not by psql. psql's backtick
# substitution would expand inside the database container, where the CLI's
# environment does not exist — it silently yields an empty name and two of the
# four queries then return nothing at all.
acct_sql="${PASSBOOK_ASSET_ACCOUNT//\'/\'\'}"   # double any quote, SQL-style

read -r -d '' CHECKS <<SQL || true
\\set acct '${acct_sql}'
select count(*) from journal_meta m
  join transaction_journals j on j.id=m.transaction_journal_id
  where m.name='external_id' and m.deleted_at is null and j.deleted_at is null;
select count(distinct m.data) from journal_meta m
  join transaction_journals j on j.id=m.transaction_journal_id
  where m.name='external_id' and m.deleted_at is null and j.deleted_at is null;
select to_char(sum(t.amount),'FM9999999.00') from transactions t
  join transaction_journals j on j.id=t.transaction_journal_id
  where t.account_id=(select id from accounts where name=:'acct')
    and t.deleted_at is null and j.deleted_at is null;
select to_char(sum(t.amount),'FM9999999.00') from transactions t
  join transaction_journals j on j.id=t.transaction_journal_id
  join journal_meta m on m.transaction_journal_id=j.id
       and m.name='external_id' and m.deleted_at is null
  where t.account_id=(select id from accounts where name=:'acct')
    and t.amount > 0 and t.deleted_at is null and j.deleted_at is null
    and not exists (
      select 1 from tag_transaction_journal tj join tags g on g.id=tj.tag_id
      where tj.transaction_journal_id=j.id and g.tag='not-earnings');
SQL

# With no argument: take a fresh backup and require it to match the live ledger
# exactly. That is the strong check and the one `make verify-backup` runs.
#
# With a dump path: verify that file instead, and check self-consistency rather
# than equality with live — an older backup legitimately holds fewer rows, so
# comparing it to today's ledger would report a failure that is not one. This
# is what catches a file that has rotted on disk.
GIVEN="${1:-}"

if [ -n "$GIVEN" ]; then
    echo "== 1. verifying an existing backup (no fresh dump taken) =="
    [ -f "$GIVEN" ] || { echo "  FAIL  no such file: $GIVEN"; exit 1; }
    dump="$GIVEN"
    cfg="$(echo "$dump" | sed 's|/firefly-|/config-|; s|\.sql\.gz$|.tar.gz|')"
    [ -f "$cfg" ] || cfg=""
else
    echo "== 1. take a fresh backup =="
    make --no-print-directory backup | sed 's/^/  /'
    dump="$(ls -1t "$BACKUPS"/firefly-*.sql.gz 2>/dev/null | head -1 || true)"
    cfg="$(ls -1t "$BACKUPS"/config-*.tar.gz 2>/dev/null | grep -v 'config-replaced-' | head -1 || true)"
fi
[ -n "$dump" ] || { echo "  FAIL  no dump produced"; exit 1; }
echo "  using $dump"
echo "  using ${cfg:-<none>}"

# Corruption usually shows up here first, before Postgres is even involved.
gzip -t "$dump" 2>/dev/null || { echo "  FAIL  $dump is not a valid gzip stream"; exit 1; }
echo "  gzip integrity ok"

echo
if [ -z "$GIVEN" ]; then
    echo "== 2. read expected figures from the live database =="
    live="$(docker compose exec -T -e PGPASSWORD="$DB_PASSWORD" db \
            psql -qtAX -U "$DB_USERNAME" -d "$DB_DATABASE" <<< "$CHECKS")"
    mapfile -t L <<< "$live"
    [ "${#L[@]}" -eq 4 ] || { echo "  FAIL  live query returned ${#L[@]} rows, expected 4:"; echo "$live" | sed 's/^/    /'; exit 1; }
    printf '  txns=%s  distinct_ext_id=%s  balance=%s  earnings=%s\n' "${L[0]}" "${L[1]}" "${L[2]}" "${L[3]}"
else
    echo "== 2. skipped — an existing backup is checked for self-consistency =="
fi

echo
echo "== 3. restore into a throwaway container (live db untouched) =="
docker rm -f "$SCRATCH" >/dev/null 2>&1 || true
docker run -d --name "$SCRATCH" \
    -e POSTGRES_USER="$DB_USERNAME" \
    -e POSTGRES_PASSWORD="verify-only-$$" \
    -e POSTGRES_DB="$DB_DATABASE" \
    "$PGIMAGE" >/dev/null
echo "  started $SCRATCH ($PGIMAGE), no volume, no published port"

wait_for_pg "$SCRATCH" "$DB_USERNAME" "$DB_DATABASE" \
    || { echo "  FAIL  scratch postgres never became ready"; exit 1; }

if ! gunzip -c "$dump" 2>/dev/null | docker exec -i "$SCRATCH" \
        psql -q -v ON_ERROR_STOP=1 -U "$DB_USERNAME" -d "$DB_DATABASE" >/dev/null 2>&1; then
    echo "  FAIL  the dump did not load cleanly into a fresh database"
    exit 1
fi
echo "  loaded the dump"

echo
echo "== 4. compare restored against live =="
restored="$(docker exec -i "$SCRATCH" \
            psql -qtAX -U "$DB_USERNAME" -d "$DB_DATABASE" <<< "$CHECKS")"
mapfile -t R <<< "$restored"
[ "${#R[@]}" -eq 4 ] || { echo "  FAIL  restored query returned ${#R[@]} rows, expected 4:"; echo "$restored" | sed 's/^/    /'; exit 1; }
names=(transactions distinct_external_ids balance earnings)
if [ -z "$GIVEN" ]; then
    for i in 0 1 2 3; do
        if [ "${L[$i]}" = "${R[$i]}" ]; then
            ok "${names[$i]}: ${R[$i]}"
        else
            fail "${names[$i]}: restored ${R[$i]}, live ${L[$i]}"
        fi
    done
else
    # No live baseline to compare an older dump against, so assert the ledger
    # is present and internally coherent instead.
    [ "${R[0]}" -gt 0 ] 2>/dev/null && ok "transactions: ${R[0]}" \
        || fail "restored ledger has no transactions"
    [ "${R[0]}" = "${R[1]}" ] && ok "external_ids all distinct: ${R[1]}" \
        || fail "duplicate external_ids: ${R[0]} rows, ${R[1]} distinct"
    [[ "${R[2]}" =~ ^-?[0-9]+\.[0-9]{2}$ ]] && ok "balance reconstructs: ${R[2]}" \
        || fail "balance did not reconstruct: '${R[2]}'"
    [[ "${R[3]}" =~ ^-?[0-9]+\.[0-9]{2}$ ]] && ok "earnings reconstructs: ${R[3]}" \
        || fail "earnings did not reconstruct: '${R[3]}'"
fi

echo
echo "== 5. config tarball =="
if [ -z "$cfg" ]; then
    fail "no config-*.tar.gz alongside the dump — aliases have no backup"
else
    tar xzf "$cfg" -C "$tmpdir"
    for want in "${EXPECTED_CONFIG[@]}"; do
        [ -f "$tmpdir/$want" ] && ok "$want" || fail "$want missing from $cfg"
    done
    extra="$(cd "$tmpdir" && find config -type f | sort | comm -13 <(printf '%s\n' "${EXPECTED_CONFIG[@]}" | sort) -)"
    [ -z "$extra" ] || echo "  note  also present: $(echo "$extra" | tr '\n' ' ')"
    # The aliases are the piece with no other copy anywhere; prove it is usable,
    # not merely present.
    grep -q '^APP_KEY=.\{32\}$' "$tmpdir/recovery/app-key.env" 2>/dev/null \
        && ok "APP_KEY carried (32 chars) — API tokens survive a rebuild" \
        || fail "APP_KEY missing or malformed in the archive"
    n="$(python3 -c "import yaml,sys; d=yaml.safe_load(open(sys.argv[1])) or {}; print(len(d.get('aliases') or {}))" \
         "$tmpdir/config/payee_aliases.yaml" 2>/dev/null || echo 0)"
    [ "$n" -gt 0 ] && ok "payee_aliases.yaml parses, $n alias(es)" \
                   || fail "payee_aliases.yaml did not parse or is empty"
fi

echo
if [ "$failures" -eq 0 ]; then
    if [ -z "$GIVEN" ]; then
        echo "BACKUP VERIFIED — $dump restores to a ledger identical to live."
    else
        echo "BACKUP VERIFIED — $dump restores to a coherent ledger."
        echo "  (file mode: checked for self-consistency, not compared against live)"
    fi
else
    echo "BACKUP NOT PROVEN — $failures check(s) failed."
    exit 1
fi
