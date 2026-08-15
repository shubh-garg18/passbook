# Adding a bank

passbook ships one dialect: Canara Bank. Everything about *that bank* lives in
one file, `src/passbook/banks/canara.py`, and everything around it is
bank-agnostic machinery. Adding HDFC, SBI, ICICI or your own is a new file in
`src/passbook/banks/` plus a fixture — not a fork.

This page is the whole job. It takes an afternoon if your bank's export is a
table, and longer if it is a PDF.

> **You do not need a Firefly instance, a Docker stack, or the author's data to
> do any of this.** Everything below is `pytest`, a redacted fixture, and one
> new module.

---

## 1. What passbook needs from a statement

A statement is a **grid** — rows of strings — carrying:

| | |
|---|---|
| a preamble | account number, customer id, name, IFSC, and the statement period |
| a header row | which column is the date, the amount, the balance, the narration |
| an opening-balance sentinel | the balance the first transaction starts from |
| transaction rows | one per transaction |
| a closing-balance sentinel | what the balance must land on |

If your bank's export has all of that in any container — `.xls`, `.xlsx`, CSV,
an HTML table pretending to be a spreadsheet, a PDF — you are in scope.

Every loader turns its container into `list[list[str]]` and hands it to
`loaders/_table.from_rows()`. That function is the same for every bank and
**must not be overridden**: it is what guarantees a `Transaction` means the same
thing whoever produced it. Your bank supplies *vocabulary*, not behaviour.

---

## 2. The `Bank` value

`src/passbook/banks/__init__.py` defines it. A bank is data plus a handful of
pure functions:

```python
CANARA = register(
    Bank(
        slug="canara",                    # [a-z0-9-]+, becomes part of every external_id
        name="Canara Bank",
        column_aliases=COLUMNS,           # normalised header text -> canonical field
        required_columns=REQUIRED,        # without these it is not a statement
        metadata_labels=METADATA,         # normalised label text -> StatementMeta field
        metadata_label_columns=(0, 3),    # which columns carry labels in the preamble
        opening_label="openingbalance",
        closing_label="closingbalance",
        period_pattern=PERIOD,            # two capture groups, each parseable below
        parse_date=parse_date,            # text -> date, locale-independent
        narration_matchers=MATCHERS,      # tried in order, first hit wins
        strip_trailing_timestamp=...,     # before tokenising, if yours has one
        extract_time=...,                 # time of day, or None — never midnight
        pdf_password_hint="...",          # one sentence, printed when a PDF is refused
        detect=detect,                    # does this grid belong to us?
    )
)
```

`register()` at import time is all the wiring there is. `banks/_load()` imports
every module in the package on first use, so a new file is discovered without a
list to update anywhere.

**Why a value and not a subclass.** A base class invites `class HDFC(Bank)` with
an overridden `from_rows`, and then there are two copies of the
balance-continuity invariant. Data cannot be overridden into a second ledger.

---

## 3. Step by step

### 3.1 Get a real export and look at it

```bash
uv run python - <<'PY'
import xlrd                       # or csv, or pdfplumber
book = xlrd.open_workbook("inbox/your-statement.xls")
sheet = book.sheet_by_index(0)
print("sheet name:", sheet.name)
for r in range(min(20, sheet.nrows)):
    print(r, [sheet.cell_value(r, c) for c in range(sheet.ncols)])
PY
```

Write down what you actually see, not what you expect. Canara's export taught
this project three things a reasonable person would have got wrong:

- the header says **`Trasnaction ID`** — the bank's own typo;
- the narration column is **`Remarks`**, not Particulars or Description;
- an **empty amount cell is `' '`, one space, not `''`** — so a test for
  emptiness that skips `.strip()` silently parses it as `Decimal('0')`.

Your bank will have its own three. Find them before writing code.

### 3.2 Check the container

```bash
head -c 8 inbox/your-statement.xls | xxd
```

| First bytes | What it is | Loader |
|---|---|---|
| `D0 CF 11 E0 A1 B1 1A E1` | genuine OLE2 `.xls` | `loaders/xls.py`, already written |
| `50 4B 03 04` | ZIP, so `.xlsx` | needs openpyxl — see §3.6 |
| `<` | an HTML table named `.xls` | `loaders/html_table.py`, already written |
| `%PDF` | PDF | `loaders/pdf.py` — the hard case |
| anything else | delimited text | `loaders/delimited.py`, already written |

`loaders/sniff()` decides this from the first eight bytes and **never from the
filename**. Four of the five containers already have a loader, so most banks
need no loader work at all — only a `Bank`.

### 3.3 Write the module

Copy `src/passbook/banks/canara.py` to `src/passbook/banks/yourbank.py` and
replace the vocabulary. Three parts need care:

**`parse_date` must be locale-independent.** Never `datetime.strptime(text,
"%d-%b-%Y")`: `%b` reads the active locale, so it works on your machine and
fails in CI or on a French laptop. Canara maps month names in a dict; copy that.

**`detect(rows)` reads content, never the filename.** Banks name every export
for a date range, and a statement moved by hand into the wrong folder must not
change how it is read. Look for something only your bank writes:

```python
def detect(rows: Rows) -> bool:
    from ..loaders._table import norm

    for row in rows[:50]:
        for cell in row:
            if norm(cell).startswith("yourbanknameltd"):
                return True
    return False
```

Exactly one registered bank must claim a given grid. `banks.detect()` raises
rather than picking when two do — guessing there would attach a statement to the
wrong dialect and land plausible, wrong rows in the ledger.

**`narration_matchers` is where the real work is.** Each matcher takes the raw
narration string and returns the fields it recognised, or `None`:

```python
def _upi(raw: str) -> dict | None:
    if not raw.startswith("UPI-"):
        return None
    parts = raw.split("-")
    return {"channel": UPI, "payee": parts[1], "utr": parts[2],
            "counterparty_bank": ..., "is_reversal": False}
```

Rules to keep:

- **A matcher never raises.** An unrecognised narration falls through to
  `OTHER` with the raw string preserved. One odd row must not fail a sync.
- **Strip a trailing timestamp before splitting on the separator.** Canara ends
  UPI narrations with `DD/MM/YYYY HH:MM:SS`, whose slashes add four spurious
  tokens to a naive `split('/')` and push the payee off by one. This is the
  single most common way to get a parser subtly wrong.
- **Keep the time.** Strip the timestamp for tokenising, but return it as
  `txn_time`. There is no time column on any of these statements, so the
  narration is the only place it exists, and the Day Rail is built on it. Return
  `None` where there is no clock — never midnight, which invents a nocturnal
  transaction that did not happen.
- **Direction comes from the amount columns, not the narration.** Even when the
  narration says `CR` or `DR`, the withdrawal/deposit columns are authoritative.
  A mismatch is a warning, not an error.

### 3.4 Register the account's bank

`config/accounts.yaml` carries `bank:` per account, validated against the
registry:

```yaml
accounts:
  - slug: yourbank-1111
    bank: yourbank            # must match Bank.slug
    account_number: "…"
    asset_account: Your Bank savings account
```

An unregistered bank is refused with the list of registered ones. Nothing is
guessed: an account attached to a bank with no dialect would parse its
statements with the wrong vocabulary.

### 3.5 Build a redacted fixture — required, not optional

**Do not commit a real statement.** Not your own, and certainly not anyone
else's. `scripts/redact.py` exists for this and is a required deliverable of any
bank PR.

What it does, and what yours must do:

- rewrites the sheet name, the whole metadata block, every narration, every
  counterparty phone and account number, the embedded customer id and the card
  last-4;
- keeps **string lengths** — a ~10-character payee truncation is a real property
  of the data and a fixture that loses it stops testing the thing that matters;
- regenerates every amount and then **recomputes the running balance**, so the
  continuity invariant still holds and the test exercising it stays meaningful
  rather than tautological;
- audits the result against the source and fails on any surviving real string.

```bash
uv run python scripts/redact.py inbox/your-statement.xls \
    tests/fixtures/yourbank.xls --audit
```

Then read the fixture yourself before committing it. The audit is a scan; you
are the check that the scan looked for the right things.

> If your export is an encrypted PDF, note that a byte scan of the file reports
> *clean* whatever is inside it — RC4 plus Flate means any string at all "does
> not appear". The audit has to decrypt and decompress first. That is the one
> thing that makes it an audit rather than theatre.

### 3.6 If your bank ships `.xlsx`

There is no loader, on purpose: `xlrd` 2.x reads only `.xls` and `openpyxl`
reads only `.xlsx`, so supporting both means a second dependency. The sniffer
already recognises the ZIP magic and raises a message saying exactly this. Open
an issue before writing it — the dependency is worth adding once a real
statement needs it, and not before.

---

## 4. The invariant your loader must satisfy

This is the one that matters, and it is not negotiable:

```
abs(balance[i] - (balance[i-1] - debit[i] + credit[i])) < Decimal("0.01")
```

Seeded from the opening-balance sentinel, and the last row must land exactly on
the closing-balance sentinel. `validate.py` runs it on every parse, before
anything is pushed.

**If it fails, your parser is wrong, not the bank's data.** It has held cleanly
on every real statement this project has seen — 93 rows, 0 breaks — and on both
committed fixtures. A row was dropped, duplicated, or misparsed. Common causes:

- a continuation row treated as a transaction (multi-line narrations);
- an empty amount cell parsed as `0` instead of `None` (the `' '` trap);
- a sentinel row parsed as a transaction;
- amounts read as floats.

**Never soften the check to make a test pass.** It is the only thing standing
between a parsing regression and silently wrong financial data. A PR that
loosens it will not be merged.

Four more assertions, all cheap and all worth having:

- exactly one of debit/credit is populated per row;
- the transaction id is unique within a file;
- dates are monotonically non-decreasing;
- money is `Decimal` everywhere, never `float`. Anywhere. No exceptions.

---

## 5. Transaction ids and `external_id`

passbook uses the bank's own transaction id as the ledger key. If your bank
gives one, use it — and check the property Canara has: the id must be **stable
across exports**, so that re-downloading an overlapping range produces the same
ids and the push deduplicates instead of doubling the ledger. Test it by
exporting two overlapping ranges and comparing.

If your bank gives **no** id column, derive one that is reproducible from the
row's own content — Canara's is `YYYYMMDD` plus the 1..n ordinal within that
date, which the PDF loader reconstructs byte-for-byte because the PDF export
carries no id column at all. A hash is the last resort:

```
sha256(f"{txn_date}|{debit or 0}|{credit or 0}|{balance}|{narration.strip()}")[:40]
```

The balance component is what makes it unique even for two identical same-day
transactions.

Whatever you produce, it is namespaced before it reaches Firefly:
`external_id = "<slug>-<txn_id>"`. **Never key anything on the bare id.** Banks
sequence ids per account, so two accounts at the same bank emit identical ones —
measured on the two committed fixtures, 93 of 93 identical, and a dict keyed on
the bare id merged two accounts and kept 93 of 186 rows with no error at all.

---

## 6. Tests your PR needs

Model them on the Canara ones; they are the checklist.

| Test | Asserts |
|---|---|
| `test_loaders.py` | the fixture parses, the row count is right, and an empty amount cell is `None` rather than `Decimal('0')` |
| `test_narration.py` | **every grammar** your bank speaks, plus an unparseable string falling through to `OTHER` |
| `test_validate.py` | a fixture with one row deleted **fails** the continuity check |
| `test_golden.py` | fixture in, expected normalised JSON out |
| a `detect` test | your bank claims your fixture, and does **not** claim Canara's |

That last one is easy to forget and is what stops two dialects fighting over a
file.

```bash
make test          # everything
make audit-docs    # tracked docs cite fixture values only — see below
```

**No test may touch the network**, and none may name an absolute path. Anchor on
`conftest.REPO_ROOT`.

---

## 7. Two rules about what you write down

**Tracked documentation cites fixture values, never a live ledger.** Every rupee
figure, payee token, account number and UTR in a committed file comes from a
fixture or is a synthetic constant. `make audit-docs` enforces the machine-
checkable half and fails the build on a hit; the half it cannot check — a payee
name, a category, a person — is yours to hold. If a lesson needs a magnitude,
state a ratio.

**Write down what you measured, and say when you did not.** This project's
documentation is unusually specific because every number in it came from running
something. If you could not verify a claim, say so instead of writing a
plausible sentence — that is worth more here than a confident guess, and there
is a table in `CLAUDE.md` of six times a plausible guess was wrong.

---

## 8. Open the PR

Include:

- [ ] `src/passbook/banks/yourbank.py`
- [ ] `tests/fixtures/yourbank.*`, redacted, with the audit output quoted in the PR
- [ ] tests from §6
- [ ] a note on **which country and which account type** the export came from —
      banks vary their format by product, and the next person needs to know what
      yours was
- [ ] anything the bank does that surprised you, in the module docstring

You do not need to support the PDF fallback, multiple accounts, or the charts.
A `Bank` and a fixture is a complete contribution.

If you get stuck on the narration grammars, open a draft PR with the fixture and
the loader and say so — that is the part that benefits most from a second pair
of eyes, and it is the part where a wrong guess is silent.
