"""Shared row-grid -> (StatementMeta, list[Transaction]) core. SPEC §6.3.

Every loader normalises its container (OLE2 sheet, HTML table, delimited text)
into a plain list-of-rows-of-strings and hands it here. The Canara layout is
identical across containers; only the envelope differs. That is exactly the
case SPEC D4's sniffer exists to catch — the bank changing its export backend
without changing the statement itself.

Nothing in this module converts a cell to anything but `str` first. Every cell
in the real export is text, including amounts, and inferring dtypes is how you
end up with floats in a ledger.
"""

import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..models import StatementMeta, Transaction

log = logging.getLogger(__name__)

Rows = list[list[str]]


class ParseError(ValueError):
    """The grid does not look like a Canara statement."""


# Locale-independent on purpose. SPEC §6.3: `%b` depends on the active locale,
# which differs between WSL and CI, and this file's months are uppercase.
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_DATE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
_PERIOD = re.compile(
    r"from\s+(\d{1,2}-[A-Za-z]{3}-\d{4})\s+to\s+(\d{1,2}-[A-Za-z]{3}-\d{4})",
    re.IGNORECASE,
)

# The bank misspells "Transaction" in its own export. Match it as-is; accept the
# correct spelling too, in case they ever fix it. SPEC §6.3.
COL_ALIASES = {
    "date": "date",
    "trasnactionid": "txn_id",
    "transactionid": "txn_id",
    "withdrawals": "debit",
    "withdrawal": "debit",
    "deposits": "credit",
    "deposit": "credit",
    "balance": "balance",
    "remarks": "narration",
}
REQUIRED_COLS = {"date", "txn_id", "debit", "credit", "balance", "narration"}

# The five that no bank can omit. `txn_id` is the sixth and is the only one a
# profile may drop, by declaring `derive_txn_id: true` — see `_derived_id`.
CORE_COLS = REQUIRED_COLS - {"txn_id"}

META_LABELS = {
    "accountnumber": "account_number",
    # The forms a PDF preamble actually uses. Added in §44, when the generic
    # PDF reader started needing to find an account number on paper rather than
    # in a spreadsheet's label column — `A/c`, `Account No.` and `A/c No` are
    # the same field with three different amounts of punctuation, and `norm`
    # has already thrown the punctuation away.
    "accountno": "account_number",
    "acno": "account_number",
    "ac": "account_number",
    "acc": "account_number",
    "accountnum": "account_number",
    "customerid": "customer_id",
    "name": "account_name",
    "branchcode": "branch_code",
    "ifsccode": "ifsc",
    "ifsc": "ifsc",
}


def norm(text: str) -> str:
    """Strip non-alphanumerics and lowercase, for tolerant header matching."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def parse_date(text: str) -> date:
    """`09-MAY-2026`, plus any format a bank profile declares. SPEC §27.

    The built-in form is tried first and is locale-independent by construction
    (its own month table, not `%b`, which changes between WSL and CI). Profile
    formats go through `strptime` and therefore CAN be locale-sensitive — which
    is why a profile should prefer numeric months where it has the choice, and
    why `passbook inspect` prints the raw cell for the operator to match.
    """
    stripped = text.strip()
    m = _DATE.match(stripped)
    if m:
        day, mon, year = m.groups()
        if mon.upper() not in MONTHS:
            raise ParseError(f"unknown month {mon!r} in {text!r}")
        return date(int(year), MONTHS[mon.upper()], int(day))

    declared = _profile_date_formats()
    for fmt in declared:
        try:
            return datetime.strptime(stripped, fmt).date()
        except ValueError:
            continue

    # Say what would have worked. `unparseable date '06-04-2024'` is true and
    # useless — the date is perfectly readable, it is the *profile* that does
    # not say how to read it, and the remedy is one line in a YAML file the
    # operator was never meant to open. This happened to a profile saved before
    # the format was inferred at all. SPEC §54.3.
    would = [f for f in DATE_CANDIDATES if _parses(stripped, f)]
    if would and not declared:
        raise ParseError(
            f"{text.strip()!r} is a date, but this bank's profile does not say in "
            f"which format. Re-save the bank from Accounts \u2192 Add a bank — "
            f"pressing Try it works the format out from your own dates "
            f"({would[0]} here) and Save writes it in."
        )
    if would:
        raise ParseError(
            f"{text.strip()!r} does not match the format this bank's profile "
            f"declares ({', '.join(declared)}). It would read as {would[0]}."
        )
    raise ParseError(f"unparseable date {text!r}")


def _parses(text: str, fmt: str) -> bool:
    try:
        datetime.strptime(text, fmt)
    except ValueError:
        return False
    return True


def parse_amount(text: str) -> Decimal | None:
    """`'10,000.00'` -> Decimal. `' '` -> None.

    The empty amount cell is a single space, not an empty string, in all 93
    transaction rows of the reference statement. Stripping before testing
    emptiness is what makes this return None rather than a silent
    Decimal('0'). SPEC §6.3 calls this the likeliest source of a silent bug.
    """
    cleaned = text.strip().replace(",", "")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ParseError(f"unparseable amount {text!r}") from exc


def _at(rows: Rows, r: int, c: int) -> str:
    if r < 0 or r >= len(rows) or c < 0 or c >= len(rows[r]):
        return ""
    return rows[r][c]


#: Column aliases to use *instead of* the registered profiles', for one call.
#: Set only by the Add-a-bank "try it" path (§49), which is testing a profile
#: that deliberately has not been saved. `None` means "ask the profiles".
COL_ALIASES_OVERRIDE: dict[str, str] | None = None


def _all_aliases() -> dict[str, str]:
    """Built-in Canara columns, plus whatever `config/banks/*.yaml` adds. §27.

    Profiles win on a clash, so a bank whose `Balance` column means something
    different can say so without the built-ins having to be edited.
    """
    from .profiles import ProfileError, column_aliases

    merged = dict(COL_ALIASES)
    if COL_ALIASES_OVERRIDE is not None:
        # The profile under test, not the ones on disk. Without this, `from_rows`
        # banded the grid with the operator's proposed columns and then failed to
        # recognise its own header — `header at row 10 is missing ['narration']`
        # — because `Particulars` is only an alias once the profile is SAVED.
        # Try-it therefore disagreed with the very thing it exists to predict.
        merged.update(COL_ALIASES_OVERRIDE)
        return merged
    try:
        merged.update(column_aliases())
    except ProfileError as exc:
        # Loudly, and as a parse failure: a broken profile silently falling back
        # to the Canara layout is how someone's SBI statement gets read with the
        # wrong columns and still balances by luck.
        raise ParseError(str(exc)) from exc
    return merged


def _all_meta_labels() -> dict[str, str]:
    from .profiles import ProfileError, metadata_labels

    merged = dict(META_LABELS)
    try:
        merged.update(metadata_labels())
    except ProfileError as exc:
        raise ParseError(str(exc)) from exc
    return merged


#: Date formats to use *instead of* the registered profiles', for the duration
#: of one call. Set only by the Add-a-bank "try it" path (§49), which is testing
#: a profile that deliberately has not been saved. `None` means "ask the
#: profiles", which is what every ordinary import does.
DATE_FORMATS_OVERRIDE: list[str] | None = None

#: The formats offered when a date will not parse. Ordered by how an Indian
#: statement is most likely to print one, and every entry is numeric-month:
#: `%b` reads the C locale's month names, which differ between WSL and CI, so a
#: profile should prefer numbers wherever the bank gives the choice (§27).
#: How many samples a format has to read before it counts as this bank's. Below
#: this, the column is not a date column and no format should be offered.
MIN_DATE_SAMPLES = 3

DATE_CANDIDATES = (
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d-%m-%y",
    "%d/%m/%y",
    "%Y-%m-%d",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d-%B-%Y",
    "%m/%d/%Y",
)


def _profile_date_formats() -> list[str]:
    if DATE_FORMATS_OVERRIDE is not None:
        return list(DATE_FORMATS_OVERRIDE)

    from .profiles import ProfileError, date_formats

    try:
        return date_formats()
    except ProfileError:
        return []


def date_formats_that_parse(samples) -> list[str]:
    """The formats that read the **most** of these samples. SPEC §49.1.

    The most, not all — and not the first. Both halves matter.

    **Not the first**, because `09-05-2026` is read by more than one candidate
    and picking whichever happened to work on row one is how a statement lands
    silently in the wrong months. So every candidate is scored over every
    sample, and ties come back as ties: two formats reading the file equally
    well means it is genuinely ambiguous — every day in it is 12 or less — and
    the operator is asked rather than guessed at.

    **Not all**, because the samples are a column and a column has a bottom.
    Below the last transaction a statement prints a legend, a disclaimer, a
    generated-on line, and those rows have words in the date cell. Requiring
    every sample to parse meant one line of footer prose returned *nothing at
    all* — measured on a real statement: 32 perfectly readable dates and no
    inferred format, so the profile saved without one and the import then failed
    on the first row. §55.

    Prose parses under no candidate, so it cannot tip the comparison between
    them; it only has to stop being fatal. `MIN_DATE_SAMPLES` keeps a handful of
    stray tokens from deciding a format on their own.
    """
    values = [str(v).strip() for v in samples if str(v).strip()]
    if not values:
        return []

    scored: dict[str, int] = {}
    for fmt in DATE_CANDIDATES:
        hits = sum(1 for value in values if _parses(value, fmt))
        if hits:
            scored[fmt] = hits
    if not scored:
        return []

    best = max(scored.values())
    if best < min(MIN_DATE_SAMPLES, len(values)):
        # Almost nothing parsed. That is not a date column with a footer under
        # it, that is the wrong column — and offering a format for it would send
        # the operator looking in the wrong place.
        return []
    return [fmt for fmt in DATE_CANDIDATES if scored.get(fmt) == best]


#: How far down a file the header may be. SPEC §6.3, raised in §99.
#:
#: Canara's is at row 9 and 50 was a generous bound for a spreadsheet. A PDF is
#: not a spreadsheet: its preamble is one grid row per printed LINE, and SBI's
#: runs to 71 of them before the table starts — so the header was there, in the
#: grid, and never looked at. `no header row found in the first 50 rows` on a
#: file whose columns had just been banded correctly.
#:
#: Raising it costs nothing and protects nothing it used to: the scan stops at
#: the first row matching four column names, so a real header still wins over
#: anything below it, and a preamble row that matches four of them would have
#: won under the old bound too.
MAX_HEADER_SCAN = 200


def _find_header(rows: Rows, derive_txn_id: bool | None = None) -> tuple[int, dict[str, int]]:
    """Scan downward for the header rather than hardcoding row 9. SPEC §6.3.

    `derive_txn_id` says whether this bank prints a per-row reference (§44.4).
    `None` means "ask the registered profiles", which is right for an ordinary
    import; the Add-a-bank page passes it explicitly, because the profile it is
    testing has deliberately not been saved yet (§49).
    """
    aliases = _all_aliases()
    for r in range(min(len(rows), MAX_HEADER_SCAN)):
        mapping: dict[str, int] = {}
        for c in range(len(rows[r])):
            field = aliases.get(norm(_at(rows, r, c)))
            if field and field not in mapping:
                mapping[field] = c
        if len(mapping) >= 4:
            missing = REQUIRED_COLS - mapping.keys()
            # `txn_id` is the one field a profile may say its bank does not
            # print (§44). Everything else is still required, and the message
            # still names what is short.
            derives = _derives_txn_id() if derive_txn_id is None else derive_txn_id
            if missing == {"txn_id"} and derives:
                log.info("header row %d, columns %s (txn_id will be derived)", r, mapping)
                return r, mapping
            if missing:
                raise ParseError(f"header at row {r} is missing {sorted(missing)}")
            log.info("header row %d, columns %s", r, mapping)
            return r, mapping
    raise ParseError(f"no header row found in the first {MAX_HEADER_SCAN} rows")


def _derives_txn_id() -> bool:
    from .profiles import ProfileError, derives_txn_id

    try:
        return derives_txn_id()
    except ProfileError:
        return False


def _derived_id(date_text: str, narration: str, debit: str, credit: str, balance: str) -> str:
    """A stable per-row id for a bank that prints none. SPEC §44.

    **The running balance is what makes it unique.** Two coffees on the same day
    for the same amount to the same payee are indistinguishable by date, amount
    and narration — and they are *not* indistinguishable by the balance after
    each, because the balance moves. So the hash covers the whole row.

    **And it is what makes it fragile**, which is the trade-off and is stated
    rather than hidden: if the bank ever restates a balance, every row from that
    point down hashes differently and re-imports as new. A bank that prints a
    reference number should always have it mapped instead — that is why this is
    opt-in per profile and never inferred.

    Prefixed `d-` so a derived id is visibly derived in Firefly, in a log line
    and in a purge-intent file. Nobody should have to wonder which kind they are
    looking at.
    """
    import hashlib

    material = "\x1f".join(
        part.strip() for part in (date_text, narration, debit, credit, balance)
    )
    return "d-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


#: How many words a metadata label may be. "Customer ID", "A/c No.".
MAX_LABEL_WORDS = 3


def _find_metadata(rows: Rows, header_row: int) -> dict[str, str]:
    """Label/value pairs above the header. SPEC §6.3, widened in §44.

    A spreadsheet puts the label in one cell and the value in the next, which is
    the first strategy and the only one there was. A **PDF does not**: it prints
    `Statement for A/c 999900001111 Between …` as one run of words on one line,
    and no amount of splitting on gaps turns that into two cells.

    So three strategies, in descending order of confidence:

    1. the label IS a cell, and the value is the next cell — a spreadsheet;
    2. the label is the START of a cell, and the value is the rest of it —
       `Account No.: 409302010012345`;
    3. the label appears ANYWHERE in a cell, and the value is the token after
       it — `Statement for A/c 999900001111 Between …`.

    Third is the loose one, and it is bounded on purpose: only above the header,
    only for labels a profile or the built-ins declare, and only where the next
    token is non-empty. The account number it finds is then checked against the
    registry by `assert_account` (§21.7) before a single row is imported, so a
    wrong read is refused rather than filed.
    """
    found: dict[str, str] = {}
    labels = _all_meta_labels()

    def claim(field: str, value: str) -> None:
        value = value.strip().lstrip(":").strip()
        if field and value and field not in found:
            found[field] = value

    for r in range(header_row):
        row = rows[r] if r < len(rows) else []

        # 1. label cell, value cell — at ANY column, not just 0 and 3.
        #
        # Those two were where Canara's spreadsheet puts them, and the
        # restriction quietly made this Canara-only: a PDF read as words comes
        # through one word per cell, so `A/c` sits at column 2 with the number
        # at column 3 and neither position was looked at. Scanning the row is
        # the same work and one fewer assumption. Above the header only, so a
        # narration cannot masquerade as a label.
        for label_col in range(len(row)):
            field = labels.get(norm(_at(rows, r, label_col)))
            if field:
                claim(field, _at(rows, r, label_col + 1))

        # 2 and 3, over every cell in the row.
        for c in range(len(row)):
            words = _at(rows, r, c).split()
            for start in range(len(words)):
                for span in range(min(MAX_LABEL_WORDS, len(words) - start), 0, -1):
                    field = labels.get(norm(" ".join(words[start : start + span])))
                    if not field:
                        continue
                    rest = words[start + span :]
                    if not rest:
                        break
                    # A prefix match hands over everything after it; a match in
                    # the middle of a line hands over one token, because the
                    # words that follow belong to the next label.
                    claim(field, " ".join(rest) if start == 0 else rest[0])
                    break
    return found


def _find_sentinel(rows: Rows, header_row: int, want: str) -> tuple[int, Decimal]:
    """Opening/Closing Balance rows: the label somewhere, the balance right of it.

    The label is matched against each cell **and against the row's cells joined**.
    On a PDF the two words can land in different columns — measured: `Closing`
    banded into a mapped reference column and `Balance :` into the narration, so
    no single cell read `closingbalance`, the closing sentinel was missed, and
    the parser ran on into the footer prose below the table. §54.
    """
    for r in range(header_row + 1, len(rows)):
        if norm("".join(_at(rows, r, c) for c in range(len(rows[r])))) == want:
            for probe in range(len(rows[r]) - 1, -1, -1):
                amount = parse_amount(_at(rows, r, probe))
                if amount is not None:
                    return r, amount
        for c in range(len(rows[r])):
            if norm(_at(rows, r, c)) == want:
                for probe in range(len(rows[r]) - 1, c, -1):
                    amount = parse_amount(_at(rows, r, probe))
                    if amount is not None:
                        return r, amount
                raise ParseError(f"{want} row {r} carries no balance")
    raise ParseError(f"no {want} sentinel row found")


def _period(rows: Rows, header_row: int) -> tuple[date, date] | None:
    """The declared statement period, or None if the file does not state one."""
    for r in range(header_row):
        for c in range(len(rows[r])):
            m = _PERIOD.search(_at(rows, r, c))
            if m:
                return parse_date(m.group(1)), parse_date(m.group(2))
    return None


def _sentinel(rows: Rows, header_row: int, want: str) -> tuple[int, Decimal] | None:
    """`_find_sentinel`, but None instead of raising. See `from_rows`."""
    try:
        return _find_sentinel(rows, header_row, want)
    except ParseError:
        return None


def from_rows(
    rows: Rows, *, derive_txn_id: bool | None = None
) -> tuple[StatementMeta, list[Transaction]]:
    """A parsed statement from a grid of strings.

    **What is required and what is derived.** SPEC §42.

    Canara states four things about itself that most banks do not: a
    `Statement for Account from <date> to <date>` line, an `Opening Balance`
    sentinel row, a `Closing Balance` sentinel row, and a Customer ID. This
    function required all four, and a bank profile could not supply them —
    profiles name *columns*, and these are *structure*. So `config/banks/*.yaml`
    let an operator describe their bank's columns perfectly and the import still
    failed on a preamble their bank does not write.

    Each of the four is now derived when it is absent, and each derivation is
    exact rather than approximate:

    * the **period** is the first and last transaction date — which is what the
      statement covers, whether or not it says so;
    * the **closing balance** is the last row's running balance;
    * the **opening balance** is the first row's balance with its own movement
      undone, which is the definition of an opening balance;
    * the **customer id** is optional, because it is a Canara credential (§11)
      and not part of any ledger.

    Nothing here softens a check. The balance-continuity invariant (§6.6) still
    runs over the same rows and is still the thing that catches a mis-mapped
    column — and a *derived* opening balance cannot make it pass falsely,
    because it is computed from the same first row the chain starts at. What a
    derived closing balance does lose is the independent cross-check Canara's
    own sentinel gives, and that is why the sentinels are still used wherever
    the file has them.

    **The account number is never derived.** It routes every row to a ledger
    (§21.1) and a wrong one is silent forever, so a file that does not carry it
    is refused with the labels a profile could name it under.
    """
    header_row, cols = _find_header(rows, derive_txn_id)
    meta_fields = _find_metadata(rows, header_row)

    if "account_number" not in meta_fields:
        raise ParseError(
            "no account number found above the header. Every row is filed under "
            "it, so it is read from the statement and never typed. Add the label "
            "your bank uses to `metadata:` in config/banks/<bank>.yaml — the "
            "known ones are "
            + ", ".join(sorted({k for k, v in _all_meta_labels().items() if v == "account_number"}))
        )

    stated_period = _period(rows, header_row)
    opening_at = _sentinel(rows, header_row, "openingbalance")
    closing_at = _sentinel(rows, header_row, "closingbalance")

    # Between the sentinels where they exist; otherwise every row under the
    # header. A row is a transaction if it carries a date OR an id — the same
    # test as before, so Canara reads identically either way.
    first = opening_at[0] + 1 if opening_at else header_row + 1
    last = closing_at[0] if closing_at else len(rows)

    transactions: list[Transaction] = []
    skipped = 0
    first_skip: str | None = None
    for r in range(first, last):
        date_text = _at(rows, r, cols["date"]).strip()
        # `cols` has no `txn_id` when the profile derives it (§44).
        id_text = _at(rows, r, cols["txn_id"]).strip() if "txn_id" in cols else ""
        if not date_text and not id_text:
            continue  # blank spacer row
        if not date_text:
            # **A transaction has a date.** Unconditionally — this was guarded
            # by `not opening_at`, on the reasoning that sentinels bracket the
            # table so anything between them is a row. They do not bracket it
            # tightly enough: a wrapped narration whose words happen to start
            # under a mapped reference column gives a row with an id and no
            # date, and `parse_date('')` then took the whole import down.
            #
            # Skipping is safe because the balance chain would catch a genuine
            # row dropped here, and it is the only thing that could be done
            # anyway: a Transaction without a date cannot be constructed.
            continue
        try:
            when = parse_date(date_text)
        except ParseError as exc:
            if first_skip is None:
                first_skip = str(exc)
            # Not a date, so not a transaction — the table has ended and this is
            # the footer prose most statements print underneath it. Skipped
            # rather than fatal, and **never silently**: if a real row were lost
            # here the balance chain would break, which is the whole point of
            # having one. §54.
            skipped += 1
            log.info("row %d: %r is not a date — not a transaction", r, date_text[:12])
            continue

        balance = parse_amount(_at(rows, r, cols["balance"]))
        if balance is None:
            raise ParseError(f"row {r}: transaction has no balance")
        if not id_text:
            id_text = _derived_id(
                date_text,
                _at(rows, r, cols["narration"]),
                _at(rows, r, cols["debit"]),
                _at(rows, r, cols["credit"]),
                _at(rows, r, cols["balance"]),
            )
        transactions.append(
            Transaction(
                txn_id=id_text,
                txn_date=when,
                narration=_at(rows, r, cols["narration"]),
                debit=parse_amount(_at(rows, r, cols["debit"])),
                credit=parse_amount(_at(rows, r, cols["credit"])),
                balance=balance,
                sheet_row=r,
            )
        )

    if skipped:
        log.info("%d row(s) under the header were not transactions", skipped)
    if not transactions:
        if first_skip:
            # Every row was skipped for a nameable reason, and the reason is
            # what the operator needs. Reporting "no transaction rows were
            # found" instead hides it — measured on a profile with no date
            # format, where all 32 rows were skipped and the message said
            # nothing about dates at all. §54.3.
            raise ParseError(
                f"{skipped} row(s) under the header could not be read as "
                f"transactions. The first said: {first_skip}"
            )
        raise ParseError("the header parsed but no transaction rows were found under it")

    if opening_at:
        opening = opening_at[1]
    else:
        # Undo the first row's own movement. Exact, not an estimate: the running
        # balance already includes it.
        head = transactions[0]
        opening = head.balance - (head.credit or Decimal(0)) + (head.debit or Decimal(0))
        log.info("no Opening Balance row; derived %s from the first transaction", opening)
    closing = closing_at[1] if closing_at else transactions[-1].balance
    if not closing_at:
        log.info("no Closing Balance row; using the last transaction's balance")

    if stated_period is not None:
        period_from, period_to = stated_period
    else:
        dates = [t.txn_date for t in transactions]
        period_from, period_to = min(dates), max(dates)
        log.info("no period line; derived %s to %s from the rows", period_from, period_to)

    meta = StatementMeta(
        account_number=meta_fields["account_number"],
        # Optional. It is Canara's PDF password and a credential (§11), not
        # part of any ledger — refusing a whole statement for its absence was
        # requiring one bank's preamble of every bank.
        customer_id=meta_fields.get("customer_id", ""),
        account_name=meta_fields.get("account_name", ""),
        branch_code=meta_fields.get("branch_code", ""),
        ifsc=meta_fields.get("ifsc", ""),
        period_from=period_from,
        period_to=period_to,
        opening_balance=opening,
        closing_balance=closing,
    )

    log.info("parsed %d transactions (rows %d-%d)", len(transactions), first, last - 1)
    return meta, transactions
