# passbook — make targets.  SPEC §5, §7, §11, §22.
#
# `make setup` is the front door for a new install; everything else assumes it
# has run. Targets are deliberately thin wrappers: the logic lives in the CLI
# and in scripts/, so the web UI and a Windows user without make can reach it.

SHELL       := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

COMPOSE  := docker compose
ENV_FILE := .env
URL      := http://localhost:8080

.PHONY: help setup preflight env check up down logs ps backup verify-backup \
        verify-ledger upgrade audit-docs dr-drill backup-remote backup-passphrase \
        restore web-password web-totp web-build test doctor sync parse payees \
        fixtures demo-ledger _needs_file

# Everything a launcher or a wizard runs must work without `uv`, because a
# Windows user installs Docker Desktop and Python and nothing else. PY is the
# interpreter those scripts run under.
PY := $(shell command -v python3 2>/dev/null || command -v python 2>/dev/null)

help:
	@echo "passbook — bank statements -> Firefly III"
	@echo
	@echo "  make setup    FIRST RUN: check prerequisites, start the stack, get a token"
	@echo "  make preflight  what is missing, and where to get it"
	@echo
	@echo "  make env      generate .env with fresh secrets (refuses to overwrite)"
	@echo "  make check    verify prerequisites and .env before anything runs"
	@echo "  make up       start the stack, wait for healthy, print the URL"
	@echo "  make down     stop and remove containers (volumes are kept)"
	@echo "  make logs     follow logs"
	@echo "  make ps       container and health status"
	@echo "  make backup   pg_dump + config tarball to backups/"
	@echo "  make verify-backup   prove the newest backup restores (scratch container)"
	@echo "  make backup-remote   encrypt + push off-machine (needs rclone, see README)"
	@echo "  make dr-drill        rebuild the ledger from encrypted archives alone"
	@echo "  make backup-passphrase  create the GPG passphrase (refuses to clobber)"
	@echo "  make restore  FILE=backups/... CONFIRM=yes   (destructive)"
	@echo
	@echo "  make upgrade  apply pending migrations (backs up first)"
	@echo "  make audit-docs  tracked docs must cite fixture values, never a real ledger"
	@echo
	@echo "  make test     run the test suite"
	@echo "  make verify-ledger   check the live ledger against archive/ (SPEC §20)"
	@echo "  make doctor   check token, reachability and target account"
	@echo "  make sync     push everything in inbox/, archive on success"
	@echo "  make parse    FILE=inbox/x.xls   parse + validate, no writes"
	@echo "  make payees   FILE=inbox/x.xls   rank payee tokens for rules.yaml"
	@echo "  make fixtures FILE=inbox/x.xls   regenerate redacted test fixtures"
	@echo "  make web-password    set the web UI password (keeps the second factor)"
	@echo "  make web-totp        show second-factor status; --reset when the phone is lost"
	@echo "  make web-build       build the React bundle locally (docker build does this too)"

# ── first run ────────────────────────────────────────────────────────────────
# SPEC §22.4. One command from a fresh clone to a working ledger. It is
# re-runnable and never overwrites a secret, so it is also the right thing to
# run when something is half-configured.

setup:
	@$(PY) scripts/setup.py

preflight:
	@$(PY) scripts/preflight.py

# ── .env generation ──────────────────────────────────────────────────────────
# Secrets are alphanumeric on purpose: docker compose interpolates these values
# into docker-compose.yml, so a '$$' or '#' in a password would break the stack
# in a confusing way.

env:
	@if [ -e "$(ENV_FILE)" ]; then
		echo "refusing to overwrite an existing $(ENV_FILE)."
		echo "APP_KEY is in there; replacing it makes Firefly's encrypted fields unreadable."
		exit 1
	fi
	# Bounded read: `tr -dc < /dev/urandom | head -c 32` makes head close the
	# pipe while tr is still draining, and pipefail turns that SIGPIPE into a
	# build failure. Feeding tr a fixed 1 KiB lets it finish and exit 0.
	# Secrets are generated before the copy so a failure leaves no partial .env.
	key=$$(head -c 1024 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 32)
	pw=$$(head -c 1024 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 40)
	[ $${#key} -eq 32 ] && [ $${#pw} -eq 40 ] || { echo "secret generation failed"; exit 1; }
	cp .env.example "$(ENV_FILE)"
	chmod 600 "$(ENV_FILE)"
	sed -i "s|^APP_KEY=.*|APP_KEY=$$key|"        "$(ENV_FILE)"
	sed -i "s|^DB_PASSWORD=.*|DB_PASSWORD=$$pw|" "$(ENV_FILE)"
	secret=$$(head -c 1024 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 48)
	sed -i "s|^PASSBOOK_WEB_SECRET=.*|PASSBOOK_WEB_SECRET=$$secret|" "$(ENV_FILE)"
	echo "wrote $(ENV_FILE) (mode 600) with a fresh APP_KEY and DB_PASSWORD."
	echo "FIREFLY_TOKEN and PASSBOOK_ACCOUNT_NUMBER stay blank until Phase 0 steps 4-6."

# ── prerequisite checks ──────────────────────────────────────────────────────
# SPEC §4: this gates the other targets. Phase 0 items that need a running
# Firefly (token, INR currency) are checked by `passbook doctor` in Phase 3,
# not here — there is nothing to ask yet.

check:
	@fail=0
	if [[ "$(CURDIR)" == /mnt/* ]]; then
		echo "FAIL  repo is on $(CURDIR)"
		echo "      Windows drive mounts break Postgres file permissions. Move under ~/. (SPEC D8)"
		fail=1
	else
		echo "ok    repo is on the Linux filesystem"
	fi

	if ! command -v docker >/dev/null 2>&1; then
		echo "FAIL  docker not installed — install docker-ce inside the distro, not Docker Desktop (SPEC D8)"
		fail=1
	elif ! docker info >/dev/null 2>&1; then
		echo "FAIL  docker daemon not reachable — try: sudo service docker start"
		echo "      (if that says permission denied: sudo usermod -aG docker \$$USER, then restart the distro)"
		fail=1
	else
		echo "ok    docker daemon reachable ($$(docker version --format '{{.Server.Version}}'))"
	fi

	if ! docker compose version >/dev/null 2>&1; then
		echo "FAIL  docker compose plugin missing — install docker-compose-plugin"
		fail=1
	else
		echo "ok    compose plugin ($$(docker compose version --short))"
	fi

	if [ ! -f "$(ENV_FILE)" ]; then
		echo "FAIL  no $(ENV_FILE) — run: make env"
		fail=1
	else
		if [ "$$(stat -c '%a' $(ENV_FILE))" != "600" ]; then
			echo "warn  $(ENV_FILE) is mode $$(stat -c '%a' $(ENV_FILE)); 600 is safer (chmod 600 $(ENV_FILE))"
		fi
		set -a; . ./$(ENV_FILE); set +a
		for v in APP_KEY DB_DATABASE DB_USERNAME DB_PASSWORD TZ APP_URL; do
			if [ -z "$${!v:-}" ]; then
				echo "FAIL  $(ENV_FILE): $$v is empty"
				fail=1
			fi
		done
		if [ -n "$${APP_KEY:-}" ] && [ $${#APP_KEY} -ne 32 ]; then
			echo "FAIL  APP_KEY is $${#APP_KEY} chars; Firefly requires exactly 32"
			fail=1
		fi
		for v in APP_KEY DB_PASSWORD; do
			case "$${!v:-}" in
				*'$$'*|*'#'*|*'"'*|*"'"*)
					echo "FAIL  $$v contains a character that breaks compose interpolation (\$$ # \" ')"
					fail=1 ;;
			esac
		done
		if [ "$${SITE_OWNER:-}" = "you@example.com" ]; then
			echo "warn  SITE_OWNER is still the .env.example placeholder"
		fi
		# Web UI credentials live in config/web-auth.json since §15.5, and carry
		# the second factor since §16. Checked here because a mangled file
		# otherwise surfaces only as "sign-in failed" on a page that cannot say why.
		auth=config/web-auth.json
		if [ ! -f "$$auth" ]; then
			echo "FAIL  $$auth is missing — run: make web-password"
			fail=1
		else
			if [ "$$(stat -c %a "$$auth")" != "600" ]; then
				echo "warn  $$auth is mode $$(stat -c %a "$$auth"), expected 600"
			fi
			if ! grep -qE '"username"[[:space:]]*:[[:space:]]*"[^"]+"' "$$auth"; then
				echo "FAIL  $$auth has no username — run: make web-password"
				fail=1
			elif ! grep -qE '"password_hash"[[:space:]]*:[[:space:]]*"(scrypt|pbkdf2):' "$$auth"; then
				echo "FAIL  $$auth has no usable Werkzeug hash — run: make web-password"
				fail=1
			else
				echo "ok    web credentials present in $$auth"
			fi
			if grep -qE '"totp_secret"[[:space:]]*:[[:space:]]*"[A-Z2-7]+"' "$$auth"; then
				# Counted out of the `backup_codes` array, NOT by grepping for
				# 64-hex strings. That grep also matched the remembered-DEVICE
				# digests, which are the same shape: measured, 8 codes + 2 devices
				# reported as "10 backup code(s) left". It over-counts by exactly
				# the number of remembered devices, and it does so in the direction
				# that SUPPRESSES the warning — with 2 codes and 2 devices it prints
				# 4 and says nothing, at precisely the point where being told
				# matters.
				codes=$$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("backup_codes") or []))' "$$auth" 2>/dev/null || echo -1)
				# Warns from 2, not from 0. Codes are single-use so the count only
				# falls, and at zero a lost phone means RESET=yes is the only door
				# left — which is too late to be told. Matches webauth.LOW_BACKUP_CODES.
				if [ "$$codes" -lt 0 ]; then
					echo "warn  could not read the backup-code count from $$auth"
				elif [ "$$codes" -eq 0 ]; then
					echo "warn  second factor enrolled but NO backup codes left"
					echo "      lose the phone now and 'make web-totp RESET=yes' is the only way in"
				elif [ "$$codes" -le 2 ]; then
					echo "warn  only $$codes backup code(s) left — re-issue a set from Account"
					echo "      they are single-use, and at zero the phone is the only factor left"
				else
					echo "ok    second factor enrolled, $$codes backup code(s) left"
				fi
			else
				echo "warn  second factor not enrolled — the next sign-in will require it"
			fi
		fi
		if [ -n "$${PASSBOOK_WEB_PASSWORD_HASH_B64:-}$${PASSBOOK_WEB_PASSWORD_HASH:-}$${PASSBOOK_WEB_USER:-}" ]; then
			echo "warn  PASSBOOK_WEB_* still in $(ENV_FILE); dead since §15.5 — safe to delete"
		fi

		if [ -n "$${FIREFLY_TOKEN:-}" ] && [ "$$(printf %s "$$FIREFLY_TOKEN" | tr -cd . | wc -c)" != "2" ]; then
			echo "warn  FIREFLY_TOKEN is not shaped like a JWT (expect ~1000 chars, 'eyJ', 2 dots)"
			echo "      the 'Command line token' is a different credential and will not work"
		fi
		[ $$fail -eq 0 ] && echo "ok    $(ENV_FILE) looks sane" || true
	fi

	mkdir -p inbox archive backups
	echo "ok    inbox/ archive/ backups/ present"

	# ── the one irreversible mistake in a recovery ───────────────────────────
	# A recovered APP_KEY sitting on disk that disagrees with .env means the
	# tarball has been extracted but step 5 has not been finished. Starting
	# Firefly now is NOT a harmless experiment: on first boot it cannot decrypt
	# the stored Passport keypair, so restoreKeysFromDB catches the
	# DecryptException, DELETES both key settings and regenerates them. The
	# original keypair is then gone from the database and putting the right
	# APP_KEY back afterwards has nothing left to decrypt — the API token is
	# dead permanently.
	#
	# This is a guard, not a warning, because the moment you most want to type
	# `make up` to see whether it worked is exactly the moment it is unsafe.
	for keyfile in recovery/app-key.env config/recovery/app-key.env; do
		[ -f "$$keyfile" ] || continue
		recovered=$$(grep -m1 '^APP_KEY=' "$$keyfile" | cut -d= -f2-)
		current=$$(grep -m1 '^APP_KEY=' "$(ENV_FILE)" 2>/dev/null | cut -d= -f2-)
		if [ -n "$$recovered" ] && [ "$$recovered" != "$$current" ]; then
			echo "FAIL  $$keyfile holds a DIFFERENT APP_KEY than $(ENV_FILE)."
			echo
			echo "      You are mid-recovery: the config tarball is extracted but the"
			echo "      recovered APP_KEY has not been copied into $(ENV_FILE) yet."
			echo
			echo "      Do NOT start the stack first to see if it works. Firefly cannot"
			echo "      decrypt the stored Passport keypair with the wrong key, so it"
			echo "      deletes and regenerates it on the very first boot. That is"
			echo "      irreversible: restoring the right APP_KEY afterwards leaves"
			echo "      nothing to decrypt, and your API token is dead for good."
			echo
			echo "      Copy it across first:"
			echo "        sed -i \"s|^APP_KEY=.*|$$(cat $$keyfile)|\" $(ENV_FILE)"
			echo
			echo "      Then re-run: make check"
			echo
			echo "      If you have deliberately started fresh and do not want the old"
			echo "      key, delete the recovered copy instead:  rm $$keyfile"
			fail=1
		else
			echo "ok    recovered APP_KEY in $$keyfile matches $(ENV_FILE)"
		fi
	done

	if [ $$fail -ne 0 ]; then
		echo
		echo "check failed."
		exit 1
	fi
	echo
	echo "all checks passed."

# ── stack ────────────────────────────────────────────────────────────────────

# Two stages, and the reason is not cosmetic. docker-compose.yml declares
# `FIREFLY_TOKEN: ${FIREFLY_TOKEN:?…}` on the web service, so compose refuses to
# start ANYTHING until a token exists — and the token can only be created from
# inside a running Firefly. A single `up` on a fresh install therefore fails
# with an interpolation error about a variable the user has never heard of.
# So: database and Firefly first, then the rest once there is a token.
up: check
	@set -a; . ./$(ENV_FILE); set +a
	if [ -z "$${FIREFLY_TOKEN:-}" ]; then
		$(COMPOSE) up -d --wait db app
		echo
		echo "Firefly III is up at $(URL) — but the web UI is NOT started yet."
		echo "It needs an API token, and a token can only be made from inside Firefly."
		echo
		echo "Run 'make setup' to be walked through it, or do it by hand:"
		echo "  1. register at $(URL) (the first account becomes the admin)"
		echo "  2. Options -> Preferences -> set the currency to INR"
		echo "  3. Options -> Remote access and tokens -> Personal Access Tokens"
		echo "     (NOT the 'Command line token' on the Profile page — different credential)"
		echo "  4. put it in .env as FIREFLY_TOKEN, then run 'make up' again"
		exit 0
	fi
	$(COMPOSE) up -d --wait
	echo
	$(COMPOSE) ps --format 'table {{.Service}}\t{{.Status}}'
	echo
	echo
	# The weekly download depends on the operator remembering (D7: no cron), so
	# the reminder has to appear somewhere they already go. `|| true` keeps a
	# fresh clone that has not run `uv sync` yet from failing `make up`.
	uv run passbook sync-age 2>/dev/null || true
	echo
	echo "Firefly III is up at $(URL)"
	echo "Web UI is up at        http://localhost:8081"
	echo "First run: register an account there, set currency to INR (Options -> Preferences),"
	echo "then create a Personal Access Token under Options -> Remote access and tokens."
	echo "(NOT the 'Command line token' on the Profile page - different credential.)"

down:
	@$(COMPOSE) down
	echo "containers removed; pgdata and fireflyupload volumes kept."

logs:
	@$(COMPOSE) logs -f --tail=100

ps:
	@$(COMPOSE) ps

# ── parser and push (Phases 2-3) ─────────────────────────────────────────────
# parse/payees/test are read-only and never touch the network.
# doctor/sync talk to Firefly; sync writes.

# SPEC §20. The check §19's incident showed was missing: everything else can pass
# while Firefly holds a third of the ledger.
verify-ledger:
	@uv run passbook verify-ledger

# SPEC §22.2. `git pull` is silent, so this is what stands between a user who
# pulled a migration and a ledger holding two incompatible id forms.
#
# The backup is a PRECONDITION, not advice — the same shape §18.7 put in front
# of the re-apply button, and for the same reason: a migration re-pushes the
# ledger, which starts with a delete.
upgrade:
	@if uv run passbook upgrade --check; then
		exit 0
	elif [ $$? -ne 3 ]; then
		exit 1
	fi
	echo
	echo "Taking a database dump before touching anything."
	$(MAKE) --no-print-directory backup
	echo
	uv run passbook upgrade

# CLAUDE.md non-negotiable 14. A one-time scrub that nothing enforces is undone
# by the next phase, so this runs in `make test` as well.
audit-docs:
	@uv run pytest tests/test_docs.py -q

test:
	@uv run pytest -q

doctor:
	@uv run passbook doctor

# Prints .env lines; stores no plaintext anywhere. SPEC §14.
web-password:
	@uv run passbook web-password

# Recovery path for the second factor. `RESET=yes` clears TOTP and the backup
# codes so the next sign-in enrols a new secret — the way back in when the
# phone is gone and the backup codes are used up.  SPEC §16.2.
web-totp:
	@if [ "$${RESET:-}" = "yes" ]; then
		uv run passbook web-totp --reset
	elif [ "$${FORGET_DEVICES:-}" = "yes" ]; then
		uv run passbook web-totp --forget-devices
	else
		uv run passbook web-totp
		echo
		echo "  make web-totp RESET=yes            clear TOTP and re-enrol at next sign-in"
		echo "  make web-totp FORGET_DEVICES=yes   revoke every remembered device"
	fi

# Only needed to run the UI outside Docker; `docker compose build web` runs the
# same command in a throwaway Node stage, so the runtime image never has Node.
web-build:
	@command -v npm >/dev/null || { echo "npm not found — use: docker compose build web"; exit 1; }
	cd frontend && npm ci --no-audit --no-fund && npm run build
	echo "built -> src/passbook/web/dist" 

# Archives each file only after its push succeeds. Safely re-runnable.
sync:
	@uv run passbook sync

_needs_file:
	@if [ -z "$(FILE)" ] || [ ! -f "$(FILE)" ]; then
		echo "usage: make $(TARGET) FILE=inbox/Acnt_stmt__*.xls"
		exit 1
	fi

parse: TARGET=parse
parse: _needs_file
	@uv run passbook parse "$(FILE)"

# Regenerates payees.md. The Alias column comes from config/payee_aliases.yaml
# (which the UI writes), so it cannot drift; your own columns are preserved.
payees: TARGET=payees
payees: _needs_file
	@uv run passbook payees "$(FILE)" --top 500 --out payees.md

# Rewrites tests/fixtures/ from a real statement, then rebuilds the golden file.
# Review the diff before committing — this is the one place real data flows
# toward the repo. SPEC §11.
fixtures: TARGET=fixtures
fixtures: _needs_file
	@uv run python scripts/redact.py "$(FILE)" tests/fixtures/statement.xls --audit
	uv run python scripts/redact.py "$(FILE)" tests/fixtures/statement.csv --audit
	uv run python scripts/redact.py "$(FILE)" tests/fixtures/statement.html --audit
	# The PDF fixture is rendered from this same redacted grid and encrypted
	# RC4-40, like the real export. SPEC §6.8; scripts/pdfwrite.py.
	uv run python scripts/redact.py "$(FILE)" tests/fixtures/statement.pdf --audit
	uv run python -m tests.regenerate_golden

# ── backup / restore ─────────────────────────────────────────────────────────
# SPEC §11. These dumps are plaintext financial history and live only on this
# laptop. backups/ is gitignored; keep it that way.

# Two artefacts, because they hold different things. Firefly's rules live in the
# database and come back with the dump. config/*.yaml does NOT — aliases are
# applied at push time, never stored server-side, so that file is the only copy
# of the token->name mapping. It is gitignored (it names real counterparties),
# which leaves it the one piece of state with no other backup at all.
backup:
	@set -a; . ./$(ENV_FILE); set +a
	mkdir -p backups
	stamp="$$(date +%F)"
	out="backups/firefly-$$stamp.sql.gz"
	cfg="backups/config-$$stamp.tar.gz"

	# ── source, as a git bundle ──────────────────────────────────────────────
	# Done FIRST, before the dump, so a failure here costs nothing and leaves
	# no half-written backup behind.
	#
	# `git bundle create --all` packs every ref and the whole history into one
	# file, so the repo stops being the one recovery input with no second copy.
	# It rides inside the config tarball, which `make backup-remote` encrypts,
	# so source and ledger end up in the same archive under the same passphrase
	# — and no second hosting account is needed.
	stage="$$(mktemp -d)"
	mkdir -p "$$stage/recovery"
	if git rev-parse --git-dir >/dev/null 2>&1; then
		# A bundle carries COMMITTED history only. Silently omitting today's
		# work is the failure mode worth being noisy about.
		dirty="$$(git status --porcelain 2>/dev/null || true)"
		if [ -n "$$dirty" ]; then
			echo "warn  working tree is dirty — the bundle carries committed history ONLY."
			echo "      these are NOT in the backup:"
			echo "$$dirty" | sed 's/^/        /'
			echo "      commit them and re-run \`make backup\` if you want them captured."
		fi
		git bundle create "$$stage/recovery/source.bundle" --all >/dev/null 2>&1 || {
			echo "FAIL  could not create the source bundle"; rm -rf "$$stage"; exit 1; }
		# TWO checks, because one is not enough.
		#
		# `git bundle verify` reads only the HEADER — the prerequisite list and
		# the refs. Measured: it reports "The bundle records a complete history"
		# on a bundle whose packfile has been overwritten with garbage. So it
		# catches a truncated or malformed bundle and nothing else.
		#
		# A clone is what actually inflates every object. On the same corrupted
		# file it dies with "pack has bad object at offset ...: inflate returned
		# -3". Bare, into a temp dir, so it costs about a second.
		#
		# It needs a repository for context, which is why both run here rather
		# than in the recovery script.
		if ! git bundle verify "$$stage/recovery/source.bundle" >"$$stage/verify.log" 2>&1; then
			echo "FAIL  \`git bundle verify\` rejected the bundle — not shipping a corrupt"
			echo "      source archive. Nothing was written; the previous backup stands."
			sed 's/^/        /' "$$stage/verify.log"
			rm -rf "$$stage"; exit 1
		fi
		probe="$$stage/probe.git"
		if ! git clone -q --bare "$$stage/recovery/source.bundle" "$$probe" >"$$stage/clone.log" 2>&1; then
			echo "FAIL  the bundle does not clone — its packfile is corrupt."
			echo "      \`git bundle verify\` passed, which it does on a damaged pack;"
			echo "      only a clone inflates the objects. Nothing was written."
			tail -3 "$$stage/clone.log" | sed 's/^/        /'
			rm -rf "$$stage"; exit 1
		fi
		if [ "$$(git -C "$$probe" rev-parse HEAD)" != "$$(git rev-parse HEAD)" ]; then
			echo "FAIL  bundle HEAD does not match the working repo. Nothing was written."
			rm -rf "$$stage"; exit 1
		fi
		rm -rf "$$probe" "$$stage/verify.log" "$$stage/clone.log"
		echo "ok    source bundle verified (header + full clone) — $$(du -h "$$stage/recovery/source.bundle" | cut -f1), $$(git rev-list --count HEAD) commit(s) @ $$(git rev-parse --short HEAD)"
	else
		echo "warn  not a git repository — no source bundle in this backup"
	fi

	# --no-owner --no-privileges makes the dump portable. Without them pg_dump
	# emits `ALTER ... OWNER TO firefly` and `GRANT ... TO firefly`, so the
	# restore fails with `role "firefly" does not exist` on any machine where
	# DB_USERNAME differs. The DR drill caught exactly that.
	$(COMPOSE) exec -T -e PGPASSWORD="$$DB_PASSWORD" db \
		pg_dump -U "$$DB_USERNAME" -d "$$DB_DATABASE" \
		--clean --if-exists --no-owner --no-privileges \
		| gzip > "$$out"
	chmod 600 "$$out"
	echo "wrote $$out ($$(du -h "$$out" | cut -f1))"
	# APP_KEY rides along. The DR drill measured what it is worth: the ledger
	# restores perfectly without it (nothing in the data is encrypted), but the
	# Passport keypair in the `configuration` table IS Crypt::encrypt'd with it,
	# so a different key means every existing API token is rejected and has to
	# be re-issued by hand. The tarball is already encrypted, so carrying the
	# key costs nothing and removes a manual step from recovery.
	# Only APP_KEY: DB_PASSWORD is replaced on a rebuild anyway, and
	# FIREFLY_TOKEN is a credential with no recovery value.
	printf 'APP_KEY=%s\n' "$$APP_KEY" > "$$stage/recovery/app-key.env"
	chmod 600 "$$stage/recovery/app-key.env"
	# config/*.yaml only — config/web-auth.json is EXCLUDED ON PURPOSE.
	#
	# The yaml files are backed up because they are irreplaceable: aliases and
	# rules are months of operator knowledge that exists nowhere else, and
	# nothing can re-derive "this ten-character token is a night canteen".
	#
	# web-auth.json is the opposite. It holds a password hash, a TOTP secret
	# and backup-code digests — none of which carry information, all of which
	# you can recreate in two minutes with `make web-password` and a fresh
	# enrolment. Carrying them off-site would put a LIVE second factor into the
	# Drive archive beside the password hash it is supposed to be independent
	# of, so one passphrase compromise would collapse two factors to none.
	# Two minutes of recovery work is a cheaper price. SPEC §16.9.
	if compgen -G 'config/*.yaml' >/dev/null; then
		tar czf "$$cfg" config/*.yaml -C "$$stage" recovery
	else
		echo "warn  no config/*.yaml to back up"
		tar czf "$$cfg" -C "$$stage" recovery
	fi
	rm -rf "$$stage"
	# Assert the decision on the real artefact rather than trusting the glob.
	# If a future edit widens it to config/*, this fails the backup instead of
	# silently shipping credentials off-machine.
	if tar tzf "$$cfg" | grep -qi 'web-auth'; then
		echo "FAIL  web-auth.json is inside $$cfg — credentials must not go off-site."
		echo "      See the disaster-recovery runbook in README."
		rm -f "$$cfg"
		exit 1
	fi
	chmod 600 "$$cfg"
	bundle_size=$$(tar tzvf "$$cfg" | awk '/source\.bundle/{printf "%.1fM", $$3/1048576}')
	echo "wrote $$cfg ($$(du -h "$$cfg" | cut -f1); $$(tar tzf "$$cfg" | grep -c . ) entries)"
	echo "      APP_KEY yes | source bundle $${bundle_size:-ABSENT} | web credentials no"

# A dump that has never been restored is not a backup. This proves it, without
# touching the live database. SPEC §11.
verify-backup:
	@./scripts/verify_backup.sh $(FILE)

# Recovery from ONLY what survives this machine dying: the encrypted archives
# plus the passphrase. Builds a parallel stack on its own network; never touches
# the live one. SPEC §11.
dr-drill:
	@./scripts/dr_drill.sh

# Encrypts before upload, so the remote holds ciphertext only. The passphrase
# lives outside the repo and outside .env, and is not recoverable. SPEC §11.
backup-remote:
	@./scripts/backup_remote.sh

# Refuses to overwrite an existing passphrase: doing so would make every
# archive already uploaded permanently undecryptable, with no error. SPEC §11.
backup-passphrase:
	@./scripts/backup_remote.sh --init-passphrase

restore:
	@if [ -z "$(FILE)" ] || [ ! -f "$(FILE)" ]; then
		echo "usage: make restore FILE=backups/firefly-YYYY-MM-DD.sql.gz CONFIRM=yes"
		exit 1
	fi
	if [ "$(CONFIRM)" != "yes" ]; then
		echo "This REPLACES the current Firefly database with $(FILE)."
		echo "Re-run with CONFIRM=yes if that is what you want."
		exit 1
	fi
	set -a; . ./$(ENV_FILE); set +a
	gunzip -c "$(FILE)" | $(COMPOSE) exec -T -e PGPASSWORD="$$DB_PASSWORD" db \
		psql -v ON_ERROR_STOP=1 -U "$$DB_USERNAME" -d "$$DB_DATABASE" >/dev/null
	echo "restored $(FILE)"

	# The matching config tarball, if it was taken. Named by the same date as
	# the dump, so the pair stays together.
	cfg="$$(echo "$(FILE)" | sed 's|/firefly-|/config-|; s|\.sql\.gz$$|.tar.gz|')"
	if [ -f "$$cfg" ]; then
		# Never silently clobber the live mapping — the current one may be
		# newer than the dump being restored.
		if compgen -G 'config/*.yaml' >/dev/null; then
			aside="backups/config-replaced-$$(date +%F-%H%M%S).tar.gz"
			tar czf "$$aside" config/*.yaml
			chmod 600 "$$aside"
			echo "current config/*.yaml saved to $$aside"
		fi
		tar xzf "$$cfg"
		echo "restored $$cfg -> config/"
	else
		echo "warn  no $$cfg alongside the dump; config/*.yaml left as-is."
		echo "      Firefly's rules came back with the database, but aliases did not."
	fi
	echo "note: APP_KEY in $(ENV_FILE) must be the one that was in use when this dump was taken."
