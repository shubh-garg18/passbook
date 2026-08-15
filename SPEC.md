# passbook — Canara Bank → Firefly III ingest pipeline

**Version:** 8.0 — Phase 15: the public release (§22)
**Target environment:** Docker Desktop on Windows, macOS or Linux — one machine,
one operator, everything bound to `127.0.0.1`. Docker Engine inside WSL2 is the
author's own setup and stays documented as an alternative (D8).
**Status:** Phases 1–4, 6, 7, 9–15 complete. Phases 5 and 8 not started.

> **Figures in this file come from `tests/fixtures/statement.xls`**, never from a
> live ledger — CLAUDE.md non-negotiable 14, enforced by `make audit-docs`. The
> fixture is a real export with its metadata, narrations and amounts rewritten
> and its balance chain recomputed, so every count and every shape below is the
> bank's; only the values are synthetic. Where a lesson needs a magnitude, it is
> stated as a ratio.

> **v8.0 changelog.** §22: the repository becomes public. Documentation is
> scrubbed of live-ledger values and the rule is enforced by a test rather than
> remembered; a schema version and `make upgrade` mean a user who pulls a
> migration gets a backup and an ordered run rather than a broken ledger;
> AGPL-3.0 is chosen and the Firefly III licence question answered (§22.3); the
> setup path is rebuilt around Docker Desktop and a `make setup` wizard, because
> D8's three manual WSL2 steps are where a non-developer stops.

> **v7.1 changelog.** §19 records the 2026-08-11 partial re-push: the live
> ledger was found holding 21 of 93 rows with a self-consistent balance and no
> error anywhere, and the Phase 13 charts are what surfaced it. Recovery is
> written up as run — protect a pre-incident dump first, check for tombstones
> *before* pushing (a push into 72 tombstones reports "0 pushed, 72 duplicates"
> and reads as success), then rebuild from the archive. The cause is
> **unattributed**, and §19.2 lists what was ruled out rather than guessing. The
> fix — an intent-recorded, resumable purge — is specified in §19.7 and
> deliberately not built. Also: `large_oneoff.exclude_categories` is aligned with
> `not_spend`, and the two person-shaped categories gain a `family` tag while
> staying in spend.
>
> **v7.0 changelog.** Phase 13 (§18): six nav items become three, the Ledger
> page gains charts, and the palette gains the range it needed.
>
> **Every figure now goes through one function.** `service.ledger_analysis`
> applies §8 and §8.1, because until this phase those semantics lived only in
> the rules and every reader of a total had to remember them. Measured on one
> real three-month ledger, the naive by-type reading was **three times** the
> true spend and **1.6 times** the true earnings. `config/rules.yaml` gains
> `not_spend`, and the `food` roll-up is derived from the tag the category rules
> already carry.
>
> **Re-apply and Status stopped being destinations** — the first is the second
> half of editing a payee and now appears there, on write *and* whenever the
> ledger disagrees with config; the second is a strip on the Ledger. Account
> moved to a header menu. Every route and every action still exists.
>
> **The category chart uses one ink at five densities, ordered by magnitude**
> (§18.4), rather than ten hues that would have cost the palette the only
> discipline it has. Light gained card separation (1.08:1 → 1.22:1), dark gained
> range (five slates inside twelve points of lightness → a span from #0d1017 to
> #313a4c, with the cover finally *above* the cards).
>
> **Three defects came out of the operator reading the shots**, and the first is
> the phase's worst: the purge button read "Back up, then purge and re-push"
> directly above a note saying this container cannot take a database dump. The
> button now says what it does, and the dump became a **precondition** — no dump
> from the last hour, no purge, refused server-side (§18.7). Chasing the second
> found `make check` counting remembered-device digests as backup codes, reporting
> ten where there were eight, in the direction that suppresses the warning
> (§18.8). The third: a zero month in a column chart is now labelled, because
> blank reads as a bug.
>
> **Phase 5 (Metabase) moves from "deferred" to "not building"** (§9), with one
> condition for reopening it.
>
> **Phase 12's outstanding item is closed** (§6.8.5): a redacted, RC4-40
> encrypted PDF fixture, so `test_pdf_matches_xls.py` runs everywhere instead of
> skipping. Building it needed the bank's line-breaking algorithm, which is now
> reproduced exactly — 193 breaks, 93/93 rows. It also surfaced the one parsed
> field the original cross-validation never compared: `counterparty_bank`
> agrees on **90/93**, and the reason is measured rather than guessed.

> **v6.0 changelog.** Phase 12 (§6.8): the PDF fallback, deferred since v2.0,
> built specifically to cross-validate against the XLS while both exports of
> the same range still exist. **Every field that drives behaviour is identical
> across the two formats** — 93/93 on txn_id, date, amounts, balance, channel,
> payee, payee_alias, utr, txn_time and is_reversal. Raw `narration` is not and
> cannot be: Canara's renderer discards the whitespace run at a line wrap.
> `payee` is therefore whitespace-collapsed in **both** loaders (§6.8.3);
> `narration` is left verbatim per §7.2. §6.4's continuation rule, written in
> v2.0 for a loader that did not exist, finally executes. **The PDF password is
> the last four digits of the account number, not the Customer ID** — earlier
> guidance in this file was wrong (§6.8.1). §6.1 records that `txn_id` is
> derivable, not merely readable.

> **v5.0 changelog.** Phase 11 (§17). Phase 10's design plan was verified by
> tests and **never looked at**; screenshotting every page found five plan
> failures and five defects the plan never anticipated, the worst being that
> the Payees page truncated every category value to five characters. All fixed.
> CLAUDE.md gains a standing rule: *tests cannot see*. Caddy joins the stack on
> `127.0.0.1:80` routing `passbook.localhost` and `khata.localhost` by Host,
> with `auto_https off`; D9 updated for the third listener. PWA manifest and
> maskable icons drawn from the Day Rail. Motion, toasts and skeletons added;
> explanatory prose moved behind progressive disclosure.

> **v4.0 changelog.** Phase 10 (§16). Flask becomes a JSON API under `/api/*`;
> the UI is a React 19 + Vite + TypeScript bundle built by a Node stage that is
> then discarded, so the runtime image has no Node in it. Auth gains **TOTP,
> eight mandatory single-use backup codes, remember-this-device, and rate
> limiting**, on an httpOnly SameSite=Strict session cookie rather than a JWT
> in localStorage. **The unknown-username timing oracle is closed** — that path
> used to skip hashing entirely and answered measurably faster. Money now
> crosses the process boundary as a decimal string, never a JSON number, and
> renders with `en-IN` grouping. `config.load_web_auth`/`save_web_auth` are
> **removed**: they round-tripped two of the file's six fields, so saving
> through them would have erased the second factor. Design tokens, the Day Rail
> and the demo-data rule are §16.4–16.6.

> **v3.2 changelog.** Phase 9 (§15): re-apply config to existing rows through
> the proven purge+resync path, a status page, read-only backup reporting, and
> web credentials moved to `config/web-auth.json` with a change-password form.
> **`bootstrap` now UPDATES a rule whose definition changed** rather than
> skipping it by title — the old behaviour left rules.yaml and the engine
> silently divergent and produced six uncategorised rows. `configwrite`
> inserts before trailing comments, so an appended payee no longer lands under
> the next section's header. payees.md's Alias column is generated, so alias
> drift is impossible by construction. **Backup execution stays on the host**:
> running it from the web container would need the Docker socket, which is a
> host compromise waiting to happen.

> **v3.1 changelog.** §14.6–14.7. The web password hash is stored base64 in
> `PASSBOOK_WEB_PASSWORD_HASH_B64`, because raw it contains `$` and an unquoted
> `$` value is truncated by `set -a; . ./.env` (measured). `passbook
> web-password` now writes `.env` itself — printing lines to copy is what
> produced a wrapped two-line paste and a hash with a newline through it, which
> surfaced only as "login failed". `make check` validates the encoding. Auth
> failures are distinguished in the server log and never on the page. Recorded
> that compose does **not** substitute `$` out of a `.env` value — that
> hypothesis was wrong, and the newline was the whole fault.

> **v3.0 changelog.** Phase 7: a local web UI (§14) on `127.0.0.1:8081`, so the
> weekly cycle no longer needs a terminal. Server-rendered, no build step, with
> single-operator auth from the start because Phase 8 is Tailscale. The CLI is
> untouched and remains fully functional. `service.py` now holds everything both
> front ends do — the logic moved, it was not duplicated — and
> `cli.sync_staleness` renders the same `sync_status()` the home page does.
> `configwrite.py` edits yaml through ruamel so `rules.yaml`'s D10 comments
> survive a UI write, and shows a diff before writing anything.

> **v2.13 changelog.** §11: the GPG passphrase may only be created via
> `make backup-passphrase`, which refuses to overwrite. The previous
> instructions showed a bare `>` redirect, which would have silently destroyed
> a working passphrase and made every uploaded archive undecryptable.

> **v2.12 changelog.** The stale-sync warning escalates past 21 days and prints
> on `make up` and `make sync` as well as `doctor` — a reminder only visible in
> a command you run when you have already remembered is worthless. Email/SMTP
> notification is explicitly **not built** (§9): it would put an app password in
> `.env` for a job a calendar entry does better. A weekly calendar reminder is
> the mechanism; `doctor` is the check that it worked.

> **v2.11 changelog.** §11 gains a disaster-recovery drill (`make dr-drill`)
> that rebuilds the ledger from the encrypted archives and passphrase alone.
> It measured that APP_KEY is not load-bearing for data but is for API-token
> continuity, so `make backup` now carries it in the encrypted tarball; and it
> caught that `pg_dump` was emitting owner statements that made the dump
> unrestorable under a different role. `passbook doctor` warns when the newest
> file in `archive/` is more than 10 days old, because Canara limits how far
> back statements can be pulled and a long gap is data loss, not lateness.

> **v2.10 changelog.** §11 gains two operational requirements. `make
> verify-backup` proves a dump restores, in a throwaway container, never
> against the live database — and is itself negative-tested against a corrupt
> and a truncated dump. `make backup-remote` encrypts both artefacts with GPG
> AES256 and pushes them off-machine via rclone, with the passphrase held
> outside the repo and outside `.env`, and retention bounded at both ends.

> **v2.9 changelog.** First real weekly cycle run, with a deliberately
> overlapping export (2026-07-15 → 2026-08-08) against a ledger built from
> 2026-05-07 → 2026-08-07. **§6.1's txn_id stability caveat is RESOLVED:**
> 32/32 overlapping rows carry identical IDs and identical content, so the bank
> ID is a genuine natural key and the hash fallback stays unbuilt. `sync`
> pushed 0 and reported 32 duplicates — §12 Phase 3's criterion met against a
> different export, not a re-run of the same file.

> **v2.8 changelog.** §8.1 adds income exclusions (`not-earnings`), the mirror
> of the spend-side ones, stated as an inversion — earnings are Salary and
> Interest Income, everything else is excluded — so new counterparties are
> handled without an edit and no withdrawal can ever carry the tag. §11's
> `make backup` now also captures `config/*.yaml`, which the database dump does
> not contain. Housekeeping pass on stale cross-references: §13 no
> longer orders a fetch of the OpenAPI document that §7.1 records as 404 on
> eight paths; §8's "two rules" is three; §10's "eight grammars" is ten; §3's
> layout gains `firefly/purge.py` and `loaders/_table.py`; §12's Phase 4 no
> longer cites a rule count. `config/rules.yaml` is gitignored alongside
> `payee_aliases.yaml` — both name real counterparties — with
> `rules.example.yaml` committed in its place. `passbook doctor` now reports
> drift between payees.md's Alias column and the yaml, and never syncs them.

> **v2.7 changelog.** Phase 4 built: `config/rules.yaml` from the operator's
> assignments, `firefly/bootstrap.py`, `passbook bootstrap` (idempotent).
> Three defects found and fixed by running it: soft-deleted rows block
> re-insertion via the duplicate hash (§7.3), `large-oneoff` was tagged
> client-side in violation of D5 (§8), and excluding by `category_is` is inert
> at store time (§8). D10 now carries the measured 40% error rate for
> inferring meaning from truncated tokens. bills.yaml stays absent — nothing in
> the data meets §9's recurrence test.

> **v2.6 changelog.** Added `passbook purge` (§7.3) and `firefly/purge.py` (§3),
> needed because aliases apply at push time so re-running them requires a
> delete-and-repush. Records that Firefly returns 401 rather than 404 for an
> absent transaction group, and how to disambiguate that from a dead token.
> `config/payee_aliases.yaml` is now gitignored (it names real counterparties);
> `payee_aliases.example.yaml` is the committed one.

> **v2.5 changelog.** §6.1 gains `txn_time` (the UPI timestamp is now captured,
> not discarded) and `payee_alias` (display-only, from `config/payee_aliases.yaml`).
> §6.5 gains the reversal payee backfill by UTR. §8's large-one-off rule amended
> to skip Investments and Transfers — on real data it would otherwise have
> mislabelled a mutual-fund purchase and a transfer between the operator's own
> accounts as unusual spend.
> Phase 4 categories are being assigned by the operator; no rules written yet.

> **v2.4 changelog.** Phase 3 built 2026-08-07. §4.4's token path corrected —
> *Options → Profile → OAuth* is a pre-v6 layout that leads to the wrong
> credential; it is *Options → Remote access and tokens*. §7 updated with the
> `POST /api/v1/transactions` request shape — read from the validating code on
> the pinned tag, since the instance serves no OpenAPI document — and with the
> PAT's 365-day expiry.

> **v2.3 changelog.** Phase 2 built 2026-08-07 and run against the reference
> statement: **93 transactions, 0 continuity breaks, computed final balance
> equals the Closing Balance sentinel, account assertion passes.** §6.5 gained
> two grammars v2.0 missed — `UPI/CR/` (14 rows) and `UPI/REF/` (1 row) — and
> the "82 UPI debits" figure is corrected to 66. §6.3's `' '` rule re-verified
> and tightened. §6.1 gained a `sheet_row` field so §6.6 can name the offending
> row. Everything else in §6 held exactly as written.

> **v2.2 changelog.** §5 corrected on review. The claim that base64 emits
> docker-compose metacharacters was **wrong** and is retracted in place — compose
> interpolates `$`, which base64 does not emit. `LOG_CHANNEL`/`APP_LOG_LEVEL` now
> set explicitly, with `docker_out` recorded as invalid in v6.x, plus the
> config-caching gotcha that makes `exec -e` experiments misleading.

> **v2.1 changelog.** Phase 1 built 2026-08-07. §5 rewritten with what the real
> `fireflyiii/core:version-6.6.6` image required: the `version-` tag prefix, an
> alphanumeric (not base64) `APP_KEY`, the image's built-in healthcheck steered at
> `/health` via `HEALTHCHECK_PATH`, and `environment:` over `env_file:` so the
> Firefly token stays out of the container. Nothing in §6–§8 was touched.

> **v2.0 changelog.** Verified against a real statement (93 transactions, May–Aug 2026).
> Changed: real column names captured (§6.3), bank transaction ID replaces the
> synthetic dedup hash (§6.1), all-cells-are-text discovered (§6.3), eight real
> narration grammars documented (§6.5), starter category rules deleted as
> unmatchable (§8), redaction scope widened (§11). The balance-continuity
> invariant (§6.6) was validated against real data and passes cleanly.

---

## 0. Purpose

Maintain a private, queryable, tabular ledger of all Canara Bank savings-account
activity, with automatic categorisation and a clean separation between
**recurring/regular spend** and **large one-off spend**.

Workflow, once built:

1. Weekly: download the statement from Canara net banking as XLS (manual — human step).
2. Drop it into `inbox/`.
3. Run `make sync`.
4. Read the numbers at `http://localhost:8080`.

---

## 1. Non-goals

Do not build these, do not suggest them.

| Non-goal | Why |
|---|---|
| Automatic bank sync / net-banking scraping | India has no consumer-accessible open-banking API. RBI's Account Aggregator framework restricts data access to registered FIUs (regulated entities only) — an individual cannot obtain access. GoCardless/Nordigen and Plaid do not cover Indian retail banks. Scraping breaks constantly and violates the bank's terms. |
| Firefly III's built-in GoCardless/Nordigen import | EEA-only, closed to new customers. |
| The `fireflyiii/data-importer` container | We push via REST API. See D3. |
| DuckDB as a separate analytics store | Firefly already stores everything in Postgres. See D2. |
| Uploading statements to any online converter | Leaks account number, customer ID, balances, counterparty phone numbers. Non-negotiable. |
| Mobile app, multi-user, cloud, HTTPS/reverse proxy | Laptop-only, single user, localhost-bound. |
| SMS-parsing ingest | Google Play policy has killed non-default-SMS-handler finance apps; irrelevant to a laptop workflow. |

---

## 2. Architecture decisions

Decided. Do not re-open or present alternatives.

### D1 — Postgres, not MariaDB
Operator is fluent in PostgreSQL; Metabase (Phase 5) connects natively with no third-party driver.

### D2 — No DuckDB. Metabase reads Firefly's Postgres directly.
Firefly III is the single system of record. When SQL dashboards are wanted, add a
`metabase_ro` role with `SELECT`-only grants against the same database. No ETL, no drift.

### D3 — Push via REST API, not CSV import
We are writing a parser anyway. `POST /api/v1/transactions` gives explicit dedup
control, lets us set category/tags/notes at creation, removes a container, and
removes the Data Importer's one-time UI mapping ceremony. The Data Importer must
not appear in `docker-compose.yml`.

### D4 — XLS is the primary format; PDF is a deferred fallback
**Verified:** the Canara export is a genuine OLE2 compound document
(magic bytes `D0 CF 11 E0 A1 B1 1A E1`), readable by `xlrd` 2.x. It is *not*
HTML-in-disguise, which is a common Indian PSU bank pattern.

Retain a cheap magic-byte sniffer anyway (§6.2) — it costs eight bytes and guards
against the bank silently changing its export backend, which does happen.

> `xlrd` 2.x reads **only** `.xls`. `openpyxl` reads **only** `.xlsx`. Neither
> reads both. This is the correct pairing, not a mistake to "fix."

### D5 — The parser normalises; Firefly classifies
The parser turns narration into structured fields (`payee`, `channel`, `utr`,
`counterparty_bank`). It does **not** assign categories. Categorisation lives in
Firefly's rules engine, seeded from a version-controlled `rules.yaml`.

### D6 — Recurring vs one-off via Firefly Bills + an amount-threshold tag
- **Recurring:** a Firefly *Bill* per known recurring payment. Bills reconcile
  passively and flag misses. No cron required.
- **Large one-off:** a rule tagging any withdrawal above `LARGE_TXN_THRESHOLD`
  (default ₹10,000) not already matched to a Bill.
- Firefly's *Recurring Transactions* feature is **not used** — it depends on a
  cron container that cannot run reliably on a laptop. See D7.

### D7 — No cron container
WSL2 stops when Windows sleeps. All automation is manual, via `make`.

**Nor email — added v2.12.** The obvious follow-on is "then have it email me",
and the answer is no: SMTP needs an app password in `.env` or a third-party
account, which is permanent credential exposure bought for a job a weekly
calendar reminder does better and for free. The operator sets the reminder;
`passbook` only reports how long it has been (`sync-age`, printed by `make up`,
`make sync` and `doctor`), escalating past `SYNC_URGENT_DAYS`. Checking whether
a reminder worked is an honest job for software; being the reminder is not.

### D8 — Docker Engine inside WSL2, not Docker Desktop
Install `docker-ce` in the Ubuntu distro with systemd enabled via `/etc/wsl.conf`.

> **WSL2 gotcha.** Repo and Docker volumes must live under `~/` on ext4, **never**
> `/mnt/c/...`. Cross-boundary bind mounts are pathologically slow and break
> Postgres file permissions.

### D9 — Bind all ports to `127.0.0.1` only

> **Amended in Phase 11 for a third listener.** Caddy publishes
> `127.0.0.1:80:80` so `passbook.localhost` and `khata.localhost` work without
> a port number. D9 is about *reachability*, not port count: nothing binds
> `0.0.0.0`, and port 80 is no more exposed than 8080 was. 8080 and 8081 keep
> working unchanged — the runbook, the DR drill and every healthcheck use the
> numbered ports and must keep doing so, since they run when Caddy may not be
> up.

`127.0.0.1:8080:8080`, not `8080:8080`.

### D10 — Categorisation rules are derived from data, not shipped as guesses

> **Measured, v2.7. Of ten tokens the operator tried to identify from the name
> fragment alone, four were wrong — a 40% error rate.**
>
> | Token | Read as | Actually |
> |---|---|---|
> | `THE CLASSI` | a restaurant | a clothing shop |
> | `HOTEL_PARK` | a hotel stay | an event venue |
> | `SHRI BALAJ` | a temple donation | a sweet shop |
> | `RAMESH KUM` | a person | a fast-food franchise |
>
> **The tokens above are illustrations in the bank's own shape.** The four real
> ones are the operator's and are not in this repository; the *measurement* —
> four wrong out of ten — is real, and it is the evidence for the rule rather
> than a colourful aside. Every one of those readings is plausible, and a
> ~10-character truncation is simply not enough information to categorise on.
> Tokens get identified by the operator from outside knowledge, never inferred
> from the string.
**New in v2.0.** Canara truncates the UPI counterparty name to ~10 characters, and
the observed spend is overwhelmingly person-to-person UPI rather than named
merchants. Rules matching `"Swiggy"` or `"Zomato"` would never fire. Therefore:
ship an **empty** `rules.yaml` plus a `passbook payees` command that ranks observed
payee tokens by frequency and total value. The operator writes rules from that
output. See §8.

---

## 3. Repository layout

```
passbook/
├── .env.example              # committed
├── .env                      # gitignored
├── .gitignore
├── Makefile
├── docker-compose.yml
├── Dockerfile                # the only image we build, §16
├── pyproject.toml            # uv-managed
├── README.md
├── CLAUDE.md
├── SPEC.md
├── config/
│   ├── rules.yaml            # starts empty — populated from `passbook payees`
│   ├── bills.yaml            # operator fills in
│   └── payee_aliases.yaml    # truncated token → canonical name
├── inbox/                    # gitignored
├── archive/                  # gitignored
├── backups/                  # gitignored
├── scripts/
│   ├── redact.py             # statement → safe test fixtures (4 containers)
│   ├── pdfwrite.py           # the PDF fixture's renderer, §6.8.5
│   ├── shoot.py              # every page, both themes, both widths, §17.1
│   ├── motion.py             # the states a screenshot cannot see, §17.5.4
│   └── icons.py              # every icon size from one source, §17.5
├── frontend/                 # React 19 + Vite + TS, §16.1 — build-time only
│   └── src/
│       ├── theme.css         # design tokens and every rule, §16.4, §18.4
│       ├── components/       # DayRail, charts, reconcile, ui, feedback
│       ├── pages/            # one per route
│       └── lib/              # api, money, types
├── src/passbook/
│   ├── cli.py                # Typer entrypoint
│   ├── config.py             # pydantic-settings
│   ├── models.py             # Transaction, StatementMeta
│   ├── loaders/
│   │   ├── __init__.py       # magic-byte sniffer + dispatch
│   │   ├── _table.py         # shared row-grid -> model core
│   │   ├── xls.py            # PRIMARY — xlrd
│   │   ├── html_table.py     # defensive fallback
│   │   ├── delimited.py      # defensive fallback
│   │   └── pdf.py            # the fallback, §6.8
│   ├── narration.py          # the ten grammars, §6.5
│   ├── validate.py           # balance-continuity invariant
│   ├── service.py            # shared core for CLI + web, §14; §18's analysis
│   ├── configwrite.py        # comment-preserving yaml edits, §14
│   ├── ops.py                # backup reporting, §15
│   ├── webauth.py            # password, TOTP, backup codes, devices, §16.2
│   ├── web/                  # §16.1: a JSON API plus the built bundle
│   │   ├── api.py            # every route, delegating to service.py
│   │   ├── app.py            # factory: blueprint, CSRF, cookies, SPA fallback
│   │   ├── auth.py           # session, throttle, CSRF
│   │   └── dist/             # gitignored: built by the Node stage
│   └── firefly/
│       ├── client.py
│       ├── push.py
│       ├── purge.py          # delete pushed rows, §7.3
│       └── bootstrap.py
└── tests/
    ├── fixtures/
    ├── test_loaders.py
    ├── test_narration.py
    ├── test_validate.py
    ├── test_push.py
    └── test_web.py
```

---

## 4. Phase 0 — Human-only prerequisites

Claude Code cannot do these. Put them in `README.md` as a checklist; `make check`
must verify each before other targets run.

1. Enable systemd in WSL2 (`[boot]\nsystemd=true` in `/etc/wsl.conf`), then
   `wsl --shutdown` from PowerShell.
2. Install Docker Engine + compose plugin in the distro; add user to `docker` group.
3. Download one statement as XLS from Canara net banking into `inbox/`.
4. After the stack is up: create the Firefly account at `localhost:8080`, then
   **Options → Remote access and tokens** (`/profile/oauth`) → Personal Access
   Tokens → paste into `.env` as `FIREFLY_TOKEN`.

   > **Corrected in v2.4.** Earlier revisions said *Options → Profile → OAuth*,
   > a pre-v6 layout that does not exist in v6.6.6. Following it lands on the
   > Profile page's **Command line token**, which is a different credential and
   > will not authenticate the API. Verified against the pinned tag:
   > `'oauth_tokens' => 'Remote access and tokens'` and
   > `'command_line_token' => 'Command line token'` are distinct strings, and
   > `/profile/oauth` resolves while the old path does not.
   >
   > A PAT is a JWT — roughly 1000 characters, starts `eyJ`, two dots. Anything
   > short and dotless is the wrong credential.
5. Set default currency to **INR** in Firefly Preferences.
6. Set `PASSBOOK_ACCOUNT_NUMBER` in `.env` (used for the safety assertion in §6.7).

---

## 5. Phase 1 — Docker stack — BUILT AND VERIFIED

Deliverable: `docker-compose.yml`, `.env.example`, `make up|down|logs`.
Built 2026-08-07 against `fireflyiii/core:version-6.6.6`. Env var names below were
read from that tag's own `.env.example`, not from memory.

| Service | Image | Notes |
|---|---|---|
| `db` | `postgres:16-alpine` | Named volume `pgdata`. Not port-published. Healthcheck `pg_isready`. |
| `app` | `fireflyiii/core:version-6.6.6` | Pin an explicit stable tag, never `latest`. `127.0.0.1:${FIREFLY_HOST_PORT:-8080}:8080` — loopback always, host side settable. Depends on `db` healthy. |

```
APP_KEY=<exactly 32 random chars>
DB_CONNECTION=pgsql
DB_HOST=db
DB_PORT=5432
DB_DATABASE=firefly
DB_USERNAME=firefly
DB_PASSWORD=<generated>
APP_URL=http://localhost:8080
TZ=Asia/Kolkata
TRUSTED_PROXIES=**
```

Also set, from upstream's `.env.example`: `APP_ENV=production`, `APP_DEBUG=false`,
`SITE_OWNER`, `DEFAULT_LANGUAGE=en_US`.

**Four things found on contact with the real image. All four are load-bearing.**

1. **The Docker Hub tag namespace is `version-X.Y.Z`, not bare semver.**
   `fireflyiii/core:6.6.6` does not exist; `fireflyiii/core:version-6.6.6` does.
   Remember this when bumping the pin.

2. **`APP_KEY` is generated alphanumeric, matching upstream.** v2.0 said
   `head -c 32 /dev/urandom | base64 | head -c 32`; upstream's own `.env.example`
   recommends `LC_ALL=C tr -dc 'A-Za-z0-9'`. `make env` follows upstream.

   **This is a convention choice, not a bug fix — do not cite it as precedent.**
   An earlier draft of this section claimed base64 emits characters that docker
   compose interpolates. That is wrong. Compose interpolation triggers on `$`
   alone, and the base64 alphabet is `A–Z a–z 0–9 + / =` — no `$`. A truncated
   base64 key would have worked fine. The real argument for alphanumeric is
   narrower: the value passes through compose interpolation, a `sed` replacement
   in `make env`, and `set -a; . ./.env` in three targets, and restricting the
   alphabet means never having to re-check it against any of them.

   The actual safety net is independent of the generator: `make check` rejects
   `$ # " '` in `APP_KEY` and `DB_PASSWORD` outright.

3. **The app image already ships a `HEALTHCHECK`**, so do not write one. It runs
   `curl --fail http://localhost:8080$HEALTHCHECK_PATH` every 5 s with a 300 s
   start period. Setting `HEALTHCHECK_PATH=/health` upgrades it from "nginx is
   answering" to a real database check: v6.6.6 routes `/health` to
   `HealthcheckController@check`, which runs `User::count()` and returns `200 OK`.
   That endpoint is registered `withoutMiddleware(['web'])`, so it needs no
   session and no auth.

4. **Pass env vars via `environment:`, not `env_file: .env`.** Our `.env` is a
   superset of Firefly's — it also holds `FIREFLY_TOKEN` and
   `PASSBOOK_ACCOUNT_NUMBER`, which are read by the CLI and have no business
   inside the container. Enumerating the vars explicitly keeps them out.

   The cost is that Firefly's own `.env.example` defaults are not inherited, so
   anything wanted must be named here. Audited: the only two that mattered were
   `LOG_CHANNEL` and `APP_LOG_LEVEL`, both now set explicitly (see 5).

5. **`LOG_CHANNEL=stack`, and `docker_out` does not exist in v6.6.6.**
   `config/logging.php` defines exactly six valid destinations —
   `single, papertrail, stdout, daily, syslog, errorlog` — plus the generic
   `stack`, which fans out to `['daily', 'stdout']`. `stdout` is the one that
   reaches `make logs`.

   `docker_out` was a valid channel in Firefly v5.x and is **gone in v6**. It
   would be assigned to `logging.default` with no matching entry under
   `channels`, and Laravel's `LogManager` would throw
   `Log [docker_out] is not defined`. Do not set it.

   Note that `LOG_CHANNEL=stack` (the default) is *broader* than
   `LOG_CHANNEL=stdout`: narrowing to `stdout` silently drops the 7-day `daily`
   file inside the container.

   **Config is cached at container boot** (`bootstrap/cache/config.php`;
   `php artisan about` reports `Config: CACHED`). Changing any env var therefore
   requires `make down && make up` — injecting it with `docker compose exec -e`
   propagates to the process but has no effect on Laravel, which reads the
   baked cache. This makes exec-based config experiments misleading.

**The host port is configurable, the container port is not.** `web` and `caddy`
reach Firefly as `app:8080` over the compose network and are unaffected by it.
`FIREFLY_HOST_PORT` exists because on WSL with `networkingMode=mirrored` the
distro shares Windows's port space: a Windows service listening on 8080 makes the
bind fail with `address already in use` while `ss -ltnp` inside the distro shows
nothing at all. Measured on 2026-08-14: a Windows background service was bound
to `0.0.0.0:8080`, and the author's machine has run on a different host port
ever since. **8080 remains the shipped default** — `.env.example`, the compose
default and `docker-compose.yml`'s `${FIREFLY_HOST_PORT:-8080}` all name it, and
nothing in the codebase hardcodes the author's port. `APP_URL` and `FIREFLY_URL`
must name whichever port is in use, and `tests/test_stack.py` reads it from
`.env` rather than assuming.

**Verified on 2026-08-07:** `make up` → both containers healthy in ~40 s (61
migrations on first boot); `/health` returns `200 OK`; host listener is
`127.0.0.1:8080` only, `db` publishes nothing; `make down && make up` preserves
data — confirmed by an unchanged Postgres `system_identifier` (a fresh volume
would have been re-`initdb`'d and got a new one) plus a surviving marker row.

> **Verify before writing.** Fetch the current Firefly III docker-compose docs and
> confirm env var names for the tag being pinned. These change across major
> versions. Do not write from memory.

---

## 6. Phase 2 — Parser

### 6.1 Data model

```python
class StatementMeta(BaseModel):
    account_number: str
    customer_id: str
    account_name: str
    branch_code: str
    ifsc: str
    period_from: date
    period_to: date
    opening_balance: Decimal
    closing_balance: Decimal

class Transaction(BaseModel):
    txn_id: str               # bank's own ID — see below
    txn_date: date
    narration: str            # raw "Remarks" cell, untouched
    debit: Decimal | None
    credit: Decimal | None
    balance: Decimal
    # populated by narration.py:
    channel: str              # UPI | IMPS | NEFT | INT | CHG | SCHEME | OTHER
    payee: str | None         # NOTE: truncated to ~10 chars for UPI
    utr: str | None
    counterparty_bank: str | None
    is_reversal: bool = False
    txn_time: time | None = None      # added v2.5 — see below
    payee_alias: str | None = None    # added v2.5 — see below
    sheet_row: int = -1               # added v2.3 — see below
```

**`txn_time` — added v2.5.** The statement has no time column, but 82 of 93 rows
are UPI and their narrations embed `DD/MM/YYYY HH:MM:SS`. §6.5 strips that before
tokenising; it must not also *discard* it. Time of day is real signal — a canteen
at 01:51 is a different thing from a canteen at 13:00 — and this is the only
place it exists. `None` where the narration carries no clock: the plain-text
shapes, NEFT, and the short `R01` reversal.

**`payee_alias` — added v2.5.** Canonical display name from
`config/payee_aliases.yaml`. `payee` always keeps the bank's raw truncated
token; an alias is a display concern and never rewrites source data. Consumers
read `display_payee`, which falls back to `payee`. The raw narration still goes
to Firefly's `notes` verbatim.

**`sheet_row` is an addition to this listing, not part of the bank's data.**
§6.6 requires the continuity check to "raise with the offending row index", and
a position in the transaction list is useless when you are staring at the file
in a spreadsheet. It is excluded from the normalised JSON output.

**`external_id` is the bank's transaction ID — not a synthetic hash.**
Changed in v2.0. The `Trasnaction ID` column carries `YYYYMMDD` + a 6-digit
per-day sequence (e.g. `20260509000001`). Verified across 93 rows: format holds
universally, values are unique, and the date component always matches the row's
date. This is a natural key and is simpler and more robust than hashing.

> **RESOLVED — v2.9. The ID is stable across exports; no fallback is needed.**
>
> Tested exactly as this section asked. A second export covering
> **2026-07-15 → 2026-08-08** was compared against a ledger built from
> **2026-05-07 → 2026-08-07**. The overlap is 32 rows.
>
> | Check | Result |
> |---|---|
> | Overlapping rows carrying an ID already in the ledger | **32 / 32** |
> | Rows in the window present in one export but not the other | **0** |
> | Same ID *and* identical date, amount and narration | **32 / 32** |
>
> The bank does **not** regenerate the sequence per export, and the ID is not a
> function of the requested range. `Trasnaction ID` is a genuine natural key.
> A `sync` of the overlapping file pushed 0 and reported 32 duplicates — the
> §12 Phase 3 criterion, now met against a genuinely different export rather
> than a re-run of the same file.
>
> **The hash fallback below is therefore not implemented, on purpose.** Do not
> build it speculatively. Reopen only if a future export contradicts this —
> which the balance invariant (§6.6) and the duplicate counts will both surface
> loudly.
>
> If that ever happens, the fallback is
> `sha256(f"{txn_date}|{debit or 0}|{credit or 0}|{balance}|{narration.strip()}")`
> truncated to 40 hex chars — the balance component makes it unique even for
> identical same-day transactions.

> **The key is derivable, not merely readable — added v6.0.** The 6-digit tail
> is a plain 1..n ordinal within each date. Verified across all 45 days of the
> reference statement, and again against the PDF export, which carries **no
> transaction-id column at all**: reconstructing it from `YYYYMMDD` plus the
> row's position within its date reproduces the bank's own value byte for byte,
> 93/93.
>
> That is a robustness property worth having written down. If a future export
> drops the column — as the PDF already does — `external_id` does not have to
> fall back to a hash, and the ledger's idempotency (§7.2) survives a format
> change. The reconstruction holds only while rows are listed in the bank's own
> within-day order; `tests/test_pdf_matches_xls.py` is what keeps that honest.

There is **no value-date column**. Do not model one.

Money is `Decimal` everywhere. Never `float`.

### 6.2 Format sniffing

`loaders/load(path) -> tuple[StatementMeta, list[Transaction]]` reads the first 8
bytes and dispatches. Never trust the extension.

| Magic bytes | Format | Loader |
|---|---|---|
| `D0 CF 11 E0 A1 B1 1A E1` | OLE2 = genuine `.xls` — **the observed case** | `xls.py` (xlrd) |
| `50 4B 03 04` | ZIP = `.xlsx` | openpyxl |
| leading `<` | HTML table mislabelled `.xls` | `html_table.py` |
| `%PDF` | PDF | `pdf.py` (Phase 6) |
| else | delimited text | `csv.Sniffer` |

Log the selected loader at INFO.

### 6.3 Sheet structure — VERIFIED, replaces v1 guesswork

Observed layout, 6 columns wide:

| Row (0-idx) | Content |
|---|---|
| 0–1 | Bank name; `Statement for Account from <date> to <date>` |
| 3–6 | Label/value pairs in cols 0–1 **and** 3–4: Account Number, Customer ID, Name, Address / Branch Code, IFSC Code, Branch Name, Address |
| 9 | **Header row** |
| 10 | `Opening Balance` sentinel — label in col 1, balance in col 4, no date |
| 11 … n-2 | Transactions |
| n-1 | `Closing Balance` sentinel — same shape as opening |

**Header row, verbatim:**

```
Date | Trasnaction ID | Withdrawals | Deposits | Balance | Remarks
```

Four things here that v1 got wrong:

1. **`Trasnaction ID` is misspelled in the bank's own export.** Match it as-is.
   Normalise by stripping non-alphanumerics and lowercasing, then accept both
   `trasnactionid` and `transactionid`. Do not "correct" it.
2. **The narration column is `Remarks`** — not Particulars, Description, or
   Narration. v1's token list would have missed it entirely.
3. Debit/credit columns are plural: `Withdrawals` / `Deposits`.
4. Do not hardcode row 9. Scan downward for the first row containing ≥4 of the
   expected header tokens, and map columns by header text, not index.

**Every cell is a string.** All cells are xlrd type 1 (text), including amounts
and balances. Consequences:

- Amounts arrive as `'10,000.00'` — comma thousands separators. Strip `,`, then
  `Decimal(str)`. This is actually a benefit: no float coercion anywhere.
- **Empty amount cells contain a single space `' '`, not `''`.** Any emptiness
  test must `.strip()` first. This is the single most likely source of a silent
  parsing bug. **Re-verified in v2.3: all 93 transaction rows use `' '`,
  without exception.** The Opening/Closing Balance sentinel rows are the one
  place `''` appears — worth knowing, because an audit that scans the whole
  sheet will find both and may wrongly conclude the rule is soft. It is not.
- Read with `xlrd` directly, or `pd.read_excel(..., dtype=str)`. Never let pandas
  infer dtypes on this file.

**Dates** are `DD-MMM-YYYY`, uppercase month (`09-MAY-2026`). Parse with
`%d-%b-%Y` after `.title()`, or match month names explicitly — do not rely on
locale, which differs between WSL and CI.

**Sentinels.** Skip the Opening/Closing Balance rows as transactions, but capture
the opening balance — it seeds the §6.6 invariant, and capture the closing balance
as the final assertion target.

### 6.4 Multi-line narration — does not apply to XLS

One row per transaction, always. Verified: 93 rows, zero continuation rows, zero
rows with both or neither of debit/credit populated. Retain the continuation rule
for `pdf.py` in Phase 6 only.

### 6.5 Narration grammars — VERIFIED, replaces v1 guesswork

**Ten** distinct shapes observed — v2.0 said eight and undercounted, see 1b/1c.
Implement each as a named matcher in `narration.py`, tried in order, with a
permissive `OTHER` fallback that stores the raw string and sets `payee = None`.
Never raise on an unrecognised narration.

Exact census over the 93 reference rows:

| Rows | Shape |
|---|---|
| 66 | `UPI/DR/` |
| 14 | `UPI/CR/` |
| 4 | `INET-IMPS-CR/` |
| 3 | `NEFT CR-` |
| 1 each | `UPI/REF/`, `UPI .. /R01/`, `PMSBY`, `SMS CHARGES`, `SBINT`, `DEBIT CARD` |

**1. UPI debit** (66 rows — the dominant case)
```
UPI/DR/<12-digit UTR>/<payee>/<bank>/**<masked VPA>@<handle>/UPI//<RRN>/DD/MM/YYYY HH:MM:SS
```

**1b. UPI credit** (14 rows) — **new in v2.3; v2.0 missed this entirely.**
```
UPI/CR/<12-digit UTR>/<payer>/<bank>/**<masked VPA>@<handle>/<purpose>//<RRN>/DD/MM/YYYY HH:MM:SS
```
Positionally identical to the debit form — same 12 tokens, same indices — so one
matcher covers both. Only token 1 differs (`CR` vs `DR`), and token 6 carries a
free-text purpose where the debit form has the literal `UPI`.

v2.0 described grammar 1 as "UPI debit, 82 of 93 rows". That 82 is the count of
*all* `UPI`-prefixed rows (66 DR + 14 CR + 1 REF + 1 reversal), not of debits.
Treating them all as debits would have been harmless only because the
Withdrawals/Deposits columns are authoritative anyway.

**1c. UPI reference** (1 row) — **new in v2.3.**
```
UPI/REF/<UTR>/<payee>/<bank>/**<masked VPA>@<handle>/<purpose>/<RRN>/DD/MM/YYYY HH:MM:SS/<ref>
```
Same field positions again, with two differences: no empty token before the RRN,
and a trailing `/<ref>` *after* the timestamp — so the trailing-timestamp strip
does not fire on this shape. Harmless, because payee/bank/UTR are read
positionally from the front.

**2. UPI reversal / refund**
```
UPI/<UTR>/R01/DD/MM/YYYY
```
Short form, no `DR`/`CR` token. The UTR **matches the original debit's UTR** —
link them on that. Set `is_reversal = True`. Detect via: first token `UPI` and
some token matching `^R\d{2}$`.

**Backfill the payee from that link — v2.5.** This form carries no payee at all,
so a refund would otherwise post as `Unknown (UPI)` and vanish when netting
spend against a counterparty. After every row is parsed, copy `payee` (and
`counterparty_bank`) from the non-reversal sharing the UTR. Only fill what is
empty, and only from a non-reversal.

**3. IMPS credit**
```
INET-IMPS-CR/<payee>/<bank name>/<account>/<phone>/<phone>/DD/MM/YYYY HH:MM:SS/<ref>
```
Hyphenated compound prefix carrying direction.

**4. NEFT credit**
```
NEFT CR-<bank ref>-<IFSC>-<full payee>--<...>
```
Hyphen-separated, **space inside the prefix**, and the payee is **not truncated**
here. Observed counterparties include a clearing corporation and a payments
processor — i.e. this is where inbound income lands.

**5. Scheme / insurance debit** — `PMSBY RENEWAL(26-27) - <customer id> - <policy no>`
No slashes. **Embeds the customer ID.**

**6. SMS charges** — `SMS CHARGES ON ACTUAL BASIS` — plain text.

**7. Savings interest credit** — `SBINT FOR THE PERIOD FROM<date> TO <date>`
Plain text. Note the missing space after `FROM`.

**8. Card charges** — `DEBIT CARD ANNUAL CHARGES XXXXXXXXXXX<last4>` — plain text.

**Four gotchas that will bite:**

- **The trailing timestamp contains slashes.** `.../09/05/2026 01:51:33` adds four
  spurious tokens to a naive `split('/')`. **Strip a trailing
  `\d{2}/\d{2}/\d{4}( \d{2}:\d{2}:\d{2})?$` before tokenising.** This is the single
  most important line in the module.

  **Strip it, but keep it — v2.5.** The time of day is captured into
  `txn_time` (§6.1) before the strip. Extraction is a *search*, not an anchored
  match: on `UPI/REF/` and `INET-IMPS-` the timestamp sits mid-string followed
  by a trailing reference, so an anchored pattern never sees it. Stripping stays
  anchored — only a genuinely trailing timestamp is removed.
- **The VPA is masked.** `**15659@YBL` — only the handle and a partial suffix
  survive. Not a reliable unique key. Usable only as part of a composite
  fingerprint (`payee + handle + bank`).
- **UPI payee is truncated to ~10 characters.** Measured length distribution
  across the 80 UPI rows that carry an extractable payee (66 DR + 14 CR; the
  reversal and the REF row are excluded): 9 chars (62×), 10 chars (8×), 5 chars
  (9×), 7 chars (1×) — the spike at 9–10 is the truncation signature.
  **Re-verified in v2.3 and unchanged.** Even named merchants are clipped —
  a global payments brand, a hotel chain and a fund house all arrive as
  nine-character stubs, exactly like the fixture's `ZABFL_ZAB` and
  `NYXN XWUBQ`. Consequences: rules must match on
  **prefixes**, `payee_aliases.yaml` keys must be the **truncated** forms, and two
  different counterparties can collide on the same truncated string. Do not
  attempt to reconstruct full names.
- **Direction lives in three places** (`UPI/DR`, `INET-IMPS-CR`, `NEFT CR-`) but
  the **Withdrawals/Deposits columns are authoritative**. Use the columns. Treat a
  narration/column direction mismatch as a validation warning, not an error.

### 6.6 Validation invariant — VERIFIED AGAINST REAL DATA

```
abs(balance[i] - (balance[i-1] - debit[i] + credit[i])) < Decimal("0.01")
```

Seeded from the Opening Balance sentinel. Confirmed on the reference statement:
**93 transactions, 0 breaks, final computed balance equals the Closing Balance
sentinel exactly.**

On failure, raise with the offending row index and both balances. **Never soften
or skip this check to make a test pass.** It is the only thing standing between a
parsing regression and silently wrong financial data.

Additional assertions, all verified to hold on real data:
- Exactly one of debit/credit populated per row (0 violations observed).
- `txn_id` unique within a file, and its `YYYYMMDD` prefix matches the row date.
- Dates monotonically non-decreasing.
- Final computed balance == Closing Balance sentinel.

### 6.7 Account safety assertion

Parse the account number from the metadata block (§6.3) and assert it equals
`PASSBOOK_ACCOUNT_NUMBER` from `.env`. Refuse to push otherwise. This prevents a
misfiled statement from a different account silently corrupting the ledger.

Note the worksheet's **sheet name also contains the account number** — useful as a
cross-check, and important for §11 redaction.

---

## 7. Phase 3 — Firefly client and push

### 7.1 Client
Thin `httpx` wrapper. Bearer auth, `Accept: application/vnd.api+json`, retry with
backoff on 5xx, typed error on 4xx that surfaces the response body — Firefly's
validation errors are detailed and worth reading.

> **Verify before writing.** Fetch the running instance's OpenAPI spec and confirm
> the request shape for `POST /api/v1/transactions`. Field names have shifted
> across Firefly versions. If it cannot be verified, say so rather than guessing.

**v2.4: the instance serves no OpenAPI document.** All of
`/api/v1/openapi.json`, `/openapi.json`, `/api/openapi.json`, `/docs`,
`/api/docs`, `/api/v1/documentation`, `/v1/documentation.json` and `/api-docs`
return 404 on v6.6.6. Saying so rather than guessing is what §13 asks for, so
the shape was instead read from the code that actually validates the request —
a stronger source than any published document, because it is the very build
being called:

| Source (inside the running container) | Establishes |
|---|---|
| `app/Api/V1/Requests/Models/Transaction/StoreRequest.php` | every field name below, and that `type` ∈ withdrawal/deposit/transfer/opening-balance/reconciliation |
| `app/Factory/TransactionJournalFactory.php:443` | duplicates throw `DuplicateTransactionException("Duplicate of transaction #N.")` |
| `app/Api/V1/Controllers/Models/Transaction/StoreController.php:96` | that exception becomes a Laravel `ValidationException` → **HTTP 422** |
| `app/Api/V1/Requests/Models/Account/UpdateRequest.php` | `opening_balance` / `opening_balance_date`, each `required_with` the other |

Confirmed live: `GET /api/v1/about` → `{"version":"6.6.6","api_version":"6.6.6"}`,
and `POST /api/v1/transactions` with `{}` → 422 (not 404), so the route exists.

### 7.2 Push semantics

`POST /api/v1/transactions` with `error_if_duplicate_hash: true`,
`apply_rules: true`, and one split.

| Parsed | Firefly |
|---|---|
| debit row | `type: "withdrawal"`, `source_name` = asset account, `destination_name` = `payee` (or `"Unknown (<channel>)"` if null) |
| credit row | `type: "deposit"`, `source_name` = `payee`, `destination_name` = asset account |
| `txn_date` | `date` (ISO 8601) |
| `debit` or `credit` | `amount` — positive string |
| `payee` + `channel` | `description`, e.g. `ZEPKV JYX (UPI)` |
| `narration` | `notes` — **always preserve the raw string verbatim** |
| `txn_id` | `external_id` |
| — | `currency_code: "INR"` |

Duplicate rejection is a **normal outcome**, not an error: count it, log at DEBUG,
continue. Weekly downloads overlap by design; that is what dedup is for.

> **Detect a duplicate by its message, never by the error key.** Both a duplicate
> and a genuine validation failure arrive as HTTP 422 keyed on
> **`errors["transactions.0.description"]`** — the same key. Verified live: an
> empty POST returns that key with `"Need at least one transaction."`, while a
> duplicate returns it with `"Duplicate of transaction #N."`. Matching on the
> key alone would silently count every real validation error as a duplicate and
> report a clean run while dropping data. Match the message text.

**Two settings the push cannot infer, both added in v2.4:**

- **`PASSBOOK_ASSET_ACCOUNT`** — the Firefly asset account to post into. An
  instance can hold several (this one had three), and `doctor` refuses to guess
  rather than risk 93 rows landing in the wrong one. Quote it in `.env`: the
  name contains spaces, and `make check`/`backup`/`restore` source the file with
  `set -a; . ./.env`, where an unquoted space is a syntax error.
- **The target account's opening balance** must equal the statement's Opening
  Balance sentinel, dated on or before the first transaction. Otherwise Firefly's
  balance will not match the bank — and if the account was seeded with the
  *closing* balance (which is what Firefly's setup wizard invites), the closing
  figure is counted twice and the account ends up negative.

Reversals (`is_reversal`) still post as ordinary deposits — Firefly nets them
correctly. Additionally tag them `reversal` so they can be excluded from spend
analysis.

### 7.3 CLI

```
passbook parse   <file> [--json]     # parse + validate, print table, no writes
passbook payees  <file> [--top N]    # rank payee tokens by count and total value
passbook push    <file> [--dry-run]
passbook sync                        # process inbox/, archive on success
passbook bootstrap                   # create accounts, rules, bills from config/
passbook doctor                      # check .env, Docker, Firefly reachability, token
passbook purge   [--confirm]         # delete pushed rows from an asset account
```

**`purge` — added v2.6, not in the original §7.3 list.** Needed because aliases
and rules apply *at push time*: once rows are in Firefly, re-pushing to pick up
a new alias just hits dedup. Deleting and re-pushing is the way to re-run them.

Implemented as `firefly/purge.py` (also an addition to §3's layout).

**The `external_id` is the selection mechanism, not a date range.** Every row
passbook pushes carries the bank's transaction ID there; nothing else on the
account does. So an opening balance is excluded *structurally* rather than by a
guard that can be got wrong — verified live: 94 groups on the account, 93 with an
`external_id`, and the one without is `Initial balance for "..."`.

Two gates before anything is deleted: dry run is the default, and `--confirm`
still prompts interactively unless `--yes` is also passed.

> **Deleting is only half of it — v2.7. `DELETE /api/v1/transactions/{id}`
> soft-deletes, and `TransactionJournalFactory::errorIfDuplicate` queries
> `withTrashed()`.** A tombstone therefore keeps rejecting identical content as
> a duplicate forever. Measured: after deleting all 93 and re-pushing, only
> **41** rows landed — exactly those whose description had changed under a new
> alias. The other 52 were byte-identical to their own tombstones and were
> refused.
>
> `purge` now follows up with `DELETE /api/v1/data/purge`
> (`PurgeController@purge`), which force-deletes `onlyTrashed()` journals and
> groups. With that in place the re-push inserted all 93. Never delete
> transactions without it.

> **Firefly answers 401, not 404, for an absent transaction group** — it declines
> to leak existence. A bare 401 during a delete loop is therefore ambiguous
> between "already gone" and "the token just died". Resolve it by probing
> `/api/v1/about`: if the token still works, the group is genuinely gone; if it
> does not, abort loudly rather than recording 93 phantom successes. This makes
> a purge safely resumable.

Do **not** use `DELETE /api/v1/data/destroy?objects=transactions` for this. It is
account-blind and would also destroy other accounts' opening balances.

`sync` must be safely re-runnable. Archive to `archive/<YYYY-MM>/` only after a
successful push; on failure leave the file in `inbox/` and exit non-zero. Every
command prints: rows parsed, pushed, duplicates skipped, warnings.

`passbook payees` exists specifically to feed §8 — it is not a nicety.

**`doctor` also checks the token's expiry (v2.4).** A Personal Access Token is
an RS256 JWT valid for **365 days**, and Firefly gives no warning before it
lapses — the failure surfaces as a bare 401. `doctor` base64-decodes the `exp`
claim locally and warns within 30 days. No API call and no signature check: the
value is only used to print a date, never to trust anything.

---

## 8. Phase 4 — Rules and Bills bootstrap

`firefly/bootstrap.py` reads `config/rules.yaml` and `config/bills.yaml` and
creates the objects via the API. Idempotent: match on title/name, skip if present.

**Ship `rules.yaml` containing only these three rules.** Everything else is derived
by the operator from `passbook payees` output. See D10 — shipping merchant-name
guesses like `Swiggy` or `Blinkit` is actively harmful here, because Canara
truncates the counterparty name to ten characters and the observed traffic is
overwhelmingly person-to-person UPI, so such rules would silently never fire and
create false confidence that categorisation is working.

> **Status, v2.7:** that derivation has happened. `config/rules.yaml` now holds
> **22 rules** — the three below plus the operator's category rules and one
> `not-earnings` rule — every one of them from the operator's assignments against
> `payees.md`, none inferred from token text. `config/rules.yaml` is gitignored
> along with `payee_aliases.yaml`: both name real counterparties.
>
> The three below remain the only ones that ship in a fresh clone.

1. **Bank charges** — channel `CHG` (narration contains `CHARGES`) → category `Bank Charges`
2. **Interest income** — narration starts with `SBINT` → category `Interest Income`
3. **Large one-off** — `amount_more` than `LARGE_TXN_THRESHOLD`, type withdrawal
   → add tag `large-oneoff`. **Ordered last**, `stop_processing: false`, so it
   never short-circuits category assignment.

   **Two things had to be corrected before this worked at all — v2.7.**

   1. **`large-oneoff` must not be tagged client-side.** Phase 3's
      `build_payload` added it whenever a withdrawal exceeded the threshold.
      That violates D5 (the parser normalises, Firefly classifies) and, worse,
      it *cannot* honour the exclusions: the pusher has no idea which category
      a row will land in. It tagged a mutual-fund purchase and a credit-card
      payment — precisely the two rows meant to be skipped.
      The tag now comes only from the rule. `reversal` stays client-side,
      because that is a parser-derived fact rather than a classification.

   2. **Excluding by `category_is` does nothing at store time.** The
      category-setting rules have not committed when the last rule's triggers
      are evaluated, so the prohibition sees no category and passes. Express
      the exclusions against the *incoming* payload instead — `description_starts`
      on the display name, `notes_contains` for the card VPA. The
      `category_is` exclusions are kept as well: inert on store, correct if the
      rule is re-run manually over stored transactions.

   **The exclusion is expressible — verified v2.6.** Rule triggers accept a
   per-trigger `prohibited: true`, which `StoreRequest.php:229` reads and
   `RuleTransformer.php:131` round-trips. There is no `prohibited` column: it is
   stored as a `-` prefix on `trigger_type`, so `-category_is` means "category
   is not". The rule is therefore:

   ```
   triggers: transaction_type=withdrawal, amount_more=<threshold>,
             category_is=Investments  (prohibited), category_is=Transfers (prohibited)
   actions:  add_tag=large-oneoff
   ```

   It must be ordered **after** the category rules, since the category has to
   already be assigned for the prohibition to see it.

   Useful trigger operators, from `config/search.php`: `description_is`,
   `description_starts`, `description_contains`, `notes_contains`,
   `category_is`, `has_no_category`, `amount_more`, `amount_less`,
   `transaction_type`. Useful actions, from `config/firefly.php`:
   `set_category`, `add_tag` (both require a value).

   **Must not fire on Investments or Transfers — amended v2.5.** The tag exists
   to surface unusual *spending*. Money moving into an investment, or between
   the operator's own accounts, is neither unusual nor spending, and tagging it
   buries the real signal under routine flow. On the reference statement this is
   not hypothetical: the two largest withdrawals over the threshold are a
   mutual-fund purchase and a transfer between the operator's own accounts —
   both would have been mislabelled. Exclude by category, and order the rule
   after the category rules so the category is already assigned when it runs.

Categorisation rules must set `stop_processing: false`.

### 8.1 Income exclusions — `not-earnings`, added v2.7

Firefly counts **every deposit as income by type**, so money that is merely
coming back — family support, a repaid loan, a refund, a penny-drop
verification, a transfer between your own accounts — inflates earnings. On one
real statement that read 1.6 times the true earnings figure.

This is the exact mirror of the spend-side exclusions, and it works the same
way: tag it, then filter on the tag.

> ### The standing rule
>
> **Earnings are `Salary` and `Interest Income`. Nothing else.**
> Every other inflow — family support, repayments, refunds, penny-drop
> verifications, transfers between the operator's own accounts, and **every P2P
> credit without exception** — is `not-earnings`.

**Express it as an inversion, not a payee list.** One strict rule:

```
triggers: transaction_type = deposit
          description_starts = Salary            (prohibited)
          description_starts = Savings Interest  (prohibited)
actions:  add_tag = not-earnings
```

Two properties follow, and both matter:

- **Future-proof.** A counterparty appearing for the first time next week is
  excluded automatically. A payee list defaults the wrong way — anything not yet
  listed silently counts as income, and nobody notices until the earnings figure
  is quietly too high.
- **It cannot touch a withdrawal.** An earlier version listed payees wholesale
  and tagged both directions. A tag on an outflow is inert for income but not
  for spend: those rows then drop out of any spend query filtering
  `not-earnings`. Restricting the trigger to `transaction_type = deposit`
  removes that footgun by construction rather than by remembering to classify
  each payee correctly.

Verified on the reference statement: of 24 deposits, **19 are tagged**, leaving
five — Salary plus Interest Income, exactly. Zero withdrawals carry the tag.

`bills.yaml` ships with commented placeholders (rent, EMI, SIP, broadband) for the
operator to fill in with `name`, `amount_min`, `amount_max`, `repeat_freq: monthly`.

Document in `README.md`: *run `passbook payees` against three months of statements,
then write rules against the truncated tokens that actually appear.*

---

## 9. Phase 5 — Metabase — NOT BUILDING (amended v7.0)

> **Amended after Phase 13. This moves from "deferred" to "not building", with
> one condition for reopening it.** The charts in §18 changed the calculus, and
> leaving a phase parked when the decision has actually been made is how a spec
> stops being true.

**The expensive part was never the chart. It was the semantics.** §8 and §8.1
define what counts as spend and what counts as earnings, and ignoring them was
measured at three times the true spend and 1.6 times the true earnings.
Metabase reads Firefly's Postgres directly (D2), so those rules would have to be
restated as a hand-written SQL view over `transactions` →
`transaction_journals` → `category_transaction_journal` → `categories`, plus the
tag join — **a second definition of "spend", kept in sync with `rules.yaml` by
memory.** This project has been bitten by exactly that shape three times: alias
drift between `payees.md` and the yaml, rules drifting from the engine until
`bootstrap` was made to update rather than skip (§15.3), and `category_is`
exclusions that looked right and were inert at store time (§8). D5 exists to stop
the ledger having two opinions.

Add to that a JVM container (~1 GB resident) on a laptop already running
Postgres, Firefly, Caddy and the web app, plus a second authentication surface
on the machine, and the trade is worse than it looked in v1.0.

**What Metabase would genuinely have added, and where each is answered instead:**

| Want | Cheaper answer |
|---|---|
| Ad-hoc questions — "what did I spend at `XENN - UB` in June?" | **Firefly's own UI at :8080.** It filters and searches the same data with the categories and tags already applied, it is already installed, and it is already authenticated. The ad-hoc query tool is running. |
| Recurrence detection (the starter view below) | A CLI command over the archived statements, using the parser that already exists — no container, and it can write `bills.yaml` directly (D6). |
| Arbitrary windows, year-over-year | §18's monthly view, once there are more than four month buckets. Two of the current four are partial. |

**Reopen only if a question survives three months of being unanswerable in both
UIs.** If it does, the exclusion predicate must be **generated from
`config/rules.yaml`** rather than typed into SQL a second time — the same rule
that makes `service.ledger_analysis` the only place §8 is implemented.

The original plan, kept for whoever reopens it: `metabase/metabase` on
`127.0.0.1:3000`; a `metabase_ro` Postgres role with `CONNECT` on the firefly DB
and `SELECT` on `transactions`, `transaction_journals`, `accounts`, `categories`,
`tags` and the join tables, no write grants; and a starter view grouping by
normalised payee and rounded amount, flagging groups that appear ≥3 times with a
median inter-transaction gap of 25–35 days.

Mark as "Phase 5 — not building, see §9" in `README.md`.

---

## 10. Testing

- `pytest`. Fixtures in `tests/fixtures/`, produced by `scripts/redact.py` (§11).
- Golden-file tests: fixture in, expected normalised JSON out.
- `test_narration.py` must cover **all ten grammars in §6.5**, plus: the trailing
  timestamp with slashes, a masked VPA, a truncated payee, an `R01` reversal, and
  an unparseable string falling through to `OTHER`.
- `test_validate.py` must include a fixture with one row deleted and assert the
  continuity check catches it.
- `test_loaders.py` must assert the `' '` (single space) empty-amount case parses
  as `None`, not `Decimal('0')`.
- `test_push.py` mocks the API. No network calls in the suite.
- `make test` runs everything.

---

## 11. Security

Widened in v2.0 — the real statement carries more PII than v1 assumed.

**The file contains, in plaintext:**
- Account number — in the metadata block **and in the worksheet's sheet name**
- **Customer ID — which is also the password for the PDF statements**
- Account holder name, full postal address, branch code, IFSC
- Counterparty phone numbers and account numbers (IMPS narrations)
- Customer ID again, embedded in scheme narrations (PMSBY)
- Masked debit card last-4
- Full balance history

**Requirements:**
- `.env`, `inbox/`, `archive/`, `backups/` in `.gitignore`. Commit `.env.example` only.
- `scripts/redact.py` is a **required deliverable**, not optional. It must rewrite:
  the sheet name, the entire metadata block, all counterparty phone/account numbers
  inside narrations, the embedded customer ID, and the card last-4 — while
  **preserving balance-column internal consistency** so the §6.6 invariant test
  remains meaningful. Regenerating amounts requires recomputing the running balance.
- Never log a full account number — mask to last 4 in all output.
- Never log the customer ID or the Firefly token.
- All ports bound to `127.0.0.1`.
- `make backup` → `pg_dump` to `backups/firefly-$(date +%F).sql.gz`, **plus
  `config-$(date +%F).tar.gz` holding `config/*.yaml`**; `make restore FILE=...`
  reverses both. Firefly's rules live in the database and return with the dump,
  but **aliases do not** — they are applied at push time and never stored
  server-side, so `payee_aliases.yaml` is the only copy of the token→name
  mapping, and it is gitignored. Without the tarball it is the one piece of
  state with no backup anywhere. `restore` saves the current `config/*.yaml`
  aside before overwriting, since the live copy may be newer than the dump.
- **A backup is unproven until it has been restored.** `make verify-backup`
  loads the newest dump into a throwaway Postgres container and asserts the
  ledger reconstructs — transaction count, distinct external IDs, balance and
  earnings, compared against live. The live database is never touched. Verified
  to reject both a corrupt gzip stream and an intact-looking truncated dump; a
  verifier that cannot fail proves nothing. `make verify-backup FILE=...`
  checks an older archive for self-consistency instead.
- **Off-site, encrypted.** `make backup-remote` GPG-symmetric-encrypts both
  artefacts (AES256) and pushes them via rclone, so the remote holds ciphertext
  only. Each archive is decrypted and byte-compared before upload. Retention is
  bounded by `PASSBOOK_BACKUP_KEEP` at both ends.

  **The passphrase lives outside the repo and outside `.env`** — at
  `~/.config/passbook/backup-passphrase`, mode 600 — because `.env` is read by
  `make` and by docker compose, and a passphrase beside the thing it protects
  is not protecting much. `backup-remote` refuses to run if it is missing,
  not mode 600, or inside the repo. **Losing it is unrecoverable**; every
  uploaded archive becomes permanently unreadable.

  Three steps need a human: creating and recording the passphrase, `rclone
  config`'s interactive Google OAuth consent, and setting
  `PASSBOOK_RCLONE_REMOTE`.

  **The passphrase must only ever be created by `make backup-passphrase`,
  which refuses to overwrite an existing file.** Overwriting one is the most
  destructive operation in this project: every archive already uploaded becomes
  permanently undecryptable, with no error at the time and no copy of the old
  passphrase anywhere. Documentation must never show a bare
  `> ~/.config/passbook/backup-passphrase` redirect — an earlier revision of the
  preflight did, which would have destroyed a working passphrase if anyone had
  followed it after one already existed.
- **Recovery is drilled, not assumed.** `make dr-drill` rebuilds the ledger from
  the encrypted archives and the passphrase alone, on its own docker network,
  never touching the live stack. Measured, both ways:

  | | New APP_KEY | Original APP_KEY |
  |---|---|---|
  | 93 transactions, the archive's closing balance, integrity check | pass | pass |
  | existing Personal Access Token | HTTP 401 | HTTP 200 |

  **No ledger data is encrypted** — v6 removed database encryption (see the
  `RemovesDatabaseDecryption` upgrade command), so §5's APP_KEY warning is real
  but narrower than it reads. What the key protects is the Passport keypair,
  stored in `configuration` under `Crypt::encrypt`; a different key makes
  `restoreKeysFromDB` catch `DecryptException`, delete it and regenerate, which
  invalidates every existing token. Losing APP_KEY therefore costs a re-issued
  token, not the ledger. `make backup` carries it in the (already encrypted)
  tarball at `recovery/app-key.env` so recovery has one fewer manual step.
- **`pg_dump` must use `--no-owner --no-privileges`.** Without them the dump
  carries `ALTER ... OWNER TO firefly`, and restoring on a machine whose
  `DB_USERNAME` differs fails with `role "firefly" does not exist`. The drill
  found this by deliberately restoring under a different role. Archives taken
  before this change still need a role of that name.
- **Source lives off-machine too.** `config-*.tar.gz` holds four yaml files;
  `SPEC.md`, `CLAUDE.md` and `src/` are not in it and must not be stuffed into
  it. A git remote is the fix. Document that backups are plaintext
  financial history living only on this laptop.
- README warning: no HTTPS, no auth beyond Firefly's own login. Never expose
  beyond localhost without adding a reverse proxy.

---

## 12. Definition of done

In order. Do not start a phase before the previous passes.

- **Phase 1:** `make up` → healthy stack, Firefly at `localhost:8080`;
  `make down && make up` preserves data.
- **Phase 2:** `passbook parse` on the real statement yields **93 transactions,
  0 continuity breaks, computed final balance == Closing Balance sentinel**, and
  the account-number assertion passes.
- **Phase 3:** `--dry-run` shows correct payloads; `sync` inserts; re-running
  inserts zero and reports duplicates skipped.
- **Phase 4:** `bootstrap` creates rules/bills; re-run is a no-op; new pushes
  arrive pre-categorised.
- **Phase 6 (PDF fallback):** only after Phases 1–4 have survived one real weekly cycle.
- **Phase 7:** the stack comes up with a healthy `web` container on
  `127.0.0.1:8081`; unauthenticated requests to every page redirect to `/login`
  and a wrong password returns 401; an upload that fails the magic-byte, size,
  continuity or §6.7 check leaves `inbox/` empty; a valid upload previews row
  count, date range, continuity, the account assertion and unknown tokens
  **without pushing**; the payees page shows a diff before writing config; and
  **the CLI still does everything it did before**.

---

## 13. Working agreement for Claude Code

- Build phase by phase. Stop and report at each boundary. Do not run ahead.
- **Verify before writing integration code.** Read the validating code on the
  pinned tag — either inside the running container or from the tag on GitHub —
  rather than writing from memory. Where a fact cannot be verified, say so
  rather than guessing plausibly.
  **The instance serves no OpenAPI document**; §7.1 lists the eight paths
  checked, all 404. Earlier revisions of this line said to fetch one. Go to
  `app/Api/V1/Requests/...` instead — see the table in §7.1 for which file
  answers which question.
- Ask before adding any dependency not implied by this spec.
- Never commit anything under `inbox/`, `archive/`, `backups/`, or `.env`.
- Small, boring, readable modules. This is maintained once a month.
- The §6.3 and §6.5 findings come from exactly one real statement covering three
  months. **When a future statement contradicts them, update this spec alongside
  the code and flag what moved.** A stale spec is worse than no spec.

---

## 15. Phase 9 — Operations in the UI

Goal: no terminal for routine work.

### 15.1 What the UI must never become

**No Docker socket.** `make backup` shells into the database container and
`verify-backup` starts a scratch one, so running either from the web container
means mounting `/var/run/docker.sock` — root on the host, granted to the one
component that listens on a port, parses untrusted uploads and is bound for
Tailscale. That turns a web compromise from "the ledger and the token" into
"the machine, plus the power to delete every backup including the off-site
copies". **The UI reports backup health; the host runs backups.**

### 15.2 Re-apply config to existing transactions

Aliases and rules apply **at push time**, so editing them cannot reach rows
already in Firefly — the UI shows the new name and Firefly the old one, and
nothing is broken. Reconciling means purge + re-push, which is a delete, so it
diffs first and asks.

Order matters, and getting it wrong is not hypothetical:

1. back up `config/`
2. purge (existing `firefly/purge.py`, including the force-delete of trashed
   rows without which the re-push is rejected as duplicates)
3. **`bootstrap` — push the rules BEFORE the transactions.** Rules are applied
   at store time; a rule Firefly has not been told about categorises nothing.
   Omitting this step re-pushed six rows with new names that matched no rule
   and landed uncategorised, while the balance still reconciled — a green tick
   next to a worse ledger.
4. re-push every archived statement
5. verify the balance, and say so loudly if it does not reconcile

### 15.3 `bootstrap` updates, it does not skip

Idempotent by title, but **not** skip-if-present. Adding a payee to a rule has
to reach the engine or config and Firefly drift apart in silence. Rules are
compared on what we set — triggers, actions, strict — ignoring the ids and
timestamps Firefly adds, and updated via `PUT /api/v1/rules/{id}`.

### 15.4 Predicting a category

Inverting `payees:` lists is not enough, and assuming it was made the re-apply
preview claim rows would *lose* their category. `description_starts` is a
**prefix** match (`Canteen` catches `Canteen (via card)`), several rules match
the raw narration instead (`notes_contains: CHARGES`), and since every
categorisation rule sets `stop_processing: false` the **last** matching rule
wins.

### 15.5 Credentials in `config/web-auth.json`

Out of `.env`, which is read by compose and by `set -a; . ./.env` in three make
targets — the constraint that forced base64 in §14.6. `config/` is already
mounted writable, so the change-password form can write it without the
container touching `.env`, and JSON stores a `$` as a character. Written
atomically: a half-written credential file would lock the operator out of the
tool this phase exists to keep them out of a terminal for.
`make web-password` remains the recovery path.

---

## 14. Phase 7 — Local web UI

The weekly cycle required opening a WSL terminal, copying a file into `inbox/`
and running `make sync`. That friction is the most likely reason a week gets
skipped, and a skipped week is data loss once the download window closes (§8.1).
The UI removes the friction; it does not remove the CLI.

`web` container, `127.0.0.1:8081`, `restart: unless-stopped`. Server-rendered
Jinja templates, one hand-written stylesheet, **no SPA and no build step**.

### 14.1 The one rule this phase lives or dies by

**The UI is a front end over `service.py`, never a second implementation.**
Every route delegates to the same functions `passbook sync` calls. There is one
parser, one push path, one balance invariant. When CLI logic needed to be
callable from a request handler it *moved* to `service.py` and both call it —
it was not copied.

`service.py` raises plain exceptions; `typer.Exit` was specific to the CLI and
would have made the code unusable from a request. `cli.sync_staleness` and the
home page now render the same `service.sync_status()`, so the escalation
thresholds and their wording exist once.

### 14.2 Pages

| Page | Does | Does not |
|---|---|---|
| Home | balance, last-sync age, recent syncs | — |
| Upload | magic-byte check, size limit, parse, validate, §6.7 assertion, preview | push |
| Confirm | the existing push path, then archive on success | anything the CLI would not |
| Payees | every token with alias, category, counts; undecided first | guess a category |

**Upload validates before it saves.** A file that fails any check is deleted,
never left in `inbox/` where a later `make sync` would find it. Staging under a
dotted name makes that atomic.

### 14.3 D10 survives contact with a UI

A form is exactly where "just autofill a sensible default" creeps in. It does
not here. The category control is a dropdown of categories that **already have
a rule**; an unknown category is refused with the list of known ones rather than
a new rule being invented. D10 measured a 40% error rate on inferring meaning
from a truncated token — a dropdown does not improve that number.

### 14.4 Writing config

`configwrite.py` uses **ruamel.yaml**, not PyYAML. A PyYAML round-trip discards
every comment, and `rules.yaml`'s comments are where D10's evidence lives
(`NYXN XWUBQ  # a fast-food franchise, not a person`). Losing them to a UI write would
destroy the reasoning that stops a token being misread twice.

Two steps, always: `plan_*` produces the new text and a unified diff, `apply`
writes it. Nothing is written until the operator has seen the diff.

**Rules key on the display name, not the token.** `description` is pushed as
`"<alias or token> (<channel>)"`, so a token that has an alias must be listed
under its alias or the rule never fires — and two tokens sharing an alias must
collapse to one entry. `plan_categories` handles both, and removes a display
name from its old category so re-assigning moves rather than duplicates.

### 14.5 Security

- **Auth ships with the phase, not after it.** One operator, username plus a
  Werkzeug hash in `.env`; the plaintext exists nowhere. Bound to loopback
  today, but Phase 8 is Tailscale and at that point localhost stops being the
  boundary. Retrofitting auth onto a working UI is the change that gets
  postponed forever.
- Failed logins are indistinguishable between wrong user and wrong password.
- **Nothing secret reaches a template.** Account numbers are masked to last 4 by
  `StatementMeta.masked_account`; the Firefly token, DB password, `APP_KEY` and
  the customer ID are never passed to the render context. Verified against the
  live page.
- The §6.7 account assertion applies identically. A statement from another
  account is refused at preview, before it can reach the confirm step, and the
  refusal message masks both numbers.
- The container runs unprivileged (uid 1000) and needs `inbox/`, `archive/` and
  a **writable** `config/`.
- Served by waitress. Flask's development server is not appropriate under
  `restart: unless-stopped`, still less on Tailscale.

### 14.6 Credentials in `.env` — measured, not assumed

A Werkzeug hash is `scrypt:N:r:p$salt$digest`. Storing it raw put two separate
faults one paste apart, and only one of them was the one people expect:

| Hazard | Real? |
|---|---|
| Terminal wraps the hash; pasted back as two lines | **Yes — this is what broke login.** The value carried a newline through the middle of the digest. `check_password_hash` does not raise on it; it just returns False, so it surfaced only as "Login failed." |
| docker compose substitutes `$salt` away | **No.** Compose interpolates `${...}` in the *compose file*; a value read from `.env` is used literally. Confirmed in the running container: both `$` present, hash intact. |
| `set -a; . ./.env` truncates at the first `$` | **Yes, if unquoted.** Measured: quoted survives, unquoted truncates. It worked only because the value happened to be single-quoted. |

So the hash is stored **base64 (urlsafe)** in `PASSBOOK_WEB_PASSWORD_HASH_B64`.
That is alphanumeric plus `-_=`: no `$` to interpolate, no quoting to get wrong,
nothing to word-split, and a single unwrapped line. Verified through all three
consumers — compose into the container, `set -a; . ./.env`, and
pydantic-settings.

**`passbook web-password` writes `.env` itself.** Printing lines to copy is what
produced the wrapped paste. It replaces in place (never appends a duplicate),
consumes a multi-line quoted entry when cleaning one up, and deletes the old raw
key. `--print-only` emits one unwrapped line per value.

`make check` validates the encoding and that it decodes to a Werkzeug hash, so a
mangled value fails at `make up` with a clear message rather than as "login
failed" on a page that cannot explain itself.

**Two things that must read as *misconfigured*, never as *wrong password*:**

- `base64.urlsafe_b64decode` **silently discards** characters outside the
  alphabet unless the input is validated first, so junk "decodes" to plausible
  bytes. `decode_hash` checks the alphabet before decoding.
- `check_password_hash` **returns False** for an unparseable hash rather than
  raising. So the stored value's shape is checked before it is compared.

### 14.7 Login failures

The page always says exactly `Login failed.` — which half was wrong is free
information to an attacker. The **server log** distinguishes them, because when
it is the operator who cannot get in, "bad password for a known username" and
"no password hash configured" are completely different problems:

```
login failed: bad password for a known username (submitted username='...')
login failed: unknown username (submitted username='...')
login failed: no password hash configured — ... Run `make web-password`.
login failed: stored hash is not a Werkzeug hash (truncated or mangled?)
```

Neither the password nor the hash ever appears in a reason.

**There is no password reset**, and the login page says so. No email is
configured and there is no second factor — both would mean storing another
credential for a single-operator tool on one laptop. `make web-password` is the
reset. No forgot-password link.

### 14.8 Dependencies added

`flask` (bundles Jinja2 and Werkzeug, whose password hashing the auth
requirement needs anyway), `waitress`, `ruamel.yaml`. Chosen for the smallest
footprint that meets the brief; a web UI cannot be built without a web framework.

---

## 16. Phase 10 — React SPA, and a second factor

> **Numbered §16, not §15.** §15 is Phase 9 and §14 is Phase 7 — the file
> already numbers those out of order. Reusing §15 would have given two
> different phases the same reference in a document whose whole job is being
> citable.

Phase 9 removed the terminal from routine work. This phase changes two things
and deliberately nothing else: **how the UI looks**, and **how it is locked**.

### 16.1 Architecture

Flask is now a JSON API under `/api/*` and a static file server for a React 19
bundle. Both from **one origin, one container, one port**.

* `web/api.py` — every route, delegating to `service.py`.
* `web/app.py` — the factory: blueprint, CSRF, cookies, and the SPA fallback.
* `frontend/` — React 19 + Vite + TypeScript. TanStack Query for server state.
* `Dockerfile` — a Node stage builds the bundle and is **discarded**; the
  runtime image is `python:3.12-slim` with no Node, no npm, no toolchain.
  Verified: `which node` in the shipped image exits 1.

**No Zustand.** Nearly everything here is server state, and TanStack Query owns
it. The only genuinely client-side state is the payee form's pending edits and
the diff handed to the next page, which is React Router's `state` — a store for
that would end up managing a modal.

**The Phase 7 rule survives the rewrite intact.** The UI is a front end over
`service.py`, never a second implementation. One parser, one push path, one
balance invariant. The CLI does everything it did before.

**Money crosses the boundary as a decimal string, never a JSON number.** A JSON
number is an IEEE double the moment `JSON.parse` sees it, and CLAUDE.md's first
non-negotiable does not stop at the process boundary. The client groups the
digits itself (`lib/money.ts`) and never calls `Number()` on an amount.

Amounts render with **`en-IN` grouping** — `12,34,567.89`, the last three
digits then pairs, matching how the bank prints. Invisible on three months of one
account, and correct the first time an annual view crosses a lakh.

### 16.2 Authentication

Phase 7's threat model was "localhost is the boundary, and Phase 8 should not
have to retrofit a password". Phase 10 assumes the Tailscale exposure is
coming.

| Control | Where |
|---|---|
| Password (scrypt, Werkzeug) | `webauth.verify_password` |
| TOTP, RFC 6238, ±1 step | `webauth.verify_totp` (pyotp) |
| 8 single-use backup codes | salted SHA-256 digests |
| Remember device, 30 days | httpOnly `pb_device` cookie |
| Rate limit, 6 per 15 min | `web/auth.py`, in-memory |
| CSRF double-submit | `pb_csrf` cookie + `X-Passbook-CSRF` |
| Session | httpOnly, **SameSite=Strict**, `pb_session` |

**Session is a cookie, not a JWT in localStorage.** Any injected script can
read localStorage; it cannot read an httpOnly cookie. SameSite is `Strict`, not
`Lax`: Lax still sends the cookie on a top-level GET navigation from another
site. There are no state-changing GETs today, and Strict means there cannot be
one by accident.

**Two hash families, for two threats.** The password is user-chosen and
low-entropy, so it gets scrypt. Backup codes (50 bits) and device tokens (256
bits) are generated from `secrets` — a slow KDF buys nothing against a value
that cannot be guessed, and device tokens are checked on every request, where
scrypt would add ~100 ms per page load.

**The timing oracle is closed.** The old `check()` returned before hashing when
the username did not match, so an unknown username answered in microseconds
while a known one took ~100 ms — free account enumeration.
`webauth.verify_password` now runs a full scrypt verification against a
throwaway hash on the no-stored-hash path. Asserted by
`test_an_unknown_username_still_costs_a_full_hash`, which counts calls rather
than measuring wall-clock, because a timing assertion in CI is a flake.

**A TOTP code cannot be replayed.** The accepted counter is persisted and any
counter at or below it is refused, so a code captured mid-window is dead.

**Backup codes are mandatory, and enrolment cannot be skipped.** A lost phone
is the failure mode TOTP *creates*, and a ledger you cannot open is worse than
the attack it prevents. Three doors back in, in order of convenience:

1. a backup code, from the browser;
2. `passbook web-totp --reset` (`make web-totp RESET=yes`) — clears the secret
   and codes, so the next sign-in enrols a fresh QR;
3. `passbook web-password` — resets the password and **preserves TOTP**.
   Resetting a forgotten password must not also destroy the second factor;
   that turns one recoverable problem into two.

`web-totp` deliberately never prints the secret. Reading it out would let
anyone with a shell mint codes silently and indefinitely; resetting it is
visible at the next sign-in.

Credentials stay in `config/web-auth.json` (§15.5), now carrying the TOTP
secret, the code digests and the device list. `config.load_web_auth` /
`save_web_auth` were **removed**: they round-tripped only two of those fields,
so any future caller using them to save would silently erase the second factor.

### 16.3 The container stays unprivileged

No Docker socket, no backup passphrase, no rclone credentials — unchanged from
§15.3, and still asserted by `test_ops_only_ever_executes_rclone`, which walks
`ops.py`'s AST rather than grepping it.

### 16.4 Design

Grounded in the object — a Canara passbook: pre-printed grid, running balance
in the right-hand column, entries overprinted by a dot-matrix head.

| Token | Light | Dark | Role |
|---|---|---|---|
| `--paper` | `#EDF1EA` | `#1A1F2B` | Safety tint: the cool anti-photocopy wash on Indian cheque stock |
| `--ink` | `#1E2536` | `#E6E9EF` | Ribbon black — blue-violet, never pure black |
| `--grid` | `#C6D0C5` | `#2F3648` | The rule that is on the page before any entry is |
| `--stamp` | `#5C43A5` | `#A692EA` | Rubber-stamp violet: actions, links, focus, the rail tick |
| `--ochre` | `#8A5A0E` | `#E0A93F` | Cover board: needs-your-decision, truncation marks |
| `--verdigris` | `#16624E` | `#46B996` | Reconciled, and the deposit column tint |

Plus one red, `#97372B`, the only red in the app. **Money is never coloured by
sign** — a passbook prints withdrawals and deposits in the same ink and lets
the column carry the meaning.

Type: **Anek Latin** (display, two static instances of the variable family),
**Mukta** (body), **IBM Plex Mono** (all data). Mono is tabular by
construction, so money columns align without an override.

**Light-first, but only by default.** `prefers-color-scheme: dark` is honoured;
paper is what an unexpressed preference gets.

#### The Day Rail — the signature element

A 24-hour track with 00:00–06:00 shaded. One tick per row; twenty-four bars per
payee token. **SVG, never Unicode block characters** — block glyphs do not
align across fonts, cannot be styled, and read as noise to a screen reader,
which would make the signature element the least accessible thing on the page.
Every rail and histogram carries an `aria-label` with the actual time.

Its input is `txn_time` (§6.1): 85 of 93 rows carry a clock. The 8 that do not
render a dashed empty track — **never midnight**, which would invent a
nocturnal transaction that did not happen.

#### Fonts

All six faces are subset to Latin, digits and the marks actually rendered:
**46.3 KB total, from 1.45 MB upstream.**

| Face | Subset |
|---|---|
| AnekLatin-Display / Label | 10.4 / 10.7 KB |
| Mukta Regular / SemiBold | 8.3 / 8.2 KB |
| IBMPlexMono Regular / Medium | 5.0 / 4.9 KB |

Built from the upstream TTFs, **not the Google Fonts CSS API**: that API's
`latin` subset does not carry `U+20B9` (₹), and every amount here is INR.

Two glyphs are absent from Anek and Mukta upstream: **`→` and `✓`**. They are
therefore never used as characters anywhere in this app — status marks and diff
arrows are inline SVG. A missing glyph swaps in a system font mid-line, which
is among the hardest visual bugs to notice.

### 16.5 What did not change

Page structure, navigation, flow and step count are Phase 9's. Upload → preview
→ push is still two submits; payees → diff → apply is still two.

**D10 is intact.** The category control lists only categories that already have
a rule; there is no free-text field and no suggestion derived from token text.
An unknown category is refused with `422 unknown_category`.

**Preview shows rows now, but no category column.** Rules are applied by
Firefly at store time, so at preview no category exists — showing one would be
a guess or a lie. The row list is complete and in sheet order because it
carries a Balance column, and a Balance column over a filtered subset asserts a
continuity that is not there.

### 16.6 Demo and fixture data

**Every row shown anywhere — tests, demos, design specimens — comes from
`tests/fixtures/statement.xls` through the parser.** Not transcribed, not
invented.

This rule exists because a hand-typed specimen table shipped four defects at
once: a mislabelled alias, a fabricated interest row, a masked account number
that was not the operator's, and — worst — a Balance column over a
non-contiguous subset, so the displayed balances did not chain. A demo that
visibly contradicts §6.6 teaches distrust of the one check this project cannot
afford to have distrusted.

### 16.7 Dependencies added

`pyotp` (RFC 6238) and `segno` (pure-Python QR → SVG; no Pillow, so the web
image needs no compiler). Frontend: React 19, Vite 6, TypeScript 5, TanStack
Query 5, React Router 7 — all build-time or bundled, none present at runtime.

### 16.9 Web credentials are excluded from the backup — deliberately

`config/web-auth.json` holds the password hash, the TOTP secret and the
backup-code digests. `make backup` globs `config/*.yaml`, so the file was
already excluded — **by accident**. It is now excluded on purpose, and the
backup target asserts it on the finished tarball rather than trusting the glob
to stay narrow: if a future edit widens it to `config/*`, the backup fails
instead of shipping credentials to Drive.

The reasoning, because "either is defensible, not knowing which is not":

| | `payee_aliases.yaml` / `rules.yaml` | `web-auth.json` |
|---|---|---|
| Contains | operator knowledge, months of it | credentials only |
| Re-derivable | **no** — nothing can recover that a token is a night canteen | **yes** — two minutes |
| Cost of losing it | permanent | `make web-password` + re-enrol |
| Cost of archiving it | none | a live second factor in the Drive archive |

Including it would put the TOTP secret in the same encrypted blob as the
password hash it exists to be independent of, so one passphrase compromise
would take an attacker from zero factors to both. The archive already carries
credentials (APP_KEY, and the DB dump's Passport keypair), but those unlock
*data* — the web credential unlocks the *live UI*. Two minutes of recovery work
is the cheaper price.

Recorded in the disaster-recovery runbook as an explicit numbered step, not a
footnote.

### 16.10 Two defects found after the first sign-in

**The enrolment QR would not scan.** segno emits `width="45" height="45"` and
**no `viewBox`** by default. An SVG with intrinsic pixel dimensions and no
viewBox does not scale — `width: 100%` grows the canvas and leaves the drawing
45 px in the top-left. Measured: **0.9 px per module**, against the ~2 px a
phone camera needs at arm's length. `omitsize=True` makes segno write a viewBox
instead, and the same container now yields **5.2 px per module**. The failure
was silent: the page looked right and only the scan failed, which is why the
test asserts the rendered geometry (viewBox present, square, equal to the
module count, no fixed pixel size) rather than that an `<svg>` came back.

The QR's colours are deliberately **not** theme tokens. A decoder needs dark
modules on a light field; inverting that in dark mode produces a code many
cameras will not read. Asserted by a test that reads `theme.css`.

**The histogram's accessible label used the wrong denominator.** The bars count
transactions that carry a clock; the row count is *all* of them, and 8 of 93
rows have no clock (NEFT, CHG, SCHEME, INT). The label said "N transactions" with
N = clocked, so `Bank Charges` announced **"0 transactions"** beside a row
reading 2, and `XENN - UB` announced 3 beside 4. Six of 52 rows were wrong, and
wrong in the direction that misinforms precisely the person who cannot see the
chart. The label now states both numbers ("3 of 4 transactions carry a time"),
the row shows a `3/4 timed` note where they differ, and the API returns
`clocked` alongside `count` so the two can never be conflated again.

The histogram is also now keyed on `(token, channel)` to match how rows are
grouped. On token alone, a token appearing under two channels would hand both
rows the same chart while their counts differed. No token spans channels in the
current data — which is exactly why it would have gone unnoticed.

### 16.11 The DR drill now covers web access

`make dr-drill` predated web auth entirely, and runbook step 7 had never been
executed. Running it end to end found three things.

**1. A recovered install met a login box that could only fail.** With no
`config/web-auth.json` the container starts cleanly — no crash, no stack trace,
and `GET /api/session` correctly reports `configured: false`. But the SPA
ignored that field and rendered the ordinary sign-in form, and `POST
/api/session` returned the generic `"Sign-in failed."` — byte-identical to a
wrong password, with the real reason visible only in `docker compose logs web`.
That is the *guaranteed* state after any recovery, and it is precisely the
ambiguity §14.7 was written about.

Fixed in both places: the API answers `503 not_configured` naming
`make web-password`, and the client shows a "Not set up yet" page instead of a
form. Being explicit leaks nothing here — there is no account to enumerate.

**2. The runbook's "existing API token" row was wrong.** It said the token does
not survive and must be re-issued. Following this runbook, it *does*: step 5
restores APP_KEY, the Passport keypair is encrypted with exactly that key, and
drill step 8 signs in to the recovered UI with the original token and reads the
balance back.

The real hazard is ordering, and it was undocumented:

| | Existing token |
|---|---|
| APP_KEY restored **before** Firefly first boots | works |
| Firefly boots once on a generated key | **dead, permanently** |

On that first boot `restoreKeysFromDB` catches the `DecryptException`,
*deletes* both key settings and regenerates them. The original keypair is gone
from the database, so putting the right APP_KEY back afterwards leaves nothing
to decrypt. The drill hit this itself — steps 2-4 run on a new key, so the web
leg has to reload the dump before testing the recovered one.

**3. `make up` at runbook step 6 produces a visible state worth predicting** —
the UI says "Not set up yet" before step 7 creates the credential. Noted inline
so it does not read as a failure mid-recovery.

Drill steps 6-8 now cover all of it: the container starts with no credential
file, reports `configured: false`, names the fix, then walks
`passbook web-password` → mandatory TOTP enrolment → eight backup codes →
signed in → `/api/overview` reading **the archive's own closing balance** off
the restored ledger, with every category live from the recovered `rules.yaml`.
The expected figures are derived from `archive/` and `.env` rather than written
into the script, so the drill is not specific to one machine. The web leg runs on the
recovered APP_KEY because that is what the runbook instructs; steps 2-4 keep
answering the separate question of whether APP_KEY is load-bearing for data.

### 16.12 The APP_KEY hazard is a guard, not a warning

§16.11 documented that booting Firefly once on the wrong APP_KEY destroys the
Passport keypair irreversibly. A warning in a runbook is the wrong shape for
that: the moment you most want to type `make up` to see whether the restore
worked is exactly the moment it is unsafe.

`make check` — which `make up` depends on — now **refuses** when
`recovery/app-key.env` exists and disagrees with `.env`. That file only exists
when the config tarball has been extracted, so its presence plus a mismatch
means precisely one thing: mid-recovery, step 5 unfinished. The failure prints
the exact `sed` to copy the key across, and the alternative (`rm` the recovered
copy) for a deliberate fresh start.

Verified in three states: absent (normal operation, passes), present and
matching (passes, and says so), present and different (**refuses, exit 1, and
`make up` never reaches docker** — the live container's `StartedAt` is
unchanged).

Note the path is `recovery/app-key.env` at the repo root, not under `config/` —
the tarball writes `recovery/` as a top-level entry. The guard checks both.

### 16.13 Recovery has four inputs, not two

The runbook said "the two `.gpg` files, the passphrase, and a new machine". It
now names four, because the fourth has no second copy: **the source repo**,
which is private, so no fork or public mirror exists.

Losing it is survivable and the runbook now says how. The dump is plain SQL and
the money is in it, so a bare-hands restore needs no file from this repo —
`postgres:16-alpine`, `fireflyiii/core:version-6.6.6`, load, done. Tested
directly: the balance reads back exactly as the live stack reported it. What is
lost without the repo is the *pipeline* — parser, grammars, rules, aliases, web
UI — not the ledger.

Those two image tags are therefore the only strings worth keeping outside the
repo, next to the passphrase. A newer Firefly image will migrate the dump's
schema on boot and there is no way back.

A second git remote is the cheap fix and is deliberately **not done** rather
than overlooked: it needs a second account, and the ledger survives without it.

### 16.14 The source travels with the ledger

`make backup` now runs `git bundle create --all` and drops the result into the
config tarball. The whole repo and its full history is 0.4 MB, encrypted under
the same passphrase as everything else, so **GitHub is a convenience rather
than a dependency** and no second hosting account is needed. §16.13's "input
with no second copy" is retired.

This reverses the earlier decision not to put source in the tarball. That call
was made against *stuffing a source tree* in; a bundle is one compact file, and
the operator weighed it again with the private-repo single-point-of-failure in
front of them.

**Committed history only, and it says so.** A dirty working tree produces a
warning naming the files, because silently omitting the day's work is the
failure mode worth being noisy about.

**Verified twice, because once is not enough.**

| Check | Catches |
|---|---|
| `git bundle verify` | truncated or malformed bundle, unsatisfiable prerequisites |
| bare `git clone` into a temp dir | a corrupt **packfile** — every object is inflated |

The second is not belt-and-braces. Measured: after overwriting sixteen bytes in
the middle of the packfile, `git bundle verify` still reported *"The bundle
records a complete history"* — it reads only the header. The clone failed with
`pack has bad object at offset 2698: inflate returned -3`. Had the requirement
been implemented as literally specified — verify, then ship — a corrupt archive
would have passed as good. Either check failing aborts `make backup` **before
anything is written**, so the previous backup stands.

The recovery sequence changed shape as a result: the source now comes *out of*
the archive, so unpacking precedes cloning. Runbook steps renumbered to 10.

Drill step 5b proves it end to end — clones from the bundle inside the
encrypted archive, confirms HEAD, checks the tree is complete, and runs
`make check` in the recovered checkout, which reaches its own checks and
correctly reports the one thing a fresh clone lacks (`no .env`).

### 16.8 Definition of done

- `docker compose build web` produces an image with **no Node in it**.
- `/api/session` answers unauthenticated; every other endpoint 401s.
- A correct password alone does **not** sign in; a wrong TOTP code does not;
  a backup code works exactly once; six failures lock the account out.
- An upload failing magic-byte, size, continuity or §6.7 leaves `inbox/` empty.
- No Firefly token, DB password, customer ID or full account number appears in
  any response, page or log.
- The CLI still does everything it did before.


---

## 17. Phase 11 — polish, clean URLs, identity

### 17.1 The plan was never looked at

Phase 10 shipped with 226 passing tests, a green DR drill, and live API calls
confirming every figure. Screenshotting it found **ten defects**, three of them
obvious within ten seconds of opening a page. Every green tick was true. None
of them could see.

`scripts/shoot.py` now renders every page in both themes at desktop and mobile
widths, signing in through the real two-factor flow against the real Firefly
with a throwaway credential injected in memory — `config/web-auth.json` is
never read or written. Output goes to `docs/shots/<tag>/`, **gitignored**: the
shots render real payees and balances, so they are statement data under §11.

CLAUDE.md carries the standing rule: *any change to rendered UI must be
screenshotted and looked at before it is reported done.*

#### Against the Phase 10 plan

| Plan element | Outcome |
|---|---|
| Asymmetric bound-edge radius | **failed** — 2px vs 10px is imperceptible on a 1500px card; shipped as a rounded rectangle, i.e. the `rounded-lg` default it was meant to avoid |
| Ledger banding | **half** — worked in light; `#1F2533` on `#212736` is ~2% apart, so it did not exist in dark |
| Vertical column rules | **survived**, and the best thing on the page |
| Type scale | **half** — headings landed; mono was applied to *hero figures*, where its fixed advance parks the comma and period in full cells, so `₹5,068.09` renders with visible gaps |
| Two-accent discipline | **failed** — verdigris means "reconciled" but coloured a closing balance and a "Warnings: none" row, so the one colour carrying meaning meant nothing |

#### Defects the plan never anticipated

1. **Payees truncated every value.** `Investment`→`Inves`, `Groceries`→`Groce`,
   `Friend Repay`→`Frien`. The controls got ~95px while the token column got
   390px and two ISO dates took 130px each. The page whose job is showing what
   a token is categorised as could not display a category — and D10 exists
   because *the bank* truncates to ~10 characters.
2. **Rows ~85px tall.** Preview was 13,348px. A passbook is dense.
3. **The Day Rail was unreadable.** Night band and track differed by ~2% in
   both themes, in 26% of the table width.
4. **Mobile nav wrapped and dropped "Sign out"** at 390px.
5. **Three signals for one state** — ochre *and* italic *and* a chip made "not
   yet decided" look like "error".

A cascade collision was found while fixing these: `tbody tr:nth-child(odd) td`
(0,1,3) out-specified `.kv tr td` (0,1,2), so ledger banding leaked onto every
key-value table. That, not verdigris, is what tinted "Warnings: none" green.

### 17.2 What changed

Structure first — animating a broken layout is polish on a defect.

* **Payees**: `table-layout: fixed` with an explicit `<colgroup>`; alias and
  category get 22% each, dates shortened to `12 May 26`.
* **Density**: one line per row — alias leads, the bank's raw token trails
  dimmed. Where there is **no alias the token becomes the display at full
  strength**, which is the row where the truncation actually matters, so it is
  the only signal that state needs. Preview 13,348px → 8,190px; Payees 8,170px →
  6,634px.
* **The cover is board, not ink.** A full-width slab of `--stamp` read as
  generic chrome. `--board` is neutral; `--stamp` is now an actual stamp *mark*
  and a 3px rule under the band. Ochre would have repeated verdigris's mistake.
* **The boldness moved to the Day Rail**: `--rail-track` / `--rail-night` are
  new tokens with real separation in both themes.
* **Hero figures** use the body face with `tabular-nums`. Mono stays in columns.
* **Verdigris means reconciled**, nothing else.

### 17.3 Motion, feedback, and cut text

Motion sits behind `prefers-reduced-motion: no-preference`: page entrance, the
Day Rail tick sliding from midnight to its hour (staggered by row, capped at 24
so row 90 does not wait three seconds), histogram bars growing from the axis.
Skeletons replace spinners — a skeleton says *this shape is coming*.

Every action ends in a toast **naming what happened in the same word the button
used**: "Push" → "Pushed", "Discard" → "Discarded", "Write config" → "Written".
Failures stay until dismissed; an error that vanishes before it is read is an
error hit twice. `describe()` maps each API error code to a next step, so no
message is a bare "failed".

Upload and push show an indeterminate bar naming the work, because a frozen
button is indistinguishable from a hung app.

**Text was cut.** The enrolment page's two sentences became "Scan this, then
type the code it shows." Every "why this exists" paragraph moved behind a
`<Why>` disclosure. Explanation that helps once is clutter forever; keeping it
one click away costs nothing.

### 17.4 Clean URLs

Caddy, `127.0.0.1:80` only, routing by Host. `auto_https off` is explicit:
Caddy's default would mint a certificate from its internal CA for a
`.localhost` name and try to install that CA in the system trust store — root
required, fails in a container, and produces warnings for a loopback service.
Verified: the log says *"automatic HTTPS is completely disabled"*, and no ACME
is attempted.

Two browser assumptions were **verified in Chromium 149, not assumed**:

| Claim | Result |
|---|---|
| `*.localhost` resolves without a hosts entry | true — RFC 6761, both hosts loaded |
| `*.localhost` is a secure context over plain HTTP | **true** — `isSecureContext`, `crypto.subtle` and `navigator.serviceWorker` all present |

The second is what makes the PWA work without TLS.

If port 80 is already taken, the symptom is `docker compose up` failing to bind.
Check from PowerShell with `netstat -ano | findstr :80` and
`Get-Process -Id <pid>`; the usual culprits are IIS, "World Wide Web Publishing
Service", or a Windows-side Docker Desktop. Inside WSL, `ss -ltnp | grep ':80 '`.

### 17.5 Identity

The mark is **the Day Rail, stamped**: a rubber-stamp frame around a 24-hour
track with the midnight-to-six band shaded and one transaction tick. Not a
wallet and not a rupee glyph — those belong to every finance app ever made.
This is the only shape that is ours, it is already the signature element, and
it is abstract enough to survive 32px.

`scripts/icons.py` generates every size from one source, so the header glyph
and the installed icon cannot drift. Maskable icons are full-bleed with the
mark at 62% so it survives a circular crop; the 32px favicon drops the frame,
which at that size crowds the rail into mush.

Manifest verified served as `application/manifest+json`. Chromium ignores a
manifest under the wrong type with no console error and no warning; the only
symptom is that "Install" never appears.

> **Correction, from writing the regression test.** The code comment claimed
> Flask would otherwise guess `application/octet-stream`. That is **wrong for
> this stack**: CPython has known `.webmanifest` since 3.11, so `mimetypes`
> gets it right on every interpreter this project supports — measured inside
> the shipped image, which ships no `/etc/mime.types` and still guesses
> correctly. The explicit `mimetype` line is belt-and-braces, and is now
> labelled as such rather than as a fix for a problem that does not occur.

`Page.getInstallabilityErrors` reports only `in-incognito`, an artifact of the
test harness; notably **no service-worker error**, so current Chromium does not
require one.

**Firefly keeps its own favicon.** Overriding it means writing files inside a
third-party image and breaking on every upgrade.

### 17.5.1 Ochre means one thing

Verdigris was caught meaning "reconciled" in one place and "decorative green"
in three. Ochre had drifted the same way, into four meanings: night in the Day
Rail, the truncation mark, the "3/4 timed" note, and genuine attention. Four
meanings is a generic attention colour, which is no signal at all.

Two of those were **facts, not decisions**, and facts do not need a hue:

* **Night in the Day Rail is encoded by position** — the shaded band behind
  hours 0–6. Colouring the bars inside it as well said the same thing twice.
  Every bar and every tick is now `--stamp`; the band does the work.
* **The truncation mark and the "n/m timed" note** describe the data, not a
  state of the operator's attention. Both now take `currentColor` / `--ink-soft`.

What remains is every `--warn` variant, and each one means *something needs
your decision or action*: the "decide" chip, new-payee chips, a staged
statement, a stale sync, a would-change count, zero backup codes, warn toasts.
One judgment call kept: **"never upload a statement to an online converter"**
stays ochre. It is a standing prohibition rather than a queued task, but it
shapes a decision made outside the app and is the one place where being
ignorable would be expensive.

The Reapply note about the database dump lost its ochre — it is informational.

### 17.5.2 One fact, one statement

Three repetitions found by reading a 390px screenshot rather than the code:

| Was | Now |
|---|---|
| `2d`, then "last sync 2 day(s) ago (file.xls)", then the file again | `2d` and the filename |
| The account named in the header, the page subtitle **and** the balance card | the header, once |
| `2026-08-08 09:06` clipped to `2026-08-08 0…` | date only |

Also `day(s)` → `1 day` / `2 days`, and "1 still **need** a decision" →
"needs … it is listed first". `day(s)` is a form field, not a sentence.

### 17.5.3 The converter warning is plain, and contextual instead

Ochre there cost the semantic and bought nothing: a standing prohibition is
never *waiting on you*, and a permanent banner is wallpaper within a week. The
notice on Upload is now plain.

Force comes from **placement**. A second, ochre block appears only when an
upload is rejected and the sniffed magic bytes say PDF — the exact moment
reaching for an online converter becomes tempting — and it names why that file
in particular is dangerous: Canara's PDF is password-protected with the
customer ID, which is also the net-banking credential (§11).

### 17.5.4 Motion and feedback: built, then actually observed

All six shipped as code in the first pass. **None had been seen running.** They
were written, typechecked and screenshotted — and `shoot.py` waits for loading
to finish, so it excluded every one of them by construction. Building a thing
and observing a thing are different acts.

`scripts/motion.py` drives each state and reports what it observed:

| | Observed |
|---|---|
| Skeletons | 10 `.skel` elements, **0 spinners**, heading already rendered |
| Page transition | `page-in 0.22s delay 0s` |
| Day Rail entrance | `tick-in 0.42s`, delays `0s 0.018s 0.036s 0.054s 0.072s 0.09s` — the stagger is real |
| Toast wording | button "Discard" → toast **DISCARDED**, "The staged file was deleted." |
| Progress | `aria-label="Parsing and validating"`, button reads "CHECKING…" not frozen |
| Error | names the cause *and* the next step, plus the contextual PDF block |
| Reduced motion | `.page` animation-name `none` — honoured |

Three defects were in the *harness*, and each would have produced a false
report:

1. Reading computed animation without `wait_for_selector` returned empty
   strings for every property — which reads as "no transition shipped" when
   one had shipped.
2. Reading `.toast__title` **first** got the previous action's confirmation:
   "Discard" appeared to produce "CHECKED", because the upload toast was still
   on screen. It is the **last** toast that belongs to the action just taken.
3. The skeleton capture stalled the API with `time.sleep()` inside a sync route
   handler, which blocks Playwright's own driver — so the count was taken
   during loading and the screenshot two seconds later, after the table had
   rendered. The number was true and the picture was of something else. CDP
   network latency slows the browser without blocking the test.

### 17.6 Regression tests for the silent failures

226 tests did not move across a new container, a manifest and a CSS
restructure, because none of them covered any of it.

| Pinned | Where | Runs |
|---|---|---|
| Manifest type, shape, icons exist, theme colour matches `--board` | `test_assets.py` | always |
| `auto_https off`, both hosts, correct upstreams, forwarded headers | `test_assets.py` | always |
| Every published port is loopback-only (D9) | `test_assets.py` | always |
| Banding scope, and `.kv` out-ranking every generic cell rule | `test_assets.py` | always |
| Served manifest type, independent of the interpreter | `test_web.py` | always |
| Host routing, both upstreams distinct, ports 8080/8081 direct | `test_stack.py` | when the stack is up |

`test_stack.py` is a **deliberate, narrow exception** to "tests use fixtures,
never the network". Host-based routing cannot be asserted from a file: the
Caddyfile says what was intended, and `test_assets.py` pins that, but only a
request proves Caddy parsed it and put each host on the right upstream. Nothing
reaches beyond 127.0.0.1, nothing writes, and every test auto-skips when the
stack is down — measured: with `caddy` stopped, 6 skip and the 3 direct-port
tests still run.

**Each was verified by reintroducing the bug it exists to catch.** Restoring
the unscoped `tbody tr:nth-child(odd) td` fails both banding tests, one
printing the original specificity comparison `(0,1,3) < (0,1,2)`. Deleting the
explicit manifest mimetype fails the interpreter-independent test with
`fell back to 'application/octet-stream'`.

That last one only works because the test was rewritten. Written first as a
plain request, it **passed with the fix deleted** — `mimetypes` was quietly
covering for the app. A regression test that cannot fail is not a regression
test, and the first version of this one could not.

### 17.7 Definition of done

- Every page screenshotted in both themes at desktop and mobile, and looked at.
- Payees shows full category names; no control clips its value.
- Banding, rail contrast and figures legible in **both** themes.
- Mobile nav reaches Sign out at 390px.
- `passbook.localhost` and `khata.localhost` resolve; 8080 and 8081 unchanged.
- Manifest and maskable icons serve with correct content types.
- The CLI still does everything it did.


---

## 6.8 Phase 12 — the PDF fallback

XLS stays primary (D4). This exists for the weeks net banking hands over a PDF
and nothing else, and it was built **now, deliberately**, because both exports
of 07-May-2026 → 07-Aug-2026 existed at the same time and that cross-validation
is not available later.

`loaders/pdf.py` normalises the PDF into the canonical grid and calls
`from_rows`. No transaction construction lives in it: both formats share one
builder, which is what makes identical output achievable rather than hoped for.
The PDF speaks a different dialect and every difference is translated on the
way in — `Particulars`→`Remarks`, `09-05-2026`→`09-MAY-2026`,
`Between…and`→`from…to`, `Client`→`Customer ID`, and a synthesised
`Trasnaction ID`.

**pdfplumber, not Camelot.** The statement has six horizontal and four vertical
edges on a page — the frame, nothing else — so there is no ruled grid for
Camelot's `lattice` flavour to find. Only `stream` would apply, which is the
same column-position work done here, at the cost of OpenCV **and Ghostscript**,
a system package, in an image whose whole design is `python:slim` with no
compiler. **Ghostscript is not required and is not installed.** pdfplumber is
pdfminer.six: pure Python, and it exposes per-word coordinates, which §6.4 needs.

### 6.8.1 The password — earlier guidance in this file was wrong

It is the **last four digits of the account number**. Not the Customer ID.
Measured: all 94 numeric candidates in the statement, including the CIF in the
PMSBY narration, were rejected; the last four opened it, and `0000`/`1234`/
`9999` are refused, so the match is real.

**It is therefore not credential-class.** Those same four digits are what the UI
already prints as the masked account (`****NNNN`) under §11's own masking rule,
so treating them as a secret would be theatre. `CANARA_PDF_PASSWORD` is
optional; absent is fine until a PDF is uploaded, and the loader's error then
names the variable.

> **The PDF's protection is nominal.** `/V 1 /R 2` is RC4-40 over a four-digit
> secret — ten thousand combinations against a broken cipher, recoverable in
> under a second. Do not email these files or leave them in shared storage on
> the assumption that the password protects them. The privacy risk is the
> file's *contents* — account number, customer ID, address, counterparty phone
> numbers — and the encryption does nothing to contain it.

### 6.8.2 §6.4's continuation rule, finally executing

Written in v2.0 for a loader that did not exist, and never run until now. A
wrapped narration produces a line with no date and no amounts which must rejoin
the transaction above it.

*How* it rejoins is the whole problem: **the renderer discards the whitespace
run at the break.** Measured on the reference statement — the space glyphs are
absent from the char stream; leftover width does not separate the cases (0
spaces 0–41 units, 1 space 4–132, 2 spaces 104–117, overlapping); the
fragment's last character decides it only outside a payee name (`/` and digits:
0 spaces, 32/32) and is ambiguous inside one (`A`: 0, 1 and 2 all observed).
193 breaks: 105 consumed nothing, 75 one space, 12 two, 1 four.

Two repairs are exact rather than heuristic:

* **The trailing UPI timestamp** `/DD/MM/YYYY HH:MM:SS` has a single space by
  grammar, so a break landing on it is repaired outright. The regex is
  deliberately *not* anchored to end-of-string: the `R01` reversal shape
  carries a trailing `/<ref>` after the timestamp, and anchoring cost that row
  its clock (92/93 instead of 93/93).
* **A break after `/` or a digit** is inside the reference, VPA or UTR fields,
  which hold no spaces. An earlier version said "any non-letter", which
  swallowed the real spaces in `JY. MURQO` and `XENN - UB`.

`WRAP_EPSILON` is tuned for **payee** agreement, not narration bytes, and that
is the deliberate trade — see §6.8.3.

### 6.8.3 One normalisation: `payee`, and only `payee`

`payee` is whitespace-collapsed in **both** loaders. It is already a derived,
tokenised field, and it is the string rules match on (D10) — so without this
the same transaction categorises differently depending on which export it came
from, which would make the fallback a trap.

Checked before migrating, not assumed: **no collisions.** 52 distinct tokens
before collapsing, 52 after. Four config entries whose keys carry a
double space were rewritten by surgical text edit — the shape is
`MURZEP  X`, two spaces, and the yaml key has to match it exactly — after a
ruamel round-trip was rejected for re-indenting the whole file and moving a D10
comment away from its entry.

**`narration` is NOT normalised.** §7.2 sends it to Firefly's notes verbatim,
and the raw string is per-format evidence. A PDF-sourced note therefore differs
in whitespace from an XLS-sourced note for the same transaction. **That is
inherent to the format, not a defect.**

### 6.8.4 Cross-validation result

93 rows, both formats, same range. Identical on **every field that drives
behaviour**:

| | |
|---|---|
| `txn_id`, `txn_date`, `debit`, `credit`, `balance` | 93/93 |
| `channel`, `payee`, `payee_alias`, `utr`, `txn_time`, `is_reversal` | 93/93 |
| account number, customer id, name, IFSC, period, both sentinels | identical |
| §6.6 continuity from the PDF's own numbers | closes on the Closing Balance sentinel |
| raw `narration` | **differs — see §6.8.2** |

`tests/test_pdf_matches_xls.py` asserts these as numbers, not skips, so any
drift in either loader moves them.

### 6.8.5 The fixture pair — Phase 12's outstanding item, closed

`tests/test_pdf_matches_xls.py` shipped in v6.0 asserting real numbers against
the operator's two exports of the same range — and **skipping on any machine
that does not have them**, which is every machine, since both are gitignored
(§11). The cross-validation existed and did not run.

It now runs against a committed pair. `scripts/redact.py` emits a fourth
container from the *same* redacted grid as the `.xls`, `.csv` and `.html`
fixtures, and `scripts/pdfwrite.py` renders it as a Canara-shaped PDF encrypted
**RC4-40 (`/V 1 /R 2`)**, like the real export. Same leak audit as the others,
widened for the format: the bytes on disk are encrypted and Flate-compressed, so
a byte scan of the file reports *clean* whatever is inside it — the audit
decrypts and decompresses first, which is the one thing that makes it an audit.

The test is parametrised over both pairs. The real pair still runs where the real
files exist, and the fixture pair reproduces its numbers to within one row:

| | real pair | fixture pair |
|---|---|---|
| `txn_id`, date, amounts, balance, metadata | 93/93 | 93/93 |
| `channel`, `payee`, `payee_alias`, `utr`, `txn_time`, `is_reversal` | 93/93 | 93/93 |
| exact `narration` | 57/93 | 58/93 |
| whitespace-collapsed `narration` | 69/93 | 69/93 |
| `counterparty_bank` | **90/93** | 92/93 |

**Why generate rather than redact the real PDF.** The XLS fixture regenerates
every amount and recomputes the balance chain (§11), so a PDF made by
string-substituting the real export would disagree with it on every figure — and
comparing the two is the entire purpose. One grid, two containers.

#### The bank's line breaking, reproduced exactly

A generated fixture is only a test of §6.4 if it wraps the way Canara wraps. The
algorithm was recovered from the real export rather than modelled, and it
reproduces **all 193 breaks across all 93 rows** at a budget of 254.0pt — the
narration column's own width, x 85 to x 339. At 254.5 it reproduces 92.

Greedy fill to the budget, then retreat to the last break opportunity inside
what fitted:

* **a space** — the line is right-stripped and the whole whitespace run is
  discarded, which is the loss §6.8.2 measures;
* **immediately after a hyphen** — the hyphen stays on the line;
* **immediately before a hyphen**, but only when the hyphen is the character
  that did not fit. Exactly one row of 93 breaks this way, and it is what makes
  `SBINT FOR THE PERIOD FROM28-MAR-26 TO 27-JUN` / `-26` the correct answer
  rather than an anomaly.

With no opportunity in the fitted prefix the break is mid-token and consumes
nothing — the case the loader cannot recover, and the reason `WRAP_EPSILON` is
tuned for `payee` rather than for narration bytes.

Two things the fixture deliberately does not reproduce, both recorded in
`pdfwrite.py`: the Canara logo (an image XObject carrying bank branding, which
the parser never reads), and the exact vertical rhythm — the real renderer's row
pitch is not a function of the number of lines it drew (3-line rows appear at
both 40pt and 52pt pitch), so it cannot be recovered from the drawn output. Row
pitch has no effect on parsing, which groups characters into lines and orders
them by `top`.

One quirk *is* reproduced on purpose: the PDF draws the **Branch Code value one
point above its label**, so it groups into a line of its own and
`Branch Code\s+(\d+)` never matches. `StatementMeta.branch_code` therefore comes
back empty from a PDF and populated from an XLS. That is why the metadata
comparison never listed it, and it is now asserted rather than omitted.

The fixture regenerates byte-for-byte. qpdf refuses to generate a deterministic
`/ID` for an encrypted file and mints a fresh second ID element on every write,
and for `/R 2` the first element feeds the key derivation — so `pdfwrite` writes
plain-with-deterministic-ID, encrypts that, and copies the first ID element over
the second. Without this, `make fixtures` would show a diff every single run.

#### `counterparty_bank` is the residual, and it is now pinned

§6.8.4's table lists every field that drives behaviour, and `counterparty_bank`
is not among them — accurate, but silent about the fact that it is the one
*parsed* field the two formats disagree on. Measured on the real pair: **90/93**.
The three misses are breaks where the wrap heuristic guessed a space that was not
there and the guess landed inside the handle token (`OKSBI` → `O KSBI`).

It is the residual `WRAP_EPSILON` trades away to keep `payee` at 93/93, and it is
tolerable because nothing reads the field: it is not pushed (§7.2 sends
description, notes, external_id, amount, date), no rule matches on it (D10
matches the display name), and it appears nowhere in the UI. Pinned as a number
so that stops being an assumption.

#### What the fixture also bought

Coverage of paths that had none, because every committed fixture was plaintext:
`loaders/pdf._decrypt` (a correct password, a wrong one, none at all, and a
truncated file), the `CANARA_PDF_PASSWORD` route through settings, and the claim
that the statement has no ruled table for Camelot's `lattice` flavour to find —
page 1 carries four vertical edges (the two metadata panels) and the
continuation pages carry none at all.

---

## 18. Phase 13 — the Ledger becomes an overview

Three changes, in the order they had to happen: cull the pages, then draw the
charts, then fix the palette the charts exposed.

### 18.1 Six nav items become three

Nothing was removed. Three things stopped being *destinations*, because they
were not destinations:

| Was | Now | Why |
|---|---|---|
| Re-apply | on Payees | It is the second half of editing a payee, not a place. |
| Status | a strip on the Ledger | It is monitoring, and the Ledger already showed the last sync. |
| Account | a header menu | It is a setting, not a task. |

**Re-apply is the one that was failing.** Aliases and rules apply at push time,
so editing config cannot reach rows already in Firefly — and with the step
sitting behind its own nav item, it was missed: payees were renamed, the ledger
at :8080 kept showing the old names, and nothing on screen said a second step
existed. It now appears in the two places that would have caught that:

1. **On write.** The diff page no longer ends at "Written." and bounce back to
   Payees. It stays, fetches the preview, and asks the only question left:
   *N existing transactions would change — apply now?*
2. **On sight.** Payees shows the same count whenever the ledger disagrees with
   config, which is the case that actually went wrong — the config was written
   in an earlier session, so no write-time prompt could ever have helped.

The `/reapply` page survives for the row-by-row table, which is too much for an
inline card, and both places render the **same** `ReconcileCall` component, so
the count, the button wording and the ordering guarantee cannot drift apart.

The header menu is a `<details>` element, not a scripted dropdown: it opens on
click and on Enter, closes on Escape, and is announced as a disclosure without a
line of JavaScript. The one scripted behaviour is closing it after a navigation.

### 18.2 The charts, and the semantics they must respect

**Non-negotiable, and the reason this section exists.** Firefly counts every
withdrawal as spend and every deposit as income, by type. Measured on one real
three-month ledger, read that way it said **three times** the true spend and
**1.6 times** the true earnings. That is not a rounding error, and a chart drawn
on the naive numbers looks entirely reasonable.

Until this phase the correction lived only in the rules (§8, §8.1) and in the
head of whoever read a total. It is now one function, `service.ledger_analysis`,
and every figure on the page comes through it:

* **Spend excludes the `not_spend` categories** — `Investments`, `Transfers`,
  `Credit Card`, `Verification`. Money moving is not money leaving.
* **Earnings exclude the `not-earnings` tag** — so earnings are Salary and
  Interest Income, exactly as §8.1's inversion states.
* **A tag can never remove a withdrawal from spend.** §8.1 guarantees the tag
  only lands on deposits; the analysis does not rely on that guarantee, because
  the failure mode is real spending silently vanishing.

`not_spend` lives in `config/rules.yaml`, not in code: these are the operator's
own category names (D10), and a category named there that does not exist simply
excludes nothing. It is deliberately **not** the same list as
`large_oneoff.exclude_categories`, which guards a tag on unusual spending where a
₹1 penny-drop verification is irrelevant.

**The `food` roll-up needed no new configuration.** Four category rules already
tag their category `food`, precisely so total food spend is one query, so the
group is derived from those rules. The bar's *total* is the tag as Firefly stored
it and its *segments* are the categories that carry the tag in config — two
sources for one number on purpose: if they ever disagree the segments will not
fill the bar, which is visible rather than silent.

**Two sources, each authoritative for what it carries.** Money and category come
from Firefly, because the rules engine assigns the category at store time (D5)
and re-deriving it client-side would be a second implementation. The clock comes
from the statement, because `txn_time` is parsed out of the narration (§6.5) and
never pushed — Firefly has no idea what time of day anything happened. The two
are joined on `external_id`, which is the bank's own transaction id (§6.1).

`/api/analysis` is a separate endpoint from `/api/overview` on purpose: it reads
every transaction on the account and parses the archive, and the balance and the
sync age — the two things the page is opened for — must not wait behind it.

What is drawn:

| Chart | Reads | Notes |
|---|---|---|
| Where it went | real spend by category | 14 categories on this ledger, largest first |
| In and out | earned vs spent, one scale | the excluded part is drawn, hatched, beside the counted part |
| food | the tag total, segmented | The total moved once, mid-phase, when two rows were reassigned to Events by the operator's own rules edit. Correct as it stands: the figure follows `rules.yaml`, and a roll-up that did *not* move when a rule moved would be the bug. |
| By month | out and in, one shared scale | **no trend line** — see §18.3 |
| The day | the Day Rail at ledger scale | spend rows only, 59 of 62 carry a clock |

**The excluded remainder is drawn, not dropped.** A chart showing a third of the
gross figure invites exactly one question, and answering it in the mark — a hatched
continuation of the same bar, with the figure named underneath — is better than
answering it in a footnote nobody reads.

**No chart library.** Every mark is a bar or a column built on the Day Rail's own
construction. Recharts or Chart.js would add ~90 KB gzipped to a bundle whose
entire six-face font budget is 46 KB, bring their own type scale and default
palette, and then have to be argued out of drawing a legend and a tooltip for
four categories. `test_assets.py` pins their absence.

### 18.3 Three months is not a trend

The operator has three months of statements. The ledger actually produces **four
month buckets, two of them partial** — the export runs 07-May to 08-Aug, so May
and August are stubs. Both facts are shown, and neither is smoothed:

* Discrete columns, no line. A slope through four points, two of them stubs, is
  the most persuasive way to be wrong.
* A partial month carries a **dashed cap on its own column**, not a note at the
  bottom of the card. A month with no money at all gets nothing: a dashed line
  hovering over an empty axis reads as a bar being hidden rather than as a quiet
  month.
* Partiality is computed from the statement **periods**, not from the first and
  last transaction dates — a quiet fortnight at the start of a range is covered,
  not missing.

Out and In are two charts sharing one scale rather than paired bars with a
legend. That is the passbook's own answer: two columns on the page, position
carrying the direction, one ink. The caption says the scale is shared, because
that is what makes the flat Out chart informative rather than broken.

### 18.4 The palette: ten fills from three colours

The hard problem. A category chart wants ten distinguishable fills; this palette
has exactly three colours that mean anything — **stamp acts, ochre asks,
verdigris reconciles** — and §17.5.1 records what happens when one of them drifts
into meaning four things.

Ten categorical hues would buy legibility by spending the only discipline the
palette has. After a rainbow, a violet bar is just a bar, and the operator has to
re-learn what colour means on every screen.

**So the ramp is not categorical at all: one ink at five densities, ordered by
amount.** That is what a dot-matrix printer actually gives you, and it encodes
the same fact the bar's length already encodes. The redundancy is the point —
hue would be carrying *identity*, which the label beside the bar already carries
in words, whereas density carries *rank*, which is the thing the eye is being
asked for. Two consequences, both wanted: two categories of similar size are
deliberately similar in tone, and nothing in a chart can ever be mistaken for
"needs your decision" or "reconciled".

`--ramp-1..5` are mixed from `--ink` toward `--sheet` in oklab, so each theme
derives its own from its own ink. Measured off the shipped stylesheet, contrast
against the card:

| | 1 | 2 | 3 | 4 | 5 | excluded |
|---|---|---|---|---|---|---|
| light | 10.98 | 6.29 | 4.10 | 2.91 | 2.21 | 1.49 |
| dark | 10.30 | 6.75 | 4.66 | 3.34 | 2.49 | 1.55 |

The floor is deliberate: the ramp is redundant with bar length and with a printed
figure, so the smallest category does not need 3:1 — it needs to be visible, and
a **2px minimum bar width** is what makes it so. The smallest category on a real
ledger came to 0.24% of the track, which renders as a sub-pixel smudge without
it. That overstates a value that small, which is why the exact figure is printed
beside every bar.

**Both themes gained separation.**

| | was | now |
|---|---|---|
| light page vs card | 1.08:1 — a card's only edge was its border | 1.22:1 |
| dark page vs card | 1.11:1 | 1.19:1 |
| dark cover vs card | 1.05:1 — the cover sat *below* the cards | 1.40:1 |

Dark mode ran five slates inside twelve points of lightness, so page, cover, card
and banding were all the same grey. The range now spans `#0d1017` to `#313a4c`
and each surface has a job: paper is the desk, sheet is the card lifted off it,
band is ledger banding, board is the cover — the top of the stack, which is
where a cover belongs.

`test_assets.py` pins the relationship rather than the hexes: page and card must
be more than 1.15:1 apart in both themes (both of Phase 11's pairs fail it), the
ramp must derive from its own theme's tokens, and no chart mark may reference a
semantic colour.

### 18.5 Motion, observed

Charts animate in the direction they are read: horizontal bars grow from the axis
on the left, columns grow up from the baseline, and the stagger is by **rank**, so
the sweep is the ordering — the largest category arrives first. The hatched
excluded part is delayed 220ms behind the counted part, so the eye reads the
figure before the caveat.

Per CLAUDE.md, a static screenshot cannot see any of this, and `shoot.py`
actively waits for loading to finish. Observed with `scripts/motion.py` against
the shipped bundle:

| | Observed |
|---|---|
| Category bars | `bar-grow 0.32s`, delays `0s 0.04s 0.08s 0.12s 0.16s 0.2s` |
| Flow bar / excluded | `bar-grow 0.38s` / same, `delay 0.22s` |
| Month columns | `bar-in 0.34s`, delays `0s 0.06s 0.12s 0.18s` in each chart |
| Ledger-wide Day Rail | `bar-in 0.32s` |
| Reduced motion | `animation-name: none` on all four |

`motion.py` also reads the **computed** palette back out of the browser and
prints the contrast table above. The ramp is `color-mix(in oklab, …)`, whose
computed value is `oklab(L a b)` — so the values are resolved by painting them to
a canvas and sampling the pixel, which is what the operator actually sees. A
sentinel catches a colour the canvas cannot parse, so an unsupported value reads
as "unresolved" rather than silently as black.

### 18.6 What was looked at, and what looking found

Every page in both themes at 1440px and 390px, plus three states that are not
routes: the header menu open; Payees → Review changes with a real pending edit
(`/api/payees/diff` writes nothing, so this is safe against the operator's own
config); and **the state where something needs doing**.

That last one needed a harness change. Re-apply exists for when the ledger
disagrees with config, and the live ledger is reconciled — so the prompt, its
count and its red button were surfaces no shoot against real data could ever
produce. `shoot.py` now fakes the *preview* for two shots, from rows read out of
`tests/fixtures/statement.xls` through the parser (§16.6 holds: nothing displayed
is hand-typed), and restores it afterwards. Faking it in the harness is the
alternative to never looking at it.

Five things came out of reading the PNGs rather than the code:

1. **The shots were of the wrong bundle.** `shoot.py` renders whatever is in
   `web/dist/`, and a `tsc --noEmit` between two edits is not a build. The
   Re-apply page in the first run showed its pre-refactor copy. Rebuilt and
   re-shot. *A typecheck is not a build, and a screenshot is of a build.*
2. **The smallest bars were sub-pixel** — the smallest category read as an
   empty row rather than a small one.
   Hence the 2px floor.
3. **Out and In used different ramp steps**, which made shade look like it
   encoded direction — the one thing §16.4 forbids. Unified.
4. **A zero-height column still carried its dashed partial cap**, which reads as
   a bar being hidden rather than a quiet month. Dropped when the value is zero.
5. **`re-push 6 row(s)`** — on the red button that deletes and re-pushes the
   ledger. §17.5.2 ruled that out (`day(s)` is a form field, not a sentence) and
   then applied it to exactly one string; seven others were still doing it, in
   `Status`, `Password`, `Preview`, `Reapply` and the new component. All now use
   `lib/money.count`, and `test_assets.py` pins the rule.
6. **The re-apply button promised a backup it cannot take** — see §18.7. This is
   the one that mattered.
7. **`display: flex` on a paragraph**, in the first version of the dump line: it
   made every inline `<code>` and every text run a separate flex *item* and laid
   one sentence out as four ragged columns. Visible instantly in a screenshot,
   invisible to a typecheck.
8. **A page shot of five loading placeholders.** The per-page wait covered
   `.spinner` but not `.skel`, and the Ledger's second query has a skeleton — so
   the shot was a race that happened to be won until the charts got slower, and
   then produced a full-page picture of the loading state that looked entirely
   plausible. The loop now waits for no skeleton anywhere.
9. **"a slope through 1 points, 1 of them stubs"** — the month caption, on a
   ledger with a single bucket. Two pluralisations that only appear in the
   degenerate case, which is exactly the state a half-restored ledger is in, and
   which is how it was seen.

The harness grew twice more in the process, both times because a state could not
otherwise be seen. `shoot_written` runs the process in a scratch directory
holding a **copy** of `config/` and symlinks to `.env`, `archive/` and
`backups/`, so the page that follows a real config write can be photographed
without writing the operator's own config — verified afterwards: zero occurrences
of the scratch alias in `config/payee_aliases.yaml`. That immediately caught two
bugs of its own: a relative `docs/shots/<tag>` path resolved *inside* the scratch
dir (the shot was written there and deleted with it), and the second theme had
nothing left to change because the first had already written it, so the diff was
empty and the write button never appeared.

### 18.7 The one destructive action, and what now stands in front of it

Reading the finished screenshots found this, and it is the worst defect of the
phase: the button read **"Back up, then purge and re-push 6 rows"**, and the note
*directly underneath it* explained that this container cannot take a database
dump because it deliberately has no Docker socket (§15.1). The button promised
the one thing the page had just said it could not do, on the only action in the
app that deletes anything. What it actually backs up is `config/`.

Both halves of the fix, because either alone leaves the hole:

1. **The button says what it does.** "Purge and re-push N rows". The `config/`
   copy is still made and is still described in *What runs, in order*.
2. **The dump became a precondition, not a suggestion.** The container cannot
   *write* a dump, but it can *read* `backups/` — so "a dump exists and is newer
   than `REAPPLY_DUMP_MAX_AGE_MINUTES`" is a fact it can check and refuse on.
   Without one the button is disabled, and `POST /api/reapply/run` answers
   **409 `stale_backup`** naming `make backup`. Enforced server-side and checked
   **before** the config copy, the purge and the push — a disabled button is a
   courtesy, not a guard.

An hour is the window: long enough to run `make backup` and then do the editing
that led here, short enough that the dump is of the ledger being deleted rather
than of an earlier one. When the dump is fresh the page says so, names the file
and its age, and states plainly that the dump is not what this page copies.

This is not hypothetical. **The live ledger was found mid-recovery while Phase 13
was being finished**: 21 of 93 rows, a balance that reconciled with itself
against those 21 rows and against nothing else, and every surviving row created
inside one 20-second window — the signature of a
purge followed by a re-push that stopped. The web UI is ruled out, since
`/api/reapply/run` writes `backups/config-prereapply-<date>.tar.gz` as its first
step and no such file exists for that date; it was an interrupted CLI cycle. The
data was never at risk — `archive/` held both statements the whole time — but the
precondition above is exactly what makes the same interruption survivable rather
than merely recoverable-if-you-happen-to-have-a-dump.

### 18.8 Backup codes warn before zero, and `make check` was lying about them

The Ledger strip read "0 backup codes" in the first screenshots. That was an
artefact of the throwaway credential `shoot.py` injects, which never generates
any — the real account had eight. But the question it raised was the right one:
**can an account reach zero silently?** Two answers, both now fixed.

* **The warning started at zero.** Codes are single-use, so the count only ever
  falls, and at zero a lost phone leaves `passbook web-totp --reset` on the host
  as the only door (§16.2) — which is too late to be told. `LOW_BACKUP_CODES = 2`
  now drives an ochre state on the strip, the Status card and `make check`, and
  the Status card says which of the two situations it is.
* **`make check` was counting wrong, in the direction that hides the problem.**
  It grepped for 64-hex-character strings — which is also the shape of a
  remembered *device* digest. Measured: eight codes plus two devices reported as
  **"10 backup code(s) left"**. With two codes and two devices it would print
  four and say nothing, at precisely the point where saying something matters. It
  now reads the `backup_codes` array out of the JSON.

### 18.9 Definition of done

- Three nav items; every route and every action still reachable.
- No figure anywhere comes from a raw sum of withdrawals or deposits.
- The category chart's fills are `--ramp-*` only.
- Page and card are visibly different values in both themes.
- Charts animate in, and stop animating under `prefers-reduced-motion`.
- **No button claims to do something this container cannot do**, and the purge
  refuses without a recent dump — server-side, before any work.
- The backup-code warning fires above zero, and counts only backup codes.
- `test_pdf_matches_xls.py` runs without the operator's own files.
- The CLI still does everything it did.

---

## 19. Postmortem — the 2026-08-11 partial re-push

Recorded because it is ledger history, and because the failure was **silent by
construction**: a coherent ledger with 72 of 93 rows missing, no error anywhere,
and a balance that reconciled with itself.

### 19.1 What was observed

The live account held **21 of 93 rows**. The 21 were the first 21 rows in sheet
order, the balance was exactly the running balance after row 21 — self-consistent
and wrong — and every one of them was **created inside a 20-second window**,
2026-08-11 03:54:17–37 UTC. A clean stop, not a scatter.

So the sequence was: a purge ran to completion (0 tombstones remained, which
means `DELETE /api/v1/data/purge` did execute — §7.3), then a re-push began at
03:54:17 and stopped 20 seconds into a job that takes about ninety.

### 19.2 What it was not

| Ruled out | How |
|---|---|
| The web UI's re-apply | `/api/reapply/run` writes `backups/config-prereapply-<date>.tar.gz` as its **first** step. There is none for 2026-08-11. |
| Either Claude Code session | Session A's transcript ends 2026-08-10 21:05:50; session B had no activity between 2026-08-10 evening and 2026-08-11 10:10. Neither contains a purge or push on the 11th other than the recovery below. |
| An interactive shell command | `~/.bash_history` contains no `passbook` invocation at all, ever. |
| A scheduler | No user crontab, nothing relevant in `/etc/cron.d`, and D7 guarantees no cron container. |
| Firefly's own audit log | Would have named the caller, but the app container's `storage/logs/` only goes back to the recovery — the earlier day's file did not survive a container restart. |

**It is therefore unattributed, and stating that plainly is better than a
plausible guess.** What can be said: no automation on this machine does it, and
no session that could be inspected did it.

### 19.3 Why the stop was clean

Twenty seconds into a ninety-second job, everything stopped at once — not a
partial failure, an execution that ceased. D7 already says why that is possible:
**WSL2 stops when Windows sleeps.** That is not a theory here. While the recovery
below was being carried out, all four containers were found stopped with their
volumes intact, having gone down without anyone asking. A push loop against a
Firefly that vanishes mid-flight, or a process suspended with the distro, both
produce exactly this shape.

### 19.4 Why it was silently wrong

Every check that existed said fine. The balance was internally consistent, the
continuity invariant was never involved (it validates statements, not the
ledger), and `passbook doctor` has nothing to compare a row count against. The
thing that surfaced it was **the Phase 13 charts**: a spend figure roughly an
eighth of what was expected, and one month bucket where there were four.

### 19.5 The recovery, as run

1. **Protect a known-good state first.** `backups/firefly-2026-08-11.sql.gz` was
   a dump of the *broken* ledger. The newest dump predating 03:54 —
   `firefly-2026-08-10.sql.gz` — was verified with
   `make verify-backup FILE=…` — 93 rows, 93 distinct external ids, and both
   the balance and the earnings figure matching — and copied with its config
   tarball to `~/passbook-known-good/`,
   **outside `backups/`**, because `backup_remote.sh` prunes that directory to
   the newest `PASSBOOK_BACKUP_KEEP`. `backup-remote` was not run: the off-site
   copy must not become the 21-row state.
2. **Check for tombstones before pushing anything.** If the interrupted cycle
   had soft-deleted rows and died before the force-purge, the missing rows would
   be byte-identical to their own tombstones and Firefly would refuse them as
   duplicates — a push reporting "0 pushed, 72 duplicates skipped" and reading
   as success. That is the §7.3 trap. Measured: **0 trashed journals, 0
   tombstoned external ids.**
3. **No purge was needed.** `reapply_preview` reported the 21 survivors already
   matched current config, so the minimum action was a plain push of both
   archived statements: **72 pushed / 21 duplicates**, then **0 pushed / 32
   duplicates**.
4. **Verified against the database, not the API:** 93 rows with an external id,
   93 distinct, the balance and earnings figures both back where the archive
   says they belong, the opening balance intact and still carrying no external
   id, 0 tombstones, no duplicate external ids. Then through `ledger_analysis`:
   14 categories, four month buckets. A fresh dump now verifies identical to
   live.

### 19.6 What the recovery also corrected

The rebuilt ledger disagreed with the pre-incident one on one figure: **two rows
moved from Eating Out to Events**, and the `food` roll-up fell by the same
amount. That is not a restore error. Their payee token was re-assigned from the
food-tagged rule to Events **at 22:23 on 2026-08-10** — the diff is visible
between the `config-prereapply-2026-08-10` snapshot and the live file — and the
ledger was never reconciled afterwards, so it had been carrying a stale category
ever since. Rebuilding from the archive under current config fixed it.

This is exactly the drift §18.1 exists to surface, and it is why that notice now
appears on Payees whenever the ledger disagrees with config, rather than only at
the moment of a write.

### 19.7 The fix — purge intent and `--resume` (BUILT)

A purge that can die mid-flight must leave evidence. **Record intent before
deleting.**

* Before the first delete, write `backups/purge-intent-<timestamp>.json`: the
  external ids to be deleted, the statements to be re-pushed, and the expected
  final row count.
* Delete the intent file only after the re-push has been verified.
* An intent file that outlives its run is therefore an **unfinished cycle**, and
  it is detectable without guessing: `passbook doctor` reports it, the Ledger
  strip shows it, and the row count can be compared against the expectation.
* `passbook purge --resume` (and the UI's re-apply) completes it rather than
  starting over.

Two properties matter. It turns a silent partial state into a **stated** one, and
it makes the interrupted case *completable* instead of merely recoverable by
someone who happens to notice. §18.7's dump precondition is the other half: it
guarantees there is something to recover *from*.

**Built, and exercised on the real ledger.** `purge()` writes the intent itself
if the caller did not, so the guarantee lives in the function that deletes rather
than in the discipline of its callers. Measured end to end: a purge of 93 rows
recorded `purge-intent-20260811-173103.json`, `verify-ledger` then reported *93
archived rows MISSING, balance out by +4,931.91, 1 unfinished purge*, and
`passbook purge --resume` pushed both statements back (93 + 32 duplicates),
verified all five checks and cleared the record. The intent is **never** cleared
on the strength of an HTTP call returning — only on §20 passing.

### 19.8 Left open

* The rows in the two `family`-tagged categories do not carry the new tag: rules apply
  at store time, so the tag reaches existing rows only through a re-apply. The
  roll-up section simply does not appear until then, which is the honest
  degradation — it does not show a zero.
* `backup-remote` has not been run since the recovery, so the off-site copy is
  older than the restored ledger.

---

## 20. Ledger integrity — `passbook verify-ledger`

The gap §19 exposed, and the reason it is a section of its own: **§6.6 validates a
file, and nothing validated the ledger.** The continuity invariant runs at parse
time against a statement. Firefly was never checked against the statements that
built it, so on 2026-08-11 a purge plus an interrupted re-push left 21 of 93 rows
with a self-consistent balance, and 367 tests, `passbook doctor`, `make check`
and the status strip all passed for seven hours.

This is the one check that catches that corruption **whatever caused it** — an
interrupted purge, a row deleted by hand in Firefly's own UI, a restore of the
wrong dump. It matters more than the purge-intent record (§19.7), which only
covers the case where passbook itself was mid-operation.

### 20.1 The five checks

| Check | Asserts | On failure it says |
|---|---|---|
| balance | Firefly's balance equals the newest archived statement's closing balance | both figures and the signed drift — "out by +4,931.91" |
| rows | live `external_id`s equal the distinct `txn_id`s across every archived statement | how many are missing, how many are unexpected, and names the first five of each |
| trashed | no soft-deleted journals | the count, and that a re-push of identical rows will be refused (§7.3) |
| purge intent | no unfinished purge recorded (§19.7) | which files, and `passbook purge --resume` |
| opening balance | present, exactly one, carrying **no** `external_id` | which of the three it is, and that the missing id is what makes `purge`'s exclusion structural |

Exit code 7 when any check fails, so it can gate a script. `doctor` runs the same
checks through the same renderer, so the two cannot word things differently.

### 20.2 `ok` is tri-state, and that is the point

`Check.ok` is `True`, `False`, or **`None` — "not checked here"**. None is not a
pass and is never painted green.

That distinction is load-bearing for exactly one check. **Firefly's API cannot
count soft-deleted journals**: verified against the pinned tag, `routes/api.php`
exposes precisely two `data/*` routes — `DELETE data/destroy` and
`DELETE data/purge` — and neither lists them. Counting them needs the database,
which means `docker compose exec`, which the web container must never be able to
do (§15.1, asserted at AST level by `test_ops_only_ever_executes_rclone`). So:

* `passbook verify-ledger` **on the host** supplies the count and checks it.
* The Ledger strip reports that check as *unverified*, in ochre, with the other
  four still asserted.

Reporting a tick for something never looked at is §19's failure in miniature.

### 20.3 Where it surfaces

* `passbook verify-ledger` — the whole verdict, exit 7 on failure.
* `passbook doctor` — same verdict, after the configuration and reachability
  checks it already ran.
* **The Ledger strip, first item**, before Firefly's version and the token: every
  other item on that strip was green while the ledger held a third of itself.
* `purge --resume` runs it before clearing an intent — the record is only removed
  once the ledger says it is whole (§19.7).
* `/api/reapply/run` runs it as its last step and keeps the intent if it fails.

### 20.4 What it deliberately does not do

**It does not repair anything.** It reports, names the remedy, and exits. §19.5 is
the recovery, and it starts with a verified backup — a check that silently
"fixed" a ledger would be the most dangerous thing in this repo.

It also does not re-derive categories or amounts: that is `reapply_preview`'s job
(§15.4), which answers a different question — "does the ledger match *config*"
rather than "does the ledger match the *statements*". Both matter, and the second
is the one that noticed 72 missing rows.

---

## 21. Phase 14 — multiple accounts

Built before the shareable snapshot deliberately: a friend who starts on a
single-account build and later adds a second account must not need a migration,
and the `external_id` scheme is the one thing that cannot be changed afterwards
without touching every row in the ledger.

### 21.1 The collision, and the namespace — do this first

**`external_id` is not unique per user.** The bank's transaction id is `YYYYMMDD`
plus a per-date ordinal (§6.1) and Canara sequences it **per account**, so a
second Canara account emits *the same ids*. Measured on two fixtures built from
one source with different synthetic account numbers: **93 of 93 ids identical**,
and — because they were chosen to — the same masked account, `****1111`.

What that breaks, in order of how quietly it breaks it:

| | Consequence |
|---|---|
| Any dict keyed on `txn_id` across accounts | **Silent data loss.** The payee inventory and the Day Rail's clock map both did this: two accounts, 186 rows, 93 survive, no error. |
| `verify-ledger` | Reports every *other* account's rows as missing (§20 compared one Firefly account against the whole archive). |
| The Day Rail | Joins one account's clock onto the other account's transaction. |
| `purge` | Account-scoped already (`find_candidates` walks one account), but its force-delete of trashed rows is user-global — documented in §7.3 and now more consequential. |
| `archive/` | Canara names every export for a range identically, so two accounts filed into one folder means the second **overwrites** the first. |

**The scheme: `external_id = "<slug>-<txn_id>"`**, e.g.
`canara-1111-20260509000001`.

`slug` is the registry key: `[a-z0-9-]+`, defaulted to `<bank>-<last4>` when an
account registers itself, disambiguated to `<bank>-<last4>-2` if that is taken,
and **immutable once rows carry it** — changing it orphans every pushed row from
the statement that produced it.

Why the slug and not the last four alone: **the last four are not unique.**
`validate.assert_account` has documented since Phase 2 that two accounts can share
them, and the second fixture proves the case. Why the bank is in it: the registry
carries `bank` from day one, and two banks can each hold an account ending 1111.

Properties that made this the choice:

* **Derivable** from statement + registry alone, so a re-push reproduces the id
  byte for byte — §6.1's idempotency property survives.
* **Readable.** The account is visible at a glance in Firefly's UI, in a log line
  and in a purge-intent file. An opaque hash would have been unique too, and
  §6.1 already rejected a hash where a meaningful key existed.
* **Carries nothing secret.** Bank name and last four; §11's masking rule already
  prints both, and §6.8.1 established the last four are not credential-class.
* **Regex-separable from the old form** — `^\d{14}$` versus
  `^[a-z0-9-]+-\d{14}$` — which is what makes the migration detectable and the
  reads tolerant.

**Tolerant reads, strict writes.** Every read accepts both forms
(`service.txn_id_of`, `slug_of`, `is_namespaced`); every write namespaces. So a
ledger pushed before this phase keeps verifying, and §21.2's migration can be run
when it suits rather than being forced by a version bump. `verify-ledger` gains a
sixth check that reports how many rows still carry a bare id, and says that a
second Canara account cannot be added safely until they are migrated.

### 21.2 Migrating the existing ledger

Not automatic, and not silent. The path is the proven one from §19.5 — and it is
the *only* path, because an `external_id` cannot be edited in place through the
API without rewriting the row:

1. `make backup` — a dump newer than the purge, which §18.7 now requires anyway.
2. `passbook purge --confirm --yes` — records intent (§19.7) and force-deletes
   trashed rows so the re-push is not refused as duplicates.
3. `passbook purge --resume` — pushes every archived statement back, now with
   namespaced ids, verifies §20, and clears the intent.
4. Expect afterwards: **the same row count and the same balance as before**,
   and the `id namespace` check reading `all N row(s) namespaced canara-1111-*`.
   If either figure moved, the migration lost rows — go back to step 1's dump.

### 21.3 Zero config for one account

Someone with one account should never learn this feature exists.

* **The first statement registers its own account.** `resolve_account` sees an
  empty registry, takes the account number from the statement (§6.3), and writes
  `config/accounts.yaml` with a default slug. The Firefly asset account comes
  from `PASSBOOK_ASSET_ACCOUNT` if set, else from Firefly when it holds exactly
  one asset account — **never guessed between several**, which is the rule
  `doctor` has followed since §7.2.
* **Account two is deliberate.** `passbook accounts add <statement>` — because it
  also needs a Firefly asset account chosen, and two accounts sharing one would
  merge in Firefly whatever the registry said.
* **An absent registry falls back to the two env vars**, so a pre-registry
  install, `make dr-drill` and every test written before this phase keep working
  untouched.

### 21.4 The registry

`config/accounts.yaml`, gitignored like `rules.yaml` and `payee_aliases.yaml`
because it names real account numbers (§11), and carried in `make backup`'s
encrypted config tarball — which is its only copy.

```yaml
accounts:
  - slug: canara-1111          # external_id namespace; IMMUTABLE once rows exist
    bank: canara               # only 'canara' has a loader today
    account_number: "…"        # matched in FULL, never on the mask
    asset_account: Canara savings
    label: Savings             # optional; what a switcher would show
```

`bank` is present from day one although only `canara` is supported. A second bank
is the obvious next step, and adding the field later would mean reshaping the
registry *and* every `external_id` in the ledger — the migration this phase
exists to do exactly once.

Refused loudly, never guessed around: a duplicate slug (it would merge two
ledgers), a duplicate account number, a **duplicate `asset_account`** (they merge
in Firefly regardless of the registry), an unsupported bank, or a slug that
cannot live inside an `external_id`. Account numbers are masked even in those
error messages — §11 holds in error paths, which is where full numbers usually
leak.

### 21.5 What stays SHARED — do not split these later

**`config/payee_aliases.yaml` and `config/rules.yaml` are shared across every
account, on purpose.**

The same person's payees are the same regardless of which account paid them:
`ZEPKV JYX` is the same counterparty whether the money left the savings account
or a second one, and D10's hard-won knowledge about a truncated token
(`NYXN XWUBQ # a fast-food franchise, not a person`) is knowledge about the *world*, not
about an account. Firefly's categories and tags are per-user anyway, so splitting
the rules would create two rule sets that must agree and no mechanism to make
them — the drift shape this project has been bitten by three times (§9).

If a rule ever genuinely needs to be account-specific, express it as a category
the operator only uses on that account. Do not split the files.

### 21.6 What is per-account

| | Scope |
|---|---|
| `external_id` namespace | per account (§21.1) |
| The continuity invariant (§6.6) | per statement, therefore per account already |
| The opening balance | per Firefly asset account, one each |
| `archive/` | `archive/<slug>/<YYYY-MM>/`, because the bank's filenames collide |
| `verify-ledger` | one verdict per account; `doctor` prints them all |
| Purge intent | records the slug, so a resume knows which ledger it is finishing |
| The Day Rail's clock map, the payee inventory | built from one account's statements — `service.statements_for()` narrows, `account_transactions()` dedupes *within* an account |
| `payee_aliases.yaml`, `rules.yaml` | **shared** (§21.5) |

Statements are attributed by **what the file says**, not by which folder it sits
in: a statement moved by hand into the wrong directory must not change whose
ledger it joins.

### 21.7 §6.7 changed its question

From *"is this MY account?"* to *"WHICH of my accounts is this?"* — matched on the
full account number, never the mask, because two accounts can share their last
four.

The refusal did not change and got more specific. `UnknownAccount` subclasses
`AccountMismatch`, so every front end already turns it into a 422 that **deletes
the staged file**, and it now carries the masked number and the registered slugs
so the UI can offer to add the account. **A statement for an unregistered account
can never silently import.**


### 21.9 The UI

**The switcher renders only when more than one account is registered**, and the
*server* decides that — `/api/accounts` returns `multiple`, so the client never
has to. There is no "1 of 1" dropdown to explain and no hint that accounts are a
concept here. Every other query sends `?account=<slug>` only when a selection
exists, so a single-account install issues byte-identical requests to the ones it
made before this phase.

A native `<select>` on the cover, not a scripted menu: one tap on a phone,
keyboard-navigable and announced without a handler. The Day Rail earned an SVG
because no element does that job; a dropdown is not that.

**The selection lives in `localStorage`, not in the URL.** It has to survive a
reload, which a query parameter would not unless every link carried it, and it is
a *view preference* rather than an address — two accounts are one ledger seen two
ways, and a shared link that silently reframed someone else's page would be worse
than one that does not carry the state. A slug the registry no longer knows falls
back to the first account rather than erroring: a selection left in a browser
after an account is removed should show data, not a broken page.

Every query is keyed on the account as well as the endpoint, so switching
refetches rather than showing the previous account's figures under a new name.

#### What "All accounts" does, and why

**Everything on the Ledger page combines, except the balance.**

Spend, income, the category breakdown, the roll-ups, the month buckets and the
Day Rail are each a **sum over transactions**, and §8/§8.1's exclusions are
decided per transaction — so combining cannot change what any figure means. ₹40
of Shopping on one account and ₹25 on another is ₹65 of Shopping.

The **Day Rail** combines for a stronger reason than arithmetic: time of day is a
property of the *person*, not of the account. Which account paid for a 01:51
canteen run is the least interesting thing about it, and splitting the rail by
account would answer a question nobody asked while making the signature element
sparser. (The clock map is still built per account and keyed on the pushed
`external_id`, never on the bank's bare id — those collide, §21.1.)

The **balance is the exception, and it is labelled rather than hidden.** The sum
across accounts is a true figure — it is what those accounts hold together — but
unlike every balance this app has shown since Phase 7 it reconciles against **no
statement's closing figure**, and that reconciliation is exactly what the card
has implied. So in "All accounts" the card reads **"Balance, summed"** and lists
the per-account figures underneath. The alternative — one bare total — would be
the §19 shape again: a number that is right, presented as a number that has been
checked.

Two more per-account aggregations, both chosen so a warning cannot be diluted:

* **Sync staleness** shows the *worst* age across accounts, never an average.
* **The ledger verdict** in the strip is the worst across accounts, with each
  check prefixed by its slug (`canara-cash: opening balance: MISSING`). One
  account's rows are missing from the other by definition, so a single combined
  verdict would be noise.

#### Upload routes by the statement, not by the switcher

A statement belongs to one account as a matter of fact (§21.7), so routing reads
the account number in the file and ignores the selection entirely. Preview then
**says which account it will land in**, before the push — and only when more than
one account exists, because a single-account operator has nothing to
disambiguate. The one thing worse than routing silently would be routing silently
into the account you happened to be looking at.

#### Registration order — checked, and it does not matter

The zero-config path registers whichever statement arrives first, so someone who
uploads their *second* account first makes it the primary. Measured both ways with
the colliding fixtures:

| | first-then-second | second-then-first |
|---|---|---|
| routing (by account number) | correct | correct |
| rows attributed per account | 93 / 93 | 93 / 93 |
| `external_id` uniqueness | 186 distinct | 186 distinct |

**Nothing downstream depends on position.** Every consumer resolves through the
registry by account number; the three places that read `accounts[0]` are the
scope *fallback* and two labels, all reads, all overridden by a selection.

Two things are order-flavoured, both cosmetic and neither a correctness issue:

1. **Which account gets the plain slug.** With colliding last four digits the
   first to register takes `canara-1111` and the second `canara-1111-2`. The slug
   is immutable once rows carry it (§21.1), so `passbook accounts add --slug`
   exists for anyone who cares which is which. In the common case — last four
   that differ — the default is unique and order is irrelevant.
2. **Which account an unset switcher shows first.** Overridden by one click, and
   remembered after it.

#### Two things only the screenshots caught

Both were written, typechecked and shipped into a build before anyone looked.

1. **The cover said `Canara · SB · INR`** — a literal since Phase 7, when one
   account was the only possibility. Beside a switcher it is a claim about
   whichever account happens to be first, printed next to a control that may be
   showing a different one. It now reads `bank` from the registry, says nothing at
   all when more than one account exists (the switcher is right there, and two
   labels for one fact is how they drift), and has dropped `SB` because the
   registry records no account type and a guessed one is wrong for the first
   non-savings account added.
2. **At 390px the switcher pushed `ACCOUNT` out of the header.** The nav row was a
   `nowrap` scroller with `scrollbar-width: none`, so sign-out, password, second
   factor and status detail were off-screen with nothing to suggest a swipe — all
   four unreachable on a phone the moment a second account was registered, which
   is precisely what §17.1 forbids. The row wraps now and the switcher takes a
   full-width line of its own, so the account name is never truncated either. The
   header can afford it: it carried six nav items in Phase 11 and carries three
   and a menu since §18.1.

The failing-verdict line in the strip needed a third pass for the same reason —
`.strip__item` is `inline-flex`, and a flex container sizes to its content, so
`white-space: normal` and `flex-basis: 100%` both looked right and the sentence
still ran off the card. It is `display: block` when it fails, measured wrapping
to 2 lines at 1440px and 5 at 390px.

### 21.8 Definition of done

- [x] `external_id` namespaced by slug; reads tolerate both forms.
- [x] Registry in `config/accounts.yaml` with `bank` from day one.
- [x] Upload/push route by account number; unknown accounts refused.
- [x] Zero config: the first statement registers itself; env vars still work.
- [x] `verify-ledger`, `doctor` and purge intent are per-account.
- [x] `archive/<slug>/` so bank filenames cannot collide.
- [x] Aliases and rules documented as shared (§21.5).
- [x] Tested against a second account that collides on ids *and* last four.
- [x] **The migration of the existing 93 rows** — run 2026-08-11: 93 rows, the
      balance and earnings unchanged, all `canara-1111-*` in the fixture's own
      namespace shape, zero bare ids, `verify-ledger` clean. Confirmed in SQL as
      well as through the app.
- [x] **UI: account switcher, per-account scoping, "All accounts"** (§21.9),
      screenshotted in both themes at both widths — including the single-account
      case, which shows no switcher at all.

---

## 22. Phase 15 — the public release

Fourteen phases were built for one operator on one laptop. This one makes the
repository something a stranger can clone, run, fork and contribute to, without
making it something a stranger can read the operator's finances out of.

Five things had to be true before it could be published at all, and they are the
five subsections below. The order is the order they blocked each other in.

### 22.1 Documentation cites fixtures, never a live ledger

**The blocker.** Fourteen phases of documentation carried real balances, real
payee tokens — several of them family members — real UTRs, real masked VPAs, the
operator's account slug, and a table of shops actually visited. Every one of
those is permanent and searchable the moment the repository is public.

The scrub is the easy half. The hard half is that **a one-time scrub is worth
nothing**: phase 16 writes a new balance into a paragraph and it is public
forever. So the rule is stated once and enforced by a test.

> **Tracked documentation cites FIXTURE values, never live ledger values.**
> Every rupee figure, payee token, account number, slug, UTR and VPA in a
> tracked file comes from `tests/fixtures/statement.xls` and its companions, or
> is a synthetic constant this repo owns. Real figures belong in a session
> report to the operator, in a terminal, or in a gitignored file. Never in
> something git tracks.

CLAUDE.md carries it as non-negotiable 14, with the fixture's whole vocabulary
tabulated so there is one obvious answer to "what number do I write here".

**`tests/test_docs.py`, also `make audit-docs`.** It reads **prose, not program
text** — every `.md` in full, and the comments and docstrings of everything else.
A test asserting a round `Decimal` amount is arithmetic; a comment saying *"this
ledger reads …"* and then quoting it is a disclosure. Scanning code literals as
well produced enough noise to make the check ignorable. Account numbers and UTRs are
the exception and are scanned everywhere, because a 12-digit run has no innocent
form here.

The amount allowlist is **derived from the golden fixture**, not typed out, so a
regenerated fixture updates it and a figure that is not in the fixture cannot
quietly become allowed. It runs in `make test` and as its own CI job.

**What it deliberately cannot catch**, stated rather than glossed: a payee
token, a category name, a person's name. Those have no machine-checkable shape,
and pretending otherwise would be a green tick for something never looked at
(§20.2). CONTRIBUTING.md carries that half as a human rule.

**Where a lesson needed a magnitude, it became a ratio.** "Three times the true
spend" carries the whole lesson that a pair of real rupee figures carried, and
carries it without the disclosure — and without rotting when the ledger moves.

The one-time audit found leaks in more than documentation: real tokens in four
code comments and three test files, a real masked VPA inside the PDF wrap-test
cases, one bank's branch and address named in a leak *test*, and four tests
hardcoding the author's own checkout path. That last one was also a plain bug —
those tests could only ever pass on one machine in the world.

### 22.2 A schema version, and `make upgrade`

**Users pull. `git pull` is silent.**

§21.2's migration needed a backup, a purge and a resume. A stranger pulling that
blind and running `make sync` would have got a ledger holding two incompatible
id forms, `verify-ledger` reporting rows missing, and nothing on screen ever
having said a migration existed. That is this project's recurring failure shape —
not an error, a silence.

`src/passbook/migrations/` is a registry: one module per migration, discovered by
filename, ordered by `VERSION`, with `SCHEMA_VERSION` derived from the highest
one found so there is no second list to keep in step. **§21.1's namespacing
ships as `m001`, the baseline** — a no-op on any install built by a version that
has the file, and the real thing for one that predates it.

Three properties, in the order they matter:

1. **Detection is from the data, never from a marker.** Each migration answers
   `pending()` by looking at the live ledger — the baseline counts rows still
   carrying a bare transaction id. `config/schema-version` records what was
   applied and **nothing trusts it**. A stored claim that something is fine,
   which nobody re-checks, is exactly §19 in miniature. A check that *cannot
   answer* is reported as pending with the reason; "could not determine" must
   never render as "nothing to do".
2. **The backup is a precondition, not advice.** No dump newer than
   `REAPPLY_DUMP_MAX_AGE_MINUTES` and `make upgrade` refuses — the same shape
   §18.7 put in front of the re-apply button, for the same reason: this deletes
   rows before it re-pushes them.
3. **Nothing is recorded until §20 passes.** Same rule as the purge intent
   (§19.7): the record is cleared on the ledger being whole, not on a function
   returning.

A migration never writes its own delete. `ctx.purge_and_repush` is the same code
`passbook purge --confirm --yes` and `--resume` run, handed in as a callable — a
second copy of the most dangerous path in this project is the last thing a
migration should own. And a migration that would delete rows `archive/` cannot
rebuild raises instead: that is data loss with a progress bar.

### 22.3 Licence — AGPL-3.0-or-later

#### Does Firefly III's AGPL reach us? No.

The question had to be answered before one could be chosen, and it was answered
by reading the licence rather than recalling it.

Firefly III v6.6.6 ships the **GNU Affero General Public License, Version 3**
(read from `LICENSE` on the pinned tag), including *"Section 13. Remote Network
Interaction"*, which requires that **"if you modify the Program"**, users
interacting with it over a network must be offered its Corresponding Source.

passbook:

* **does not modify Firefly.** `docker-compose.yml` pins the official image
  `fireflyiii/core:version-6.6.6` and pulls it at run time. Nothing is patched,
  vendored or rebuilt.
* **does not link or copy any Firefly code.** It speaks HTTP to a documented
  REST API from a separate process, in a different language.
* **does not redistribute Firefly.** Referencing an image tag distributes
  nothing.

So §13's trigger — modification — never fires, and passbook is not a derivative
work under §0 either: calling a published network API is not incorporating a
program. **AGPL-3.0 does not reach this code.** The one change that would make
it reach: vendoring or patching Firefly's source, at which point the obligation
attaches to *that patched Firefly*.

#### Choosing it anyway

passbook is therefore free to pick any licence, and picks AGPL-3.0-or-later for
three reasons:

1. **It matches the ecosystem.** Firefly III is AGPL-3.0. A self-hosted personal
   finance tool sitting beside it, under the same licence, has no compatibility
   friction for anyone packaging or bundling both.
2. **It closes the hole that actually exists here.** The obvious commercial
   deformation of this project is "run it as a hosted service and give nothing
   back" — someone else holding other people's bank statements on a licence that
   asks nothing in return. §13 is exactly the clause for that. MIT and Apache-2.0
   are not.
3. **It costs an individual user nothing.** Running it on your own machine, for
   yourself, triggers no obligation at all. The copyleft only bites on
   distribution or on offering it to others over a network.

The alternative considered was **Apache-2.0** — permissive, with an explicit
patent grant, and better for corporate adoption. Rejected because corporate
adoption is not a goal of this project and (2) is.

**No dependency constrains the choice**, checked rather than assumed: every
installed distribution's own metadata was read, and the two non-obvious ones
verified against the FSF's licence list. `waitress` is Zope Public License 2.1,
which the FSF calls *"a lax, permissive non-copyleft free software license which
is compatible with the GNU GPL"*; `pikepdf` is MPL-2.0, whose §3.3 the FSF says
*"provides indirect compatibility between this license and … the GNU AGPL
version 3"*. Everything else is MIT, BSD, MIT-CMU or Apache-2.0.

The three redistributed font families are **OFL-1.1**, and the subsets ship with
the licence text and all three copyright lines (`NOTICE.md`,
`frontend/src/fonts/OFL.txt`). Subsetting is a Modified Version under clause 2
and is permitted; the family names are unchanged, which clause 5 allows because
it forbids renaming a derivative *to* a reserved name, not distributing a subset
under it.

### 22.4 Setup for someone who is not a developer

**D8 stays the operator's documented configuration and stops being the default.**

Docker Engine inside WSL2 needs systemd enabled in `/etc/wsl.conf`, a
`wsl --shutdown` from PowerShell, a package install inside the distro, and a
group change with a re-login — four manual steps, each of which fails quietly in
its own way. Docker Desktop is one GUI installer, covers Windows, macOS and
Linux, and manages the WSL2 backend itself. The trade that made D8 right for the
operator (no Windows-side daemon, everything in the distro) is not the trade a
first-time user is making.

Both are supported and nothing in the stack changes between them. D8 is recorded
in `docs/setup.md` as the alternative, with the reason the shared default
differs.

**The ordering bug this surfaced, which nothing had exercised.**
`docker-compose.yml` declares `FIREFLY_TOKEN: ${FIREFLY_TOKEN:?…}` on the `web`
service, so compose refuses to start **anything** until a token exists — and the
token can only be created from inside a running Firefly. A single
`docker compose up` on a fresh install therefore cannot work, and the first
thing a new user would have seen is an interpolation error naming a variable
they had never heard of. It had never been hit because the operator's `.env` has
had a token in it since Phase 3.

Both `make up` and the wizard now start in two stages: database and Firefly
first, then the rest once there is a token. `make up` prints the four steps to
get one rather than failing.

Three pieces:

* **`scripts/preflight.py`** — what is missing, why it is needed, and a URL.
  Standard library only, because it has to run on a machine where nothing is
  installed yet. "docker: not found" is a true statement that helps nobody.
* **`scripts/setup.py`** (`make setup`) — the wizard. Generates secrets, brings
  the stack up in stages, walks the user to **Options → Remote access and
  tokens** (*not* the Command line token, which cost the operator an hour and
  which earlier revisions of this file sent people to), **validates the token by
  using it**, lists the asset accounts to choose from, and sets the web
  password. Re-runnable, and it never overwrites a secret.
* **Three launchers**, one per OS, double-clickable, each deferring to
  `scripts/launch.py` — which runs the wizard if the install is unconfigured and
  otherwise starts the stack and opens the browser. They pause on failure: a
  launcher that closes its own window on error tells the user nothing, which is
  worse than not existing.

**The token is validated, not merely shaped.** A shape check catches the
Command-line-token mistake early and says exactly what went wrong; then the
token is used against `/api/v1/about`, because a credential that has not
authenticated anything is a guess.

**The default port path is now exercised.** The operator's own instance runs
Firefly on 8090 because a Windows service holds 8080 (§5), and nothing had run
end to end on the default since. Preflight checks 8080 and 8081, reports a held
port as a warning rather than a failure, and names the WSL mirrored-networking
trap where `ss` inside the distro shows nothing at all.

`rules.example.yaml` ships **only the three §8 rules** and
`payee_aliases.example.yaml` ships no aliases at all — D10 holds: categories are
the operator's to derive, never ours to guess.

### 22.5 The bank registry

Phase 14 put `bank` in the account registry from day one and supported exactly
one value. Publishing makes that field's promise real, because "I bank with
HDFC" is the first thing a stranger will say.

Everything that is *about Canara* rather than *about statements* is now one
`Bank` value in `src/passbook/banks/canara.py`: column spellings including the
bank's own misspelling, the date format, the sentinel labels, the metadata
labels, the narration grammars, the PDF password rule, and a `detect` function.
`loaders/` and `narration.py` became bank-agnostic machinery that reads those
fields. **Adding a bank is a new file and a `register()` call** — no edit to the
loaders, the validator, the pusher, the UI, or any list.

Four decisions worth recording:

* **A value object, not a base class.** Subclassing invites
  `class HDFC(Bank)` with an overridden `from_rows`, and then there are two
  copies of the balance-continuity invariant. A dataclass of data and small pure
  functions cannot be overridden into a second ledger. `from_rows` is explicitly
  the one function no bank may replace.
* **Detection reads content, never the filename or the config.** Banks name
  every export for a date range, and §21.6 already rules that a statement is
  attributed by what it says. Exactly one registered bank must claim a grid;
  two claims raises rather than picking, because guessing there attaches a
  statement to the wrong dialect and lands plausible, wrong rows.
* **`UnknownBank` is a `ParseError`.** Every front end already turns one into a
  422 that deletes the staged upload; a bare `LookupError` would surface as a
  500 and leave the file in `inbox/`, where the next `make sync` would find it.
* **`config.SUPPORTED_BANKS` is gone**, replaced by `supported_banks()` reading
  the registry. A second hardcoded list is the drift shape this project has been
  bitten by three times (§9).

`docs/adding-a-bank.md` is the whole job in one page: the interface, the
magic-byte sniffer, the registry's `bank` field, the continuity invariant a new
dialect must satisfy, how to build a redacted fixture, and the five tests a PR
needs. `tests/test_banks.py` registers a complete second dialect in nine lines
of data and asserts it coexists with Canara — the worked example that keeps the
document honest.

### 22.6 Contributor infrastructure

`CONTRIBUTING.md`, `SECURITY.md` with private disclosure (this handles financial
data, so a public issue is the wrong first move), issue templates for bug,
feature and new-bank, a PR template, and CI running the suite on two Python
versions plus `audit-docs` and a real frontend **build** — because a typecheck
is not a build (§18.6).

The new-bank template asks for the header row verbatim *including
misspellings*, two narration lines with the values changed but the shape kept,
and whether there is a transaction id column. That is most of the analysis, and
it is the part only someone holding the statement can do.

Every template's final checkbox is the same one: no real account numbers, no
statement file, nothing unredacted. A traceback with a real narration in it is a
leak that lives in a public issue forever.

### 22.7 Definition of done

- [x] No live-ledger figure, payee token, account number, UTR or VPA in any
      tracked file, and a test that fails on one. Verified by a one-time audit
      against the operator's real values, read out of the private checkout at
      run time, plus `make audit-docs` standing guard afterwards.
- [x] `make upgrade` detects pending migrations from the ledger, backs up first,
      runs them in order, and records nothing unless §20 passes.
- [x] A licence, with the Firefly III question answered from the licence text
      on the pinned tag rather than from memory.
- [x] Adding a bank is one file, one `register()` call, and a fixture —
      demonstrated by a second dialect registered inside `tests/test_banks.py`.
- [x] Screenshots in the repository are generated from the fixture, through a
      scratch stack that can only see the fixture.
- [x] A fresh clone installs and passes its own suite with no Docker, no
      Firefly and no `.env`: **644 passed, 73 skipped**, and the three failures
      that run found were all fresh-clone bugs rather than false alarms —
      four tests naming one absolute checkout, a Firefly probe gated on the web
      UI's port, and a manifest test that answered from the SPA fallback when no
      bundle had been built.

**Two things are NOT verified here, and saying so is the point of §13:**

- **The full `make setup` path has not been run end to end on a clean machine.**
  Every service in `docker-compose.yml` carries a fixed `container_name`, so a
  second stack cannot come up beside a running one, and the machine this was
  built on has a live install on it. What *was* run: `preflight` from a fresh
  clone, `docker compose config` validating, the staged-startup logic, and the
  wizard's own steps in isolation. The wizard needs one clean-machine run before
  §22.4 can be called finished.
- **The default host port 8080 has not been bound.** A Windows service holds it
  on this machine (§5), which is the reason `FIREFLY_HOST_PORT` exists at all.
  What was checked instead: nothing in the codebase names the author's port —
  `.env.example`, `docker-compose.yml`'s `${FIREFLY_HOST_PORT:-8080}`,
  `config.py`'s default and `test_stack.py`'s fallback all say 8080, and
  `preflight` warns when it is held. That is a code audit, not a bind.
