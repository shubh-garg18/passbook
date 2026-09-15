#!/usr/bin/env bash
# Disaster-recovery drill.
#
# Everything verify_backup.sh proves is proved by the machine that still holds
# the originals. This proves the harder thing: recovery from ONLY what survives
# that machine dying — the two encrypted archives and the passphrase.
#
# Nothing here touches the live stack. It builds a parallel universe on its own
# docker network: a scratch Postgres loaded from the encrypted dump, and a
# passbook container that has never seen this machine\'s .env or config.
#
# **It used to have a fourth step this one does not need.** The ledger was a
# separate application that encrypted part of its own configuration with a key
# in .env, so recovery had a question attached to it — is that key load-bearing
# — and the answer was yes: restoring with a different one silently destroyed
# every API token. Nothing in these tables is encrypted, so that whole leg of
# the drill, and the failure it was drilling for, are gone.
#
#   make dr-drill

set -euo pipefail
cd "$(dirname "$0")/.."

NET=passbook_dr_net
PG=passbook_dr_db
WEB=passbook_dr_web
WEBPORT=8098
PGIMAGE=postgres:16-alpine
# The web image is ours, so the drill uses whatever `docker compose build web`
# last produced. Steps 6-8 skip loudly rather than silently if it is absent.
WEBIMAGE=$(grep -oE 'passbook/web:[^ ]+' docker-compose.yml | head -1)

# Deliberately NOT the live database name or user: the dump carries no
# credentials, and a recovery onto different ones is the realistic case.
DR_DB=passbook_dr
DR_USER=dr_recovery
DR_PASS="dr-$(head -c 256 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 24)"

tmp="$(mktemp -d)"
cleanup() {
    docker rm -f "$WEB" "$PG" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    rm -rf "$tmp"
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

say() { echo "  $*"; }
fail() { echo "  FAIL  $*"; failures=$((failures+1)); }
ok()   { echo "  ok    $*"; }
failures=0

# ── what the restored ledger must look like ──────────────────────────────────
# Derived from archive/ and .env, never hardcoded. An earlier version of this
# script carried one particular ledger's row count, closing balance and asset
# account name as literals, so it could only ever pass on the machine it was
# written on — and it would have reported FAIL three times on a fresh clone
# while nothing was actually wrong.
expect="$(uv run python - <<'PY' 2>/dev/null || true
from passbook import service

statements = service.archived_statements()
if statements:
    newest = max(statements, key=lambda s: (s.meta.period_to, s.path.stat().st_mtime))
    ids = {t.txn_id for s in statements for t in s.transactions}
    print(len(ids), newest.meta.closing_balance)
PY
)"
EXPECT_TXNS="${expect%% *}"
EXPECT_BAL="${expect##* }"
ASSET_ACCOUNT="$(grep -m1 '^PASSBOOK_ASSET_ACCOUNT=' .env 2>/dev/null | cut -d= -f2- | tr -d "\"'")"
if [ -z "$expect" ] || [ -z "$ASSET_ACCOUNT" ]; then
    echo "  FAIL  cannot derive what a recovered ledger should hold."
    echo "        archive/ needs at least one pushed statement and .env must set"
    echo "        PASSBOOK_ASSET_ACCOUNT. Run a sync before drilling recovery —"
    echo "        there is nothing to recover otherwise."
    exit 1
fi
say "expecting $EXPECT_TXNS row(s) and a balance of $EXPECT_BAL on \"$ASSET_ACCOUNT\""
say "(read from archive/ and .env, so this drill is not machine-specific)"
echo

echo "== 0. simulate the only surviving inputs =="
# Encrypt the current backups with a throwaway passphrase, then forget the
# plaintext exists. From here on the drill may only read the .gpg files.
pass="$tmp/passphrase"
head -c 1024 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 40 > "$pass"
chmod 600 "$pass"
dump_plain="$(ls -1t backups/ledger-*.sql.gz backups/firefly-*.sql.gz 2>/dev/null | grep -v '\.gpg$' | head -1)"
cfg_plain="$(ls -1t backups/config-*.tar.gz 2>/dev/null | grep -v '\.gpg$' | grep -v 'config-replaced-' | head -1)"
[ -n "$dump_plain" ] || { echo "  FAIL  no backup to drill with; run make backup"; exit 1; }
for f in "$dump_plain" "$cfg_plain"; do
    [ -f "$f" ] || continue
    gpg --batch --yes --quiet --symmetric --cipher-algo AES256 --pinentry-mode loopback \
        --passphrase-file "$pass" -o "$tmp/$(basename "$f").gpg" "$f"
done
say "inputs: $(cd "$tmp" && ls *.gpg | tr '\n' ' ') + passphrase"
say "NOT used from here on: the live database, .env, config/, or any plaintext backup"

echo
echo "== 1. decrypt, as a new machine would =="
for g in "$tmp"/*.gpg; do
    gpg --batch --quiet --decrypt --pinentry-mode loopback --passphrase-file "$pass" \
        -o "${g%.gpg}" "$g"
    ok "decrypted $(basename "${g%.gpg}")"
done
dump="$(ls -1 "$tmp"/ledger-*.sql.gz "$tmp"/firefly-*.sql.gz 2>/dev/null | head -1)"
cfg="$(ls -1 "$tmp"/config-*.tar.gz 2>/dev/null || true)"

echo
echo "== 2. stand up a scratch database and load the dump =="
docker network create "$NET" >/dev/null 2>&1 || true
docker run -d --name "$PG" --network "$NET" \
    -e POSTGRES_USER="$DR_USER" -e POSTGRES_PASSWORD="$DR_PASS" -e POSTGRES_DB="$DR_DB" \
    "$PGIMAGE" >/dev/null
wait_for_pg "$PG" "$DR_USER" "$DR_DB" || { echo "  FAIL  scratch postgres never became ready"; exit 1; }
ok "scratch postgres up (different name, user and password from live — the dump"
say "      carries no credentials, and a recovery onto different ones is the real case)"

gunzip -c "$dump" | docker exec -i "$PG" psql -q -v ON_ERROR_STOP=1 -U "$DR_USER" -d "$DR_DB" >/dev/null
ok "dump loaded"

echo
echo "== 3. is the ledger all there? =="
q() { docker exec -i "$PG" psql -qtAX -U "$DR_USER" -d "$DR_DB" -c "$1"; }
txns=$(q "select count(*) from passbook.transactions;")
bal=$(q "select to_char(a.opening_balance + coalesce(sum(case when t.kind='deposit' then t.amount else -t.amount end), 0), 'FM9999999.00')
           from passbook.asset_accounts a
           left join passbook.transactions t on t.account = a.name
          where a.name = '$ASSET_ACCOUNT'
          group by a.opening_balance;")
[ "$txns" = "$EXPECT_TXNS" ] && ok "$EXPECT_TXNS transactions present" \
                            || fail "expected $EXPECT_TXNS transactions, got $txns"
[ "$bal" = "$EXPECT_BAL" ] && ok "balance reads $EXPECT_BAL" \
                          || fail "balance reads $bal, expected $EXPECT_BAL"

# The identity is the primary key, so the restored table cannot hold a row
# twice — but the count above and the count below disagreeing would say the
# restore lost rows rather than duplicated them, which is the failure that
# actually happens.
ids=$(q "select count(distinct external_id) from passbook.transactions;")
[ "$ids" = "$txns" ] && ok "every row carries a distinct identity" \
                     || fail "$txns rows behind $ids identities"

echo "== 5. config archive =="
if [ -n "$cfg" ]; then
    tar xzf "$cfg" -C "$tmp"
    n=$(python3 -c "import yaml,sys;d=yaml.safe_load(open(sys.argv[1]))or{};print(len(d.get('aliases')or{}))" "$tmp/config/payee_aliases.yaml" 2>/dev/null || echo 0)
    [ "$n" -gt 0 ] && ok "payee_aliases.yaml recovered, $n alias(es)" || fail "aliases not recovered"
else
    fail "no config archive"
fi

echo
echo "== 5b. the source itself, out of the archive =="
# The repo used to be the one recovery input the archives could not carry.
# `make backup` now packs it as a git bundle inside the config tarball, so this
# leg proves you can get the code back from Drive + passphrase alone, with no
# GitHub account involved.
bundle="$tmp/recovery/source.bundle"
if [ ! -f "$bundle" ]; then
    fail "no recovery/source.bundle in the archive — run \`make backup\` to create one"
else
    say "bundle: $(du -h "$bundle" | cut -f1)"
    # A clone is the real verification. `git bundle verify` reads only the
    # header — measured: it passes on a bundle whose packfile has been
    # overwritten with garbage. Cloning inflates every object.
    if git clone -q "$bundle" "$tmp/recovered-src" >"$tmp/clone.log" 2>&1; then
        ok "cloned the repo from the bundle (no GitHub, no network)"
    else
        fail "clone from the bundle failed"
        tail -3 "$tmp/clone.log" | sed 's/^/      /'
    fi

    if [ -d "$tmp/recovered-src/.git" ]; then
        got=$(git -C "$tmp/recovered-src" rev-parse HEAD)
        want=$(git rev-parse HEAD)
        n=$(git -C "$tmp/recovered-src" rev-list --count HEAD)
        if [ "$got" = "$want" ]; then
            ok "HEAD matches the working repo — ${got:0:7}, $n commit(s)"
        elif git cat-file -e "$got" 2>/dev/null; then
            behind=$(git rev-list --count "$got..$want" 2>/dev/null || echo '?')
            ok "HEAD is ${got:0:7} ($n commits) — $behind commit(s) behind the working"
            say "      tree, i.e. the backup predates them. Not a fault."
        else
            fail "bundle HEAD ${got:0:7} is unknown to this repo — wrong or corrupt bundle"
        fi

        # Complete enough to actually work in, not just to contain files.
        missing=""
        for f in Makefile docker-compose.yml Dockerfile pyproject.toml \
                 src/passbook/cli.py scripts/dr_drill.sh frontend/package.json; do
            [ -f "$tmp/recovered-src/$f" ] || missing="$missing $f"
        done
        [ -z "$missing" ] && ok "checkout is complete (Makefile, compose, Dockerfile, src, scripts, frontend)" \
                          || fail "recovered checkout is missing:$missing"

        # `make check` must RUN — a fresh clone has no .env, so it is expected
        # to fail on that. What is being proved is that the Makefile and its
        # helpers survived, not that a bare checkout passes.
        checkout_log="$tmp/recovered-check.log"
        ( cd "$tmp/recovered-src" && make check ) >"$checkout_log" 2>&1 || true
        if grep -q "repo is on the Linux filesystem" "$checkout_log"; then
            ok "\`make check\` runs in the recovered tree and reaches its own checks"
            if grep -q "no .env — run: make env" "$checkout_log"; then
                say "      and correctly reports the one thing a fresh clone lacks:"
                say "      $(grep -m1 'no .env' "$checkout_log" | sed 's/^ *//')"
            fi
        else
            fail "\`make check\` did not run in the recovered tree"
            head -5 "$checkout_log" | sed 's/^/      /'
        fi
    fi
fi

echo
echo "== 6. web access after recovery: the credential file is NOT in the backup =="
# The backup excludes config/web-auth.json on purpose, so a recovered install
# has no web credentials at all. That is the GUARANTEED state after any real
# recovery, which makes it worth proving rather than assuming.
if ! docker image inspect "$WEBIMAGE" >/dev/null 2>&1; then
    fail "$WEBIMAGE not built — run: docker compose build web"
else
    # A recovered config/ — exactly what step 4 of the runbook extracts. Note
    # what is NOT here: web-auth.json.
    dr_config="$tmp/config"
    if [ -e "$dr_config/web-auth.json" ]; then
        fail "web-auth.json came out of the archive — it must never be backed up"
    else
        ok "recovered config/ contains no web-auth.json, as designed"
    fi

    docker run -d --name "$WEB" --network "$NET" -p "127.0.0.1:$WEBPORT:8081" \
        -v "$dr_config:/app/config" \
        -e PASSBOOK_DATABASE_URL="postgresql://$DR_USER:$DR_PASS@$PG:5432/$DR_DB" \
        -e PASSBOOK_ACCOUNT_NUMBER="${DR_ACCOUNT:-$(grep -m1 '^PASSBOOK_ACCOUNT_NUMBER=' .env | cut -d= -f2-)}" \
        -e PASSBOOK_ASSET_ACCOUNT="$ASSET_ACCOUNT" \
        -e PASSBOOK_WEB_SECRET="drill-$(head -c 256 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 24)" \
        -e TZ=Asia/Kolkata \
        "$WEBIMAGE" >/dev/null

    web() { curl -sS -c "$tmp/jar" -b "$tmp/jar" "$@"; }
    up=0
    for _ in $(seq 1 60); do
        code=$(curl -sS -o /dev/null -w '%{http_code}' "http://localhost:$WEBPORT/api/session" 2>/dev/null || echo 000)
        [ "$code" = "200" ] && { up=1; break; }
        sleep 1
    done
    if [ "$up" != "1" ]; then
        fail "web container never answered with no credential file — it should start cleanly"
        docker logs "$WEB" 2>&1 | tail -15 | sed 's/^/      /'
    else
        ok "web container starts cleanly with NO credential file (no crash, no stack trace)"
        sess=$(web "http://localhost:$WEBPORT/api/session")
        if echo "$sess" | grep -q '"configured":false'; then
            ok "GET /api/session reports configured:false — the SPA shows a setup page"
        else
            fail "session did not report configured:false: $sess"
        fi
        csrf=$(awk '/pb_csrf/{print $7}' "$tmp/jar" | tail -1)
        att=$(web -X POST -H 'Content-Type: application/json' -H "X-Passbook-CSRF: $csrf" \
              -d '{"username":"anyone","password":"anything"}' \
              "http://localhost:$WEBPORT/api/session")
        if echo "$att" | grep -q '"code":"not_configured"'; then
            ok "a sign-in attempt names the fix instead of a bare \"Sign-in failed.\""
            echo "$att" | sed 's/.*"error":"\([^"]*\)".*/      says: \1/' | cut -c1-100
        else
            fail "unconfigured sign-in is indistinguishable from a wrong password: $att"
        fi
    fi

    echo
    echo "== 7. walk runbook step 9: make web-password, enrol TOTP, sign in =="
    DRILL_USER=drill
    DRILL_PASS="drill-$(head -c 256 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 20)"
    if docker exec "$WEB" passbook web-password \
            --username "$DRILL_USER" --password "$DRILL_PASS" >/dev/null 2>&1; then
        ok "\`passbook web-password\` wrote credentials into the recovered config/"
    else
        fail "web-password failed inside the recovered install"
    fi
    [ -f "$dr_config/web-auth.json" ] && ok "config/web-auth.json created, mode $(stat -c %a "$dr_config/web-auth.json")" \
        || fail "web-auth.json was not created"

    rm -f "$tmp/jar"
    web "http://localhost:$WEBPORT/api/session" >/dev/null
    csrf=$(awk '/pb_csrf/{print $7}' "$tmp/jar" | tail -1)
    stage=$(web -X POST -H 'Content-Type: application/json' -H "X-Passbook-CSRF: $csrf" \
            -d "{\"username\":\"$DRILL_USER\",\"password\":\"$DRILL_PASS\"}" \
            "http://localhost:$WEBPORT/api/session")
    if echo "$stage" | grep -q '"stage":"enroll"'; then
        ok "password accepted; enrolment is mandatory, as designed"
    else
        fail "expected stage=enroll on a fresh credential, got: $stage"
    fi

    secret=$(web -X POST -H "X-Passbook-CSRF: $csrf" \
             "http://localhost:$WEBPORT/api/totp/enroll/start" \
             | python3 -c 'import json,sys;print(json.load(sys.stdin)["secret"])')
    [ -n "$secret" ] && ok "enrolment issued a secret and a QR" || fail "no secret from enrolment"
    code=$(python3 -c "import pyotp,sys;print(pyotp.TOTP(sys.argv[1]).now())" "$secret" 2>/dev/null \
           || uv run python -c "import pyotp,sys;print(pyotp.TOTP(sys.argv[1]).now())" "$secret")
    codes=$(web -X POST -H 'Content-Type: application/json' -H "X-Passbook-CSRF: $csrf" \
            -d "{\"code\":\"$code\"}" "http://localhost:$WEBPORT/api/totp/enroll/confirm")
    n=$(echo "$codes" | python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("backupCodes") or []))')
    [ "$n" = "8" ] && ok "authenticator confirmed; 8 backup codes issued once" \
                   || fail "expected 8 backup codes, got $n"

    echo
    echo "== 8. does the recovered UI read the recovered ledger? =="
    ov=$(web "http://localhost:$WEBPORT/api/overview")
    bal=$(echo "$ov" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("balance"))')
    if [ "$bal" = "$EXPECT_BAL" ]; then
        ok "signed in, and /api/overview reads balance $EXPECT_BAL from the restored ledger"
    else
        fail "recovered UI reported balance=$bal (ledger error: $(echo "$ov" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("ledgerError"))'))"
    fi
    pay=$(web "http://localhost:$WEBPORT/api/payees")
    cats=$(echo "$pay" | python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("categories") or []))')
    [ "$cats" -gt 0 ] && ok "recovered rules.yaml is live: $cats categor(y|ies) offered" \
                      || fail "no categories — rules.yaml did not survive"
fi

echo
echo "== verdict =="
if [ "$failures" -eq 0 ]; then
    echo "  RECOVERABLE from the encrypted archives + passphrase alone."
else
    echo "  $failures problem(s) — recovery is NOT proven."
    exit 1
fi
