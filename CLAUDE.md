# CLAUDE.md

Working instructions for Claude Code — and for any contributor — in the
`passbook` repository. `SPEC.md` is the source of truth for *what* to build.
This file covers *how to work*.

---

## What this is

A pipeline that turns weekly Canara Bank statement downloads into a categorised
ledger in a self-hosted Firefly III instance. Single operator, one machine,
everything bound to `127.0.0.1`. See `docs/adding-a-bank.md` if you want it to
read a different bank's export.

## Environment facts

- **The supported default is Docker Desktop** on Windows, macOS or Linux — one
  installer, three platforms, and it manages the WSL2 backend itself. See
  `docs/setup.md`.
- **The author's own machine runs Docker Engine inside WSL2 Ubuntu** with
  systemd enabled via `/etc/wsl.conf`. That is SPEC D8, and it stays a
  documented alternative rather than the default because it needs three manual
  steps a non-developer will not get through.
- On WSL2, the repo must live under `~/` on ext4. **Never** `/mnt/c/...` —
  cross-boundary bind mounts are pathologically slow and break Postgres file
  permissions. `make check` enforces this.
- WSL2 stops when Windows sleeps. Nothing scheduled is reliable. All automation
  is manual, via `make`.
- Python managed with `uv`. Currency INR, timezone `Asia/Kolkata`; both are
  settable, and nothing else has been exercised end to end.

## Ground truth about the statement file

Verified against a real 3-month export (93 transactions). Do not re-derive these
from assumptions:

- It is a **genuine OLE2 `.xls`** (`D0 CF 11 E0`). Read with **`xlrd` 2.x**.
  `openpyxl` will raise `InvalidFileException` — that is expected, not a bug to
  fix.
- Header row: `Date | Trasnaction ID | Withdrawals | Deposits | Balance | Remarks`.
  **"Trasnaction" is misspelled by the bank.** Match it; do not correct it.
- The narration column is **`Remarks`**.
- **Every cell is a string**, including amounts (`'10,000.00'` — comma
  separators). Never let pandas infer dtypes. Strip commas, parse `Decimal`.
- **An empty amount cell contains a single space `' '`, not `''`.** Always
  `.strip()` before testing emptiness. This is the likeliest source of a silent
  bug.
- Dates are `DD-MMM-YYYY` uppercase (`09-MAY-2026`).
- Rows 0–8 are preamble; row 9 is the header; row 10 is an `Opening Balance`
  sentinel; the last row is a `Closing Balance` sentinel. Scan for the header
  rather than hardcoding row 9.
- The `Trasnaction ID` is `YYYYMMDD` + a 6-digit daily sequence, and that tail
  is a plain 1..n ordinal within the date — so the key is **derivable**, not
  merely readable. SPEC §6.1.
- The PDF export is encrypted **RC4-40** (`/V 1 /R 2`) and the password is the
  **last four digits of the account number** — not the Customer ID. SPEC §6.8.1.

## Non-negotiables

1. **Money is `Decimal`, never `float`.** Anywhere. No exceptions.
2. **Strip the trailing `DD/MM/YYYY HH:MM:SS` from UPI narrations before
   splitting on `/`.** The timestamp contains slashes and will otherwise produce
   four spurious tokens. SPEC §6.5.
3. **The balance-continuity invariant must pass before anything is pushed.** It
   is verified to hold cleanly on real data — 93 rows, 0 breaks — and on the
   committed fixture. So if it fails, the parser is wrong, not the data. Fail
   loudly with the row index. **Never soften or skip this check to make a test
   pass.**
4. **Never log or commit** the account number in full, the customer ID, or the
   Firefly token. Mask account numbers to last 4.
5. **The customer ID is a credential** wherever it appears — including inside
   `PMSBY` narration strings.
6. **Nothing under `inbox/`, `archive/`, `backups/`, or `.env` gets committed.**
7. **Never suggest uploading a statement to an online converter.** Privacy-fatal.
8. **Do not ship guessed merchant rules.** Canara truncates UPI payee names to
   ~10 chars and the traffic is mostly person-to-person. A rule matching
   `"Swiggy"` will never fire and will create false confidence. Rules come from
   `passbook payees` output. SPEC D10.
9. **Never total the ledger by hand.** Every spend or earnings figure — a card,
   a chart, a CLI line — comes from `service.ledger_analysis`, which applies
   SPEC §8/§8.1. Firefly counts every withdrawal as spend and every deposit as
   income *by type*; measured on one real three-month ledger, that read **three
   times** the true spend and **1.6 times** the true earnings. Investments,
   Transfers, Credit Card and Verification are movement, not spending
   (`not_spend` in `config/rules.yaml`); `not-earnings`-tagged deposits are
   money coming back, not earned. A naive sum is not approximately right, and it
   looks fine.
10. **Never key anything on `txn_id` across accounts.** The bank sequences it
    per account, so two Canara accounts emit identical ids — measured on the two
    committed fixtures: 93 of 93, and the same masked last four. `external_id`
    is `<slug>-<txn_id>` (SPEC §21.1); reads go through `service.txn_id_of`,
    writes through `Account.external_id`. A dict keyed on the bare id merged two
    accounts and kept 93 of 186 rows with no error at all.
11. **A green tick for something you did not check is a lie.** `Check.ok` is
    tri-state — `True`, `False`, `None` — and `None` renders as *unverified*, in
    ochre, never as a pass. A ledger once held 21 of 93 rows for seven hours
    behind an all-green strip (SPEC §19, §20).
12. **Never repair a ledger from a check.** `verify-ledger` reports, names the
    remedy, exits 7. Recovery starts with a verified backup (§19.5); a check
    that silently fixed things would be the most dangerous thing in this repo.
13. **Chart marks use `--ramp-*` and nothing else.** `--stamp` acts, `--ochre`
    asks, `--verdigris` reconciles; a bar wearing one of them destroys the only
    discipline the palette has. Ten categories are separated by one ink at five
    densities ordered by magnitude, never by ten hues. SPEC §18.4.
14. **Tracked documentation cites FIXTURE values, never live ledger values.**
    This one gets its own section, because it is the rule most easily broken by
    accident.

## Tracked docs cite fixtures, never a real ledger

Every rupee figure, payee token, account number, slug, UTR and VPA that appears
in a **tracked file** — `SPEC.md`, `CLAUDE.md`, `README.md`, anything under
`docs/`, code comments, tests, example config — comes from
`tests/fixtures/statement.xls` and its companions, or is a synthetic constant
this repo owns.

The fixture's vocabulary, so there is one obvious answer to "what number do I
write here":

| | |
|---|---|
| account number | `999900001111`, masked `****1111` |
| second account | `888800001111` — masked `****1111` too, on purpose (§21.1) |
| slug | `canara-1111` |
| `external_id` | `canara-1111-20260509000001` |
| opening / closing balance | `10,000.00` / `5,068.09` |
| gross withdrawals / deposits | `107,686.36` / `102,754.45` |
| rows | 93, over 47 payee tokens, 85 of them carrying a clock |
| payee tokens | `ZEPKV JYX`, `NYXN XWUBQ`, `JYXQI`, `XENN - UB`, `JY. MURQO` |
| IFSC / branch code | `CNRB0009999` / `999` |
| customer id | `888800011` |
| PDF password | `1111` (the last four — §6.8.1) |

**Why.** A real figure in a tracked file is a permanent, searchable disclosure
of one person's finances, and it survives every future edit that copies the
paragraph around it. It also rots: the ledger moves and the document does not,
so a reader cannot tell a stale figure from a wrong one. And a fixture value can
be *checked* — the test suite parses the same file.

**Where real figures do belong:** in a session report to the operator, in a
terminal, in `payees.md` (gitignored), in `docs/shots/` (gitignored). Never in
something git tracks.

**Enforced, not remembered.** `make audit-docs` — which is also
`pytest tests/test_docs.py`, so it runs in CI and in `make test` — greps every
tracked file for anything shaped like a real ledger figure and fails on a hit,
allowlisting the fixture's own numbers. Run it before any commit that touches
documentation. A one-time scrub that nothing enforces is undone by the next
phase.

If a lesson genuinely needs a magnitude to land, state the **ratio** — "three
times the true figure" — which is the part carrying the lesson anyway.

## Verify, don't recall

**The general rule: anything whose behaviour is defined outside this repo gets
measured, not remembered.** Every one of these was believed and then found to be
false by checking:

| Believed | Measured |
|---|---|
| The PDF password is the Customer ID | It is the last four digits of the account number. All 94 numeric candidates tested (§6.8.1). |
| `getComputedStyle` gives an rgb string | For `color-mix(in oklab, …)` Chromium returns `oklab(L a b)`. The shipped colour is what a canvas paints, so it is sampled from a pixel (§18.5). |
| qpdf can write a deterministic `/ID` | Not for an encrypted file — it refuses, and mints a fresh second ID element every write. `pdfwrite` pins both (§6.8.5). |
| `git bundle verify` proves a bundle | It reads the header only. Sixteen corrupted bytes in the packfile still verified "complete"; a real clone caught it (§16.14). |
| The bank's line wrapping needs a heuristic | It is a recoverable algorithm: 193 breaks reproduced exactly (§6.8.5). |
| `grep -o '"[A-Fa-f0-9]\{64\}"'` counts backup codes | It also counts remembered-device digests, which are the same shape — 8 codes reported as 10, in the direction that suppresses the warning (§18.8). |

The pattern in all six: the belief was plausible, the failure was silent, and
one measurement settled it. When a fact comes from a library, a renderer, a
browser, a CLI or the bank, **run it and read the answer**. If it cannot be
measured, say so instead of writing a plausible sentence.

### Firefly in particular

Firefly III's REST API field names, env vars, and UI paths have all shifted
across versions. Before writing or modifying anything that talks to Firefly —
`src/passbook/firefly/`, `docker-compose.yml`, `.env.example`, or any documented
click-path:

1. **Read the validating code on the pinned tag.** Either inside the running
   container (`docker compose exec -T app …`) or from the tag on GitHub. The
   code that actually rejects the request is the authority — not documentation,
   not memory, and not this file.
2. Confirm the exact shape for the endpoint or setting in use.
3. If it cannot be verified, **say so explicitly** rather than writing a
   plausible guess.

**The instance serves no OpenAPI spec.** Checked on v6.6.6:
`/api/v1/openapi.json`, `/openapi.json`, `/api/openapi.json`, `/docs`,
`/api/docs`, `/api/v1/documentation`, `/v1/documentation.json`, `/api-docs` all
404. Do not go looking again; go to the source instead.

Where the authority actually lives:

| Question | File on the pinned tag |
|---|---|
| Transaction request field names | `app/Api/V1/Requests/Models/Transaction/StoreRequest.php` |
| Account fields (`opening_balance`, …) | `app/Api/V1/Requests/Models/Account/UpdateRequest.php` |
| What an error response looks like | the controller that catches it, e.g. `…/Transaction/StoreController.php` |
| Valid `LOG_CHANNEL` / config values | `config/logging.php` and friends |
| UI labels and click-paths | `resources/lang/en_US/firefly.php`, cross-checked against `routes/web.php` |
| Env var names | that tag's own `.env.example` |

### Treat locations in this file and in SPEC.md as unreliable

They are written from memory and have been wrong three times so far:

- *"Options → Profile → OAuth"* for the token — a pre-v6 layout. Following it
  yields the **Command line token**, a different credential that cannot
  authenticate the API.
- *`LOG_CHANNEL=docker_out`* — valid in v5.x, removed in v6. Would have thrown
  `Log [docker_out] is not defined` at boot.
- *"Fetch the running instance's OpenAPI spec"* — the instance serves none.

So: when this file or `SPEC.md` names a path, a menu item, a config value, or an
endpoint, **verify it against the pinned tag before following it**, and correct
the file in the same change. A confidently-worded wrong location costs more than
no location at all.

## Tests cannot see

**Any change to rendered UI must be screenshotted and looked at before it is
reported done.** Not asserted about — looked at.

Phase 10 shipped with 226 passing tests, a green DR drill, and live API calls
confirming every number. It also shipped five visible design failures, three of
them obvious within ten seconds of opening the page: every category value
truncated to five characters on the one page whose job is showing categories,
ledger banding invisible in dark mode, and hero figures set in a monospace face
so a grouped rupee amount rendered with gaps around its punctuation. Tests, API
calls and drills confirmed all of it was *working*. None of them could see.

`uv run --with playwright --with pyotp python scripts/shoot.py <tag>` renders
every page in both themes at desktop and mobile widths into `docs/shots/<tag>/`
(gitignored — a shot of a real ledger contains real payees and balances). Read
the PNGs. Crop in and read them again if the page is long.

**Screenshots published in this repo are generated from the fixture**, against a
throwaway Firefly loaded by `scripts/demo_ledger.py`. Never from a real ledger.
`docs/screenshots/` is the only tracked image directory, and everything in it
comes from that path.

**A typecheck is not a build.** `shoot.py` serves whatever is in
`src/passbook/web/dist/`, so `npx tsc --noEmit` between two edits proves nothing
about the pictures — Phase 13's first run screenshotted a pre-refactor page and
looked entirely plausible. Run `npm run build` (or `make web-build`) before
shooting, every time.

### And a static screenshot cannot see motion either

A page capture shows the **settled** state. It cannot show a transition, a
skeleton, a progress bar, a toast, or whether `prefers-reduced-motion` is
honoured — and `shoot.py` actively waits for loading to finish, so it excludes
every one of those by construction.

So the rule has a second clause. **For motion and feedback: trigger the state,
observe it, and describe what you observed.** Not "a toast is wired up" —
*what the toast said, how long it stayed, and what the button said before it.*

Phase 11 shipped page transitions, a staggered Day Rail entrance, skeletons,
progress bars and toasts. All six were written, typechecked and screenshotted,
and **not one of them was ever seen running** before it was reported. Building a
thing and observing a thing are different acts, and only the second one is
evidence.

`uv run --with playwright --with pyotp python scripts/motion.py` drives each
state — slow API, mid-flight upload, a rejected file, reduced-motion — captures
the transient frames, and prints the computed animation values.

## How to work

- **Phase by phase.** SPEC §12 defines done-ness. Complete one, report, stop.
- **Ask before adding a dependency** not implied by the spec.
- **Small, boring modules.** Touched once a month. Readability beats cleverness.
- **Tests use fixtures, never the network.** `test_stack.py` is the one narrow,
  documented exception, and it auto-skips when the stack is down.
- **Nothing in a test may name an absolute path.** Anchor on
  `conftest.REPO_ROOT`. Four tests once hardcoded one particular checkout, which
  made them pass on exactly one machine in the world.
- **When reality contradicts the spec** — and it will; §6.3 and §6.5 come from a
  single three-month sample — update `SPEC.md` in the same change and flag what
  moved.
- **Anything that changes the shape of stored data ships a migration.** Add a
  file under `migrations/`, bump `SCHEMA_VERSION`, and make sure `make upgrade`
  applies it. Users pull; a migration nobody runs is a broken ledger. See
  `docs/upgrading.md`.

## Human-only steps

Prompt for these; do not attempt to work around them.

- Downloading statements from the bank's net banking.
- Creating the Firefly account and its Personal Access Token.
- Setting the default currency in Firefly Preferences.
- Filling `.env` — or letting `make setup` walk through it.
- Filling real values into `config/bills.yaml` and `config/rules.yaml`.

## Commands

```
make setup      # first-run wizard: secrets, stack, Firefly token
make up | down | logs
make check      # verify prerequisites and .env
make test
make audit-docs # tracked docs must cite fixture values only
make sync       # process inbox/
make upgrade    # apply pending migrations, taking a backup first
make backup     # pg_dump + config tarball to backups/
```

## Expected failure modes

| Symptom | Cause |
|---|---|
| `openpyxl` raises `InvalidFileException` | Correct behaviour on `.xls`. Use `xlrd`. |
| Amounts parse as `0` instead of `None` | The empty cell is `' '`, not `''`. Strip first. |
| UPI payee comes out as `'09'` or `'2026 01:51:33'` | Trailing timestamp not stripped before `split('/')`. |
| Balance invariant fails | A row was dropped, duplicated, or misparsed. Fix the parser — never the check. |
| Every transaction rejected as duplicate | Expected on overlapping downloads. Verify the count; do not "fix" it. |
| The ledger looks fine but a figure is short | Run `passbook verify-ledger`. A self-consistent balance proves nothing: §6.6 validates a *file*, and only §20 compares Firefly against `archive/`. |
| `verify-ledger` reports an unfinished purge | A purge died mid-cycle. `passbook purge --resume` finishes it; the intent file is only cleared once §20 passes. |
| A second account's rows vanish from a total | Something keyed on `txn_id` instead of `external_id`. Non-negotiable 10, SPEC §21.1. |
| An upload is refused with `unknown_account` | Correct: the account is not in `config/accounts.yaml`. `passbook accounts add <statement>`. It must never silently import (§21.7). |
| A test drops a `purge-intent-*.json` into `backups/` | `ops.BACKUPS` is CWD-relative and `purge()` records intent. Tests that call it must `monkeypatch.chdir(tmp_path)`. |
| A chart or card reads roughly the gross withdrawal or deposit total | Something bypassed `service.ledger_analysis`. Those are Firefly's by-type totals; non-negotiable 9. |
| A regenerated fixture shows a diff with no content change | Only the PDF can do this, and it should not — `pdfwrite` pins both `/ID` elements. If it recurs, qpdf's ID handling changed. |
| `failed to bind host port 127.0.0.1:8080` and `ss -ltnp` shows nothing listening | On WSL with `networkingMode=mirrored` a **Windows** process holds the port and the distro cannot see it. `netstat.exe -ano \| grep :8080` names it. Move the host port with `FIREFLY_HOST_PORT` and match `APP_URL`/`FIREFLY_URL`; never kill a Windows service. |
| A container is `Up` but cannot resolve `db` | Its network attach failed — check `docker inspect -f '{{.NetworkSettings.Networks}}'`. An empty map means it is running detached and every hostname will fail. `docker compose down && up`, not a restart. |
| Postgres container will not start | On WSL2, the repo is on `/mnt/c/`. Move it under `~/`. |
| Firefly 422 on push | Field-name drift. Re-read the validating code on the pinned tag. |
| Everything lands in "(no category)" | `bootstrap` not run, or `apply_rules` not set on the POST. |
| A rule matching a merchant name never fires | Payee is truncated to ~10 chars. Match prefixes. |
| `make audit-docs` fails on a number you just wrote | You cited a live ledger. Use the fixture's value, or state a ratio. |
