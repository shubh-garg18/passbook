"""Everything the CLI and the web UI both do, defined once. SPEC §14.

The CLI had this logic inline, tangled with `typer.Exit`. The web UI needs the
same steps but must turn a failure into an HTTP response, not a process exit —
so the work moved here and raises plain exceptions. Both front ends are thin
wrappers over these functions. There is no second parser and no second push
path; if you find yourself writing one, that is the bug.
"""

import calendar
import logging
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from . import narration as narration_mod
from .config import (
    SYNC_STALE_DAYS,
    SYNC_URGENT_DAYS,
    Account,
    Settings,
    default_slug,
    last_sync,
    load_accounts,
    load_payee_aliases,
    save_accounts,
)
from .firefly.bootstrap import load_rules
from .firefly.client import FireflyClient
from .firefly.push import PushResult, push_transactions
from .loaders import load as load_statement
from .models import StatementMeta, Transaction
from .validate import UnknownAccount, assert_account, check

log = logging.getLogger(__name__)

# Labels narration.py assigns itself. They are not bank tokens and must never
# be offered as "new payees needing a decision". SPEC D10.
SYNTHETIC_PAYEES = {"Bank Charges", "Savings Interest", "PMSBY"}


@dataclass
class ParsedStatement:
    path: Path
    meta: StatementMeta
    transactions: list[Transaction]
    warnings: list[str] = field(default_factory=list)

    @property
    def debits(self) -> Decimal:
        return sum((t.debit for t in self.transactions if t.debit), Decimal(0))

    @property
    def credits(self) -> Decimal:
        return sum((t.credit for t in self.transactions if t.credit), Decimal(0))


def parse_statement(path: Path, aliases: dict[str, str] | None = None) -> ParsedStatement:
    """Load, enrich and validate. Raises rather than exiting.

    Propagates ParseError, BalanceBreak and IntegrityError untouched — the
    balance invariant is never softened for a caller's convenience, web
    included. CLAUDE.md non-negotiable #3.
    """
    meta, transactions = load_statement(path)
    narration_mod.enrich(transactions, aliases if aliases is not None else load_payee_aliases())
    warnings = check(meta, transactions)
    return ParsedStatement(path=path, meta=meta, transactions=transactions, warnings=warnings)


def account_matches(meta: StatementMeta, settings: Settings) -> None:
    """SPEC §6.7, single-account form. Raises AccountMismatch.

    Kept for the pre-registry path (the DR drill passes two env vars into a
    recovered container and nothing else). `resolve_account` is what the upload
    path uses now — §6.7's question changed from "is this MY account?" to "WHICH
    of my accounts is this?" (§21.2), but the refusal did not.
    """
    assert_account(meta, settings.passbook_account_number)


def resolve_account(
    meta: StatementMeta,
    settings: Settings,
    *,
    client: FireflyClient | None = None,
    allow_register: bool = True,
) -> Account:
    """Route a statement to its account, registering the FIRST one. SPEC §21.2-3.

    Three outcomes, and only the first two are silent:

    * the account is registered — route to it;
    * the registry is **empty** and this is the first statement — register it, so
      a single-account operator never learns this feature exists (§21.3). The
      Firefly asset account is taken from `PASSBOOK_ASSET_ACCOUNT` if set, else
      from Firefly itself when it holds exactly one asset account. It is never
      guessed between several — `doctor` has refused to do that since §7.2 and
      posting 93 rows into the wrong account is tedious to undo;
    * anything else raises `UnknownAccount`, which every front end already turns
      into a 422 that deletes the staged file. **An unregistered account cannot
      silently import.**
    """
    accounts = load_accounts(settings=settings)
    try:
        return route_statement(meta, accounts)
    except UnknownAccount:
        if accounts or not allow_register:
            raise

    asset = (settings.passbook_asset_account or "").strip()
    if not asset and client is not None:
        names = [a["attributes"]["name"] for a in client.asset_accounts()]
        if len(names) == 1:
            asset = names[0]
        elif names:
            raise UnknownAccount(meta.masked_account, []) from None
    if not asset:
        raise UnknownAccount(meta.masked_account, []) from None
    return register_from_statement(meta, asset, accounts)


# --- sync staleness ----------------------------------------------------------
# One definition of the tiers and the wording. `cli.sync_staleness` renders this
# to a terminal and the web template renders the same fields to HTML; neither
# decides anything for itself.


@dataclass
class SyncStatus:
    state: str  # never | ok | warn | stale
    age: int | None
    filename: str | None
    headline: str
    detail: str = ""


def _days(n: int) -> str:
    """`1 day` / `2 days`. "day(s)" is a form field, not a sentence."""
    return f"{n} day" if n == 1 else f"{n} days"


def sync_status() -> SyncStatus:
    synced = last_sync()
    if synced is None:
        return SyncStatus(
            state="never",
            age=None,
            filename=None,
            headline="nothing in archive/ — no statement has been pushed yet",
        )

    name, age = synced
    if age <= SYNC_STALE_DAYS:
        return SyncStatus("ok", age, name, f"last sync {_days(age)} ago ({name})")

    if age <= SYNC_URGENT_DAYS:
        return SyncStatus(
            "warn",
            age,
            name,
            f"last successful sync was {_days(age)} ago ({name})",
            "Canara only serves statements going back so far, so a gap is data loss "
            "rather than lateness — rows that age out of the download window are gone "
            "from every copy, including the backups. Download this week.",
        )

    return SyncStatus(
        "stale",
        age,
        name,
        f"last successful sync was {_days(age)} ago ({name})",
        "Download today. Past three weeks the oldest missing transactions may already "
        "sit outside the range Canara will still hand over. Nothing in this project can "
        "bring those back — not `make restore`, not the off-site archives. They only "
        "ever existed at the bank, and only for a while. There is no cron to catch this "
        "for you (SPEC D7: WSL2 sleeps with Windows).",
    )


# --- push --------------------------------------------------------------------


def push_statement(
    parsed: ParsedStatement,
    settings: Settings,
    client: FireflyClient | None = None,
    *,
    account: Account | None = None,
) -> PushResult:
    """Push one parsed statement. Identical semantics to `passbook sync`.

    `account` decides both the Firefly asset account and the `external_id`
    namespace (§21.1). Without it the statement is routed by its own metadata,
    which is the only source that cannot disagree with itself.
    """
    owned = client is None
    client = client or FireflyClient(settings.firefly_url, settings.firefly_token or "")
    try:
        target = account or resolve_account(parsed.meta, settings, client=client)
        return push_transactions(client, parsed.transactions, target)
    finally:
        if owned:
            client.close()


def archive_statement(
    parsed: ParsedStatement,
    archive: Path = Path("archive"),
    account: Account | None = None,
) -> Path:
    """Move a pushed statement into the archive. Only after a successful push.

    **Per account** since §21.6: `archive/<slug>/<YYYY-MM>/`. Canara names every
    export for the same range identically — `Acnt_stmt__07052026_07082026.xls` —
    so two accounts filed into one folder means the second silently overwrites
    the first, and the archive is the only copy of a statement once `inbox/` is
    cleared.

    Without an account the old flat layout is used, so existing archives and the
    DR drill are untouched. `archived_statements` rglobs, so both layouts are
    read, and `statements_for` attributes each file by what it SAYS rather than
    where it sits.
    """
    target = archive / (account.slug if account else "") / f"{parsed.meta.period_to:%Y-%m}"
    target.mkdir(parents=True, exist_ok=True)
    destination = target / parsed.path.name
    shutil.move(str(parsed.path), str(destination))
    return destination


# --- payee inventory ---------------------------------------------------------


@dataclass
class PayeeRow:
    token: str
    alias: str
    category: str
    channel: str
    count: int
    withdrawn: Decimal
    deposited: Decimal
    first: str
    last: str

    @property
    def total(self) -> Decimal:
        return self.withdrawn + self.deposited

    @property
    def display(self) -> str:
        return self.alias or self.token

    @property
    def needs_decision(self) -> bool:
        """Uncategorised and not one of narration.py's own labels.

        This is the whole point of the payees page: surface what the operator
        has not yet ruled on. It never guesses — D10 measured a 40% error rate
        on inferring meaning from a truncated token.
        """
        return not self.category and self.token not in SYNTHETIC_PAYEES


def rule_categories(rules: dict | None = None) -> dict[str, str]:
    """display-name -> category, inverted out of rules.yaml.

    Rules match on the *display* name (alias where one exists, raw token
    otherwise), because that is what `description` carries at push time.
    """
    rules = rules if rules is not None else load_rules()
    mapping: dict[str, str] = {}
    for spec in rules.get("rules") or []:
        category = spec.get("category")
        if not category:
            continue
        for payee in spec.get("payees") or []:
            mapping[payee] = category
    return mapping


def predict_category(description: str, narration: str, rules: dict | None = None) -> str:
    """What Firefly's rules would set for this row. Mirrors bootstrap.py.

    Inverting the `payees:` lists alone is not enough, and getting that wrong
    made the re-apply preview claim rows would *lose* their category:

    * `description_starts` is a PREFIX match, so a rule listing `Canteen`
      also catches `Canteen (via card)`.
    * Several rules match the raw narration instead — `Bank Charges` via
      `notes_contains: CHARGES`, `Interest Income` via `notes_starts: SBINT`,
      `Credit Card` via `notes_contains: **TCARD`. Those have no payee entry at
      all.
    * Every categorisation rule sets `stop_processing: false`, so all matching
      rules run and the LAST one wins.
    """
    rules = rules if rules is not None else load_rules()
    found = ""
    for spec in rules.get("rules") or []:
        category = spec.get("category")
        if not category:
            continue
        matched = any(description.startswith(p) for p in (spec.get("payees") or []))
        if not matched and spec.get("notes_contains"):
            matched = spec["notes_contains"] in narration
        if not matched and spec.get("notes_starts"):
            matched = narration.startswith(spec["notes_starts"])
        if matched:
            found = category
    return found


def payee_inventory(
    transactions: list[Transaction],
    aliases: dict[str, str] | None = None,
    categories: dict[str, str] | None = None,
) -> list[PayeeRow]:
    """Every token with its alias, category and totals, decisions first."""
    aliases = aliases if aliases is not None else load_payee_aliases()
    categories = categories if categories is not None else rule_categories()
    rules = load_rules()

    grouped: dict[tuple[str, str], list[Transaction]] = {}
    for txn in transactions:
        grouped.setdefault((txn.payee or "(unparsed)", txn.channel), []).append(txn)

    rows = []
    for (token, channel), txns in grouped.items():
        alias = aliases.get(token, "")
        dates = sorted(t.txn_date for t in txns)
        rows.append(
            PayeeRow(
                token=token,
                alias=alias,
                # Predicted the same way Firefly decides, so a notes-matched
                # row (Bank Charges, Interest Income) is not shown as undecided.
                category=categories.get(alias or token, "")
                or predict_category(
                    f"{alias or token} ({channel})", txns[0].narration, rules
                ),
                channel=channel,
                count=len(txns),
                withdrawn=sum((t.debit for t in txns if t.debit), Decimal(0)),
                deposited=sum((t.credit for t in txns if t.credit), Decimal(0)),
                first=dates[0].isoformat(),
                last=dates[-1].isoformat(),
            )
        )
    # Undecided first, then by value: the page exists to be worked top-down.
    rows.sort(key=lambda r: (not r.needs_decision, -r.total, r.token))
    return rows


def unknown_tokens(
    transactions: list[Transaction],
    aliases: dict[str, str] | None = None,
    categories: dict[str, str] | None = None,
) -> list[str]:
    """Tokens this statement introduces that no config mentions yet."""
    return [r.token for r in payee_inventory(transactions, aliases, categories) if r.needs_decision]


def ledger_balance(settings: Settings, client: FireflyClient | None = None) -> Decimal | None:
    """Current balance of the configured asset account, or None if unavailable."""
    owned = client is None
    client = client or FireflyClient(settings.firefly_url, settings.firefly_token or "")
    try:
        for account in client.asset_accounts():
            if account["attributes"]["name"] == settings.passbook_asset_account:
                return Decimal(str(account["attributes"]["current_balance"]))
        return None
    finally:
        if owned:
            client.close()


@dataclass
class ReapplyChange:
    external_id: str
    date: str
    amount: Decimal
    old_description: str
    new_description: str
    old_category: str
    new_category: str

    @property
    def name_changed(self) -> bool:
        return self.old_description != self.new_description

    @property
    def category_changed(self) -> bool:
        return self.old_category != self.new_category


def reapply_preview(
    client: FireflyClient, settings: Settings, archive: Path = Path("archive")
) -> tuple[list[ReapplyChange], int]:
    """What a purge-and-resync would change. Reads only; changes nothing.

    Aliases and rules are applied **at push time**, so editing config leaves
    rows already in Firefly untouched. This compares what is in the ledger
    against what the current config would produce, so the operator sees the
    consequence before anything is deleted.
    """
    from .firefly.push import build_payload

    account_id = None
    for account in client.asset_accounts():
        if account["attributes"]["name"] == settings.passbook_asset_account:
            account_id = account["id"]
    if account_id is None:
        return [], 0

    live: dict[str, dict] = {}
    for group in client.account_transactions(account_id):
        for split in group["attributes"]["transactions"]:
            if split.get("external_id"):
                live[split["external_id"]] = split

    aliases = load_payee_aliases()
    rules = load_rules()

    changes: list[ReapplyChange] = []
    # Statements overlap by design — a weekly download re-covers the previous
    # weeks — so the same txn_id appears in several files. Count and report it
    # once, keyed on the bank's own id.
    seen: set[str] = set()
    for path in sorted(p for p in archive.rglob("*") if p.is_file() and not p.name.startswith(".")):
        try:
            parsed = parse_statement(path, aliases)
        except Exception:
            continue
        for txn in parsed.transactions:
            current = live.get(txn.txn_id)
            if current is None or txn.txn_id in seen:
                continue
            seen.add(txn.txn_id)
            split = build_payload(txn, settings.passbook_asset_account or "")["transactions"][0]
            new_category = predict_category(split["description"], txn.narration, rules)
            change = ReapplyChange(
                external_id=txn.txn_id,
                date=txn.txn_date.isoformat(),
                amount=(txn.debit or txn.credit or Decimal(0)),
                old_description=current.get("description") or "",
                new_description=split["description"],
                old_category=current.get("category_name") or "",
                new_category=new_category,
            )
            if change.name_changed or change.category_changed:
                changes.append(change)

    changes.sort(key=lambda c: c.date)
    return changes, len(seen)


# --- the ledger, aggregated --------------------------------------------------
# SPEC §18. One definition of what counts as spend and what counts as earnings,
# used by every figure and every chart. There was no such definition before this
# phase: §8 and §8.1 established the semantics in the *rules*, and every reader
# of the totals had to remember to apply them.
#
# Getting this wrong is not a rounding error. Measured on one real three-month
# ledger, the naive by-type reading was **three times** the true spend and
# **1.6 times** the true earnings. A chart drawn on the naive numbers is not
# roughly right, and it looks entirely plausible.

# Deposits that are money coming back rather than money earned carry this tag,
# applied by the one strict rule in §8.1. It can never land on a withdrawal:
# the rule triggers on `transaction_type = deposit`.
NOT_EARNINGS_TAG = "not-earnings"

# A refund posts as an ordinary deposit and is tagged by the pusher (§7.2).
REVERSAL_TAG = "reversal"

_CENT = Decimal("0.01")


def load_not_spend(rules: dict | None = None) -> list[str]:
    """Categories that are movement, not spending. From rules.yaml `not_spend`.

    Config, not code, because these are the operator's own category names — the
    same reason `rules.yaml` holds the categorisation itself (D10). A category
    named here that does not exist simply excludes nothing.
    """
    rules = rules if rules is not None else load_rules()
    return [str(c) for c in (rules.get("not_spend") or [])]


def tag_rollups(rules: dict | None = None) -> dict[str, list[str]]:
    """tag -> the categories carrying it, read out of the category rules.

    `food` is not a new concept to configure: four rules already tag their
    category with it, precisely so that total food spend is one query. Deriving
    the group from those rules means the chart and the rules engine cannot
    disagree about what food is.
    """
    rules = rules if rules is not None else load_rules()
    groups: dict[str, list[str]] = {}
    for spec in rules.get("rules") or []:
        tag, category = spec.get("tag"), spec.get("category")
        if tag and category:
            groups.setdefault(str(tag), []).append(str(category))
    return groups


@dataclass(frozen=True)
class Slice:
    name: str
    amount: Decimal
    count: int


@dataclass(frozen=True)
class RollUp:
    """A tag's total, with the categories that make it up."""

    tag: str
    amount: Decimal
    count: int
    parts: list[Slice]


@dataclass(frozen=True)
class MonthTotals:
    month: str  # YYYY-MM
    spend: Decimal
    income: Decimal
    partial: bool


@dataclass(frozen=True)
class LedgerAnalysis:
    gross_spend: Decimal
    spend: Decimal
    gross_income: Decimal
    income: Decimal
    withdrawals: int
    deposits: int
    categories: list[Slice]       # real spend, largest first
    excluded_spend: list[Slice]   # what `not_spend` kept out, largest first
    excluded_income: Slice        # what the not-earnings tag kept out
    refunds: Slice                # reversals: deposits that undo a spend
    rollups: list[RollUp]
    months: list[MonthTotals]
    hours: list[int]              # 24 buckets, real spend only
    clocked: int                  # spend rows with a clock — sums `hours`
    counted: int                  # spend rows in total. NOT the same number.
    uncategorised: Slice
    not_spend: list[str]


def _split_amount(split: dict) -> Decimal:
    """Firefly sends `'48.000000000000'`. Decimal, never float (non-negotiable #1)."""
    return Decimal(str(split.get("amount") or "0")).quantize(_CENT)


def _month_partial(month: str, coverage: tuple[date, date] | None) -> bool:
    """Does the statement coverage stop short of either end of this month?

    A weekly export runs mid-month to mid-month, so the first and last buckets
    of any range are stubs. Two of the reference ledger's four months are
    partial — which is most of the reason nothing here draws a trend line.
    """
    if coverage is None:
        return False
    year, number = int(month[:4]), int(month[5:7])
    first = date(year, number, 1)
    last = date(year, number, calendar.monthrange(year, number)[1])
    return coverage[0] > first or coverage[1] < last


def ledger_analysis(
    splits: Iterable[dict],
    *,
    times: dict[str, time | None] | None = None,
    coverage: tuple[date, date] | None = None,
    rules: dict | None = None,
) -> LedgerAnalysis:
    """Aggregate Firefly's own splits under §8/§8.1's exclusions.

    **Firefly is the source for money and category, the statement for the
    clock.** The category is assigned by the rules engine at store time (D5), so
    reading it back from the ledger is the only way to report it without
    re-implementing categorisation. `txn_time` is parser-derived and is never
    pushed, so it exists only in the statement — hence `times`, keyed on
    `external_id`, which is the bank's transaction id (§6.1).

    A pure function over data: no HTTP, no file reads. The tests feed it splits.
    """
    rules = rules if rules is not None else load_rules()
    not_spend = load_not_spend(rules)
    rollup_members = tag_rollups(rules)
    times = times or {}

    gross_spend = gross_income = spend = income = Decimal(0)
    withdrawals = deposits = 0
    by_category: dict[str, list[Decimal]] = {}
    excluded: dict[str, list[Decimal]] = {}
    excluded_income = [Decimal(0), 0]
    refunds = [Decimal(0), 0]
    by_tag: dict[str, list[Decimal]] = {}
    months: dict[str, list[Decimal]] = {}
    hours = [0] * 24
    clocked = counted = 0

    for split in splits:
        kind = split.get("type")
        amount = _split_amount(split)
        month = str(split.get("date") or "")[:7]
        tags = split.get("tags") or []

        if kind == "withdrawal":
            withdrawals += 1
            gross_spend += amount
            category = split.get("category_name") or "(no category)"
            if category in not_spend:
                bucket = excluded.setdefault(category, [Decimal(0), 0])
                bucket[0] += amount
                bucket[1] += 1
                continue

            spend += amount
            bucket = by_category.setdefault(category, [Decimal(0), 0])
            bucket[0] += amount
            bucket[1] += 1
            for tag in tags:
                if tag in rollup_members:
                    slot = by_tag.setdefault(tag, [Decimal(0), 0])
                    slot[0] += amount
                    slot[1] += 1
            if month:
                months.setdefault(month, [Decimal(0), Decimal(0)])[0] += amount

            counted += 1
            # Tolerant join (§21.1): the split's external_id may be namespaced
            # (`canara-1111-2026…`) or bare, and `times` is keyed on the bank's
            # own id — which is only unique WITHIN an account, so `times` must be
            # built from that one account's statements. `transaction_times`
            # enforces that by taking statements, not a whole archive.
            external = str(split.get("external_id") or "")
            # Namespaced key first, bare id as the fallback: with two accounts the
            # bare id is ambiguous (§21.1), so the caller keys the map on the
            # external_id it pushed and only falls back for a pre-migration row.
            moment = times.get(external)
            if moment is None:
                moment = times.get(txn_id_of(external))
            if moment is not None:
                hours[moment.hour] += 1
                clocked += 1

        elif kind == "deposit":
            deposits += 1
            gross_income += amount
            if REVERSAL_TAG in tags:
                refunds[0] += amount
                refunds[1] += 1
            if NOT_EARNINGS_TAG in tags:
                excluded_income[0] += amount
                excluded_income[1] += 1
                continue
            income += amount
            if month:
                months.setdefault(month, [Decimal(0), Decimal(0)])[1] += amount
        # anything else — an opening balance, a reconciliation — is neither.

    def slices(source: dict[str, list]) -> list[Slice]:
        return sorted(
            (Slice(name, total, int(n)) for name, (total, n) in source.items()),
            key=lambda s: (-s.amount, s.name),
        )

    return LedgerAnalysis(
        gross_spend=gross_spend,
        spend=spend,
        gross_income=gross_income,
        income=income,
        withdrawals=withdrawals,
        deposits=deposits,
        categories=slices(by_category),
        excluded_spend=slices(excluded),
        excluded_income=Slice(NOT_EARNINGS_TAG, excluded_income[0], int(excluded_income[1])),
        refunds=Slice(REVERSAL_TAG, refunds[0], int(refunds[1])),
        rollups=[
            RollUp(
                tag=tag,
                amount=total,
                count=int(n),
                parts=[s for s in slices(by_category) if s.name in rollup_members[tag]],
            )
            for tag, (total, n) in sorted(by_tag.items(), key=lambda kv: -kv[1][0])
        ],
        months=[
            MonthTotals(month, totals[0], totals[1], _month_partial(month, coverage))
            for month, totals in sorted(months.items())
        ],
        hours=hours,
        clocked=clocked,
        counted=counted,
        uncategorised=Slice(
            "(no category)",
            by_category.get("(no category)", [Decimal(0), 0])[0],
            int(by_category.get("(no category)", [Decimal(0), 0])[1]),
        ),
        not_spend=not_spend,
    )


# --- accounts: routing, and the id namespace ---------------------------------
# SPEC §21. The bank's transaction id is `YYYYMMDD` + a per-date ordinal
# **sequenced per account**, so two Canara accounts emit identical ids. Every
# read here therefore tolerates both forms and every write is namespaced.

_BARE_TXN_ID = re.compile(r"^\d{14}$")
_NAMESPACED = re.compile(r"^(?P<slug>[a-z0-9][a-z0-9-]*)-(?P<txn_id>\d{14})$")


def txn_id_of(external_id: str) -> str:
    """The bank's own id, from either form.

    `canara-1111-20260509000001` -> `20260509000001`, and a bare id passes
    through. Tolerant reads are what let the migration (§21.2) be run when it
    suits instead of being forced by a version bump.
    """
    text = (external_id or "").strip()
    match = _NAMESPACED.match(text)
    return match.group("txn_id") if match else text


def slug_of(external_id: str) -> str | None:
    """Which account pushed this row, or None for a pre-migration id."""
    match = _NAMESPACED.match((external_id or "").strip())
    return match.group("slug") if match else None


def is_namespaced(external_id: str) -> bool:
    return bool(_NAMESPACED.match((external_id or "").strip()))


def route_statement(meta: StatementMeta, accounts: list[Account]) -> Account:
    """Which of my accounts is this statement for? SPEC §21.2.

    This replaces §6.7's question. The old one — "is this MY account?" — could
    only ever be asked of one account, and answering it wrong is the failure that
    assertion has guarded since Phase 2: a misfiled statement silently corrupting
    a ledger. With a registry the question changes but the refusal does not.

    Matched on the **full** account number, never the mask: two accounts can
    share their last four, which `assert_account` already documents.
    """
    number = meta.account_number.strip()
    for account in accounts:
        if account.account_number.strip() == number:
            return account
    raise UnknownAccount(meta.masked_account, [a.slug for a in accounts])


def register_from_statement(
    meta: StatementMeta,
    asset_account: str,
    accounts: list[Account] | None = None,
    *,
    bank: str = "canara",
) -> Account:
    """Add the account this statement belongs to. SPEC §21.3.

    The zero-config path: the first statement an install uploads registers its
    own account, so a single-account operator never learns this feature exists.
    The slug defaults to `<bank>-<last4>` and is disambiguated if that is taken —
    it is part of every `external_id` the account will ever push, so it has to be
    unique by construction rather than by hope.
    """
    accounts = list(accounts if accounts is not None else load_accounts())
    slug = default_slug(bank, meta.account_number)
    if any(a.slug == slug for a in accounts):
        suffix = 2
        while any(a.slug == f"{slug}-{suffix}" for a in accounts):
            suffix += 1
        slug = f"{slug}-{suffix}"
    account = Account(
        slug=slug,
        bank=bank,
        account_number=meta.account_number.strip(),
        asset_account=asset_account.strip(),
        label=meta.account_name.strip()[:40],
    )
    accounts.append(account)
    save_accounts(accounts)
    log.warning("registered account %s (%s) -> %r", account.slug, account.masked, account.asset_account)
    return account


def statements_for(
    account: Account, statements: Iterable[ParsedStatement]
) -> list[ParsedStatement]:
    """Only the statements belonging to this account. §21.6.

    `archive/` is per-account by directory, but this filters on what the
    STATEMENT says rather than where it sits: a file moved by hand into the wrong
    folder must not be attributed to the wrong ledger.
    """
    number = account.account_number.strip()
    return [s for s in statements if s.meta.account_number.strip() == number]


# --- ledger integrity: the check that was missing -----------------------------
# SPEC §20. The continuity invariant (§6.6) validates a *file* at parse time.
# Nothing validated the *ledger*, and on 2026-08-11 that gap cost seven hours: a
# purge and a re-push that stopped after 21 of 93 rows left Firefly holding a
# self-consistent balance, and 349 tests, `doctor`, `make check` and the status
# strip all passed while the ledger was a third of itself (§19).
#
# So: compare the ledger against the statements that built it. This is the one
# check that catches that corruption whatever caused it — an interrupted purge, a
# hand-deleted row in Firefly's own UI, a restore of the wrong dump.


@dataclass(frozen=True)
class Check:
    """One assertion about the live ledger.

    `ok` is **tri-state on purpose**. `None` means "not checked here", which is
    not a pass: reporting a green tick for something never looked at is the exact
    failure this module exists to prevent.
    """

    name: str
    ok: bool | None
    detail: str


@dataclass(frozen=True)
class LedgerVerdict:
    checks: list[Check]

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.ok is False]

    @property
    def unchecked(self) -> list[Check]:
        return [c for c in self.checks if c.ok is None]

    @property
    def ok(self) -> bool:
        """No check failed. Unchecked ones do not make it false — they are
        reported separately, because "cannot see" and "fine" are different."""
        return not self.failed

    @property
    def headline(self) -> str:
        if self.failed:
            first = self.failed[0]
            return f"{first.name}: {first.detail}"
        if self.unchecked:
            return f"{len(self.checks) - len(self.unchecked)} of {len(self.checks)} checks passed"
        return f"all {len(self.checks)} checks passed"


def verify_ledger(
    client: FireflyClient,
    account: "Account | Settings",
    archive: Path = Path("archive"),
    *,
    trashed: int | None = None,
    intents: list[str] | None = None,
) -> LedgerVerdict:
    """Assert the live ledger still matches the statements that built it.

    `trashed` is passed in rather than looked up: **Firefly's API cannot answer
    it.** Verified against the pinned tag — `routes/api.php` exposes exactly two
    `data/*` routes, `DELETE data/destroy` and `DELETE data/purge`, and neither
    lists soft-deleted journals. Counting them needs the database, which the web
    container deliberately has no credentials for (§15.1). The CLI supplies it;
    everywhere else the check reports itself unchecked rather than passing.
    """
    # Accepts an `Account` or, for the pre-registry path, a `Settings`. §21.6:
    # every check below is scoped to ONE account, because a ledger holding two
    # accounts would otherwise report each one's rows as missing from the other.
    if not isinstance(account, Account):
        account = Account(
            slug=default_slug("canara", account.passbook_account_number or "0000"),
            bank="canara",
            account_number=(account.passbook_account_number or "").strip(),
            asset_account=(account.passbook_asset_account or "").strip(),
        )

    statements = statements_for(account, archived_statements(archive))
    checks: list[Check] = []

    account_id = None
    balance: Decimal | None = None
    for live_account in client.asset_accounts():
        if live_account["attributes"]["name"] == account.asset_account:
            account_id = live_account["id"]
            balance = Decimal(
                str(live_account["attributes"]["current_balance"])
            ).quantize(_CENT)

    if account_id is None:
        return LedgerVerdict([
            Check(
                "account",
                False,
                f"no asset account named {account.asset_account!r} — "
                "nothing can be verified against it",
            )
        ])

    splits = [
        split
        for group in client.account_transactions(account_id)
        for split in group["attributes"]["transactions"]
    ]
    raw_ids = [str(s["external_id"]) for s in splits if s.get("external_id")]
    # Tolerant read (§21.1): a row pushed before the migration carries the bank's
    # bare id, one pushed after carries `<slug>-<txn_id>`. Both map to the same
    # transaction, and comparing them any other way would report the entire
    # ledger as missing the day the scheme changed.
    live_ids = {txn_id_of(external) for external in raw_ids}
    stale_ids = [external for external in raw_ids if not is_namespaced(external)]
    foreign = [
        external
        for external in raw_ids
        if is_namespaced(external) and slug_of(external) != account.slug
    ]
    openings = [s for s in splits if s.get("type") == "opening balance"]

    # 1. balance against the newest statement's own closing figure ------------
    if not statements:
        checks.append(Check("balance", None, "nothing in archive/ to compare against"))
    else:
        newest = max(statements, key=lambda s: (s.meta.period_to, s.path.stat().st_mtime))
        expected = newest.meta.closing_balance
        drift = (balance or Decimal(0)) - expected
        checks.append(
            Check(
                "balance",
                drift == 0,
                (
                    f"{balance} matches {newest.path.name}'s closing balance"
                    if drift == 0
                    else f"Firefly says {balance}, {newest.path.name} closes at "
                    f"{expected} — out by {drift:+}"
                ),
            )
        )

    # 2. every archived transaction is in the ledger, and nothing else is -----
    if not statements:
        checks.append(Check("rows", None, "nothing in archive/ to compare against"))
    else:
        expected_ids = {t.txn_id for s in statements for t in s.transactions}
        missing = sorted(expected_ids - live_ids)
        unexpected = sorted(live_ids - expected_ids)
        if not missing and not unexpected:
            checks.append(
                Check("rows", True, f"{len(live_ids)} rows, one per archived transaction")
            )
        else:
            parts = []
            if missing:
                sample = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
                parts.append(f"{len(missing)} archived row(s) MISSING from Firefly ({sample})")
            if unexpected:
                sample = ", ".join(unexpected[:5]) + (" …" if len(unexpected) > 5 else "")
                parts.append(f"{len(unexpected)} row(s) in Firefly with no statement ({sample})")
            checks.append(
                Check("rows", False, f"{len(live_ids)} live vs {len(expected_ids)} archived — "
                                     + "; ".join(parts))
            )

    # 3. tombstones — see the docstring for why this is passed in ------------
    if trashed is None:
        checks.append(
            Check(
                "trashed",
                None,
                "needs the database; Firefly's API cannot list soft-deleted "
                "journals and this process has no DB access (§15.1). Run "
                "`passbook verify-ledger` on the host.",
            )
        )
    else:
        checks.append(
            Check(
                "trashed",
                trashed == 0,
                "no soft-deleted journals"
                if trashed == 0
                else f"{trashed} soft-deleted journal(s) remain — a re-push of "
                "identical rows will be refused as duplicates (§7.3). "
                "`passbook purge --confirm --yes` force-deletes them.",
            )
        )

    # 4. no purge left half-finished -----------------------------------------
    outstanding = intents if intents is not None else []
    checks.append(
        Check(
            "purge intent",
            not outstanding,
            "no purge left unfinished"
            if not outstanding
            else f"{len(outstanding)} unfinished purge(s): {', '.join(outstanding)} — "
            "run `passbook purge --resume`",
        )
    )

    # 5. the id namespace — the migration, and rows from another account -----
    if foreign:
        checks.append(
            Check(
                "id namespace",
                False,
                f"{len(foreign)} row(s) on this account carry another account's "
                f"namespace ({', '.join(sorted({slug_of(f) or '?' for f in foreign}))}) "
                "— they were pushed into the wrong ledger",
            )
        )
    elif stale_ids:
        checks.append(
            Check(
                "id namespace",
                False,
                f"{len(stale_ids)} row(s) still carry the bank's bare id, which is "
                f"sequenced per account and collides between accounts (§21.1). "
                f"Run the migration in §21.2; until then a second Canara account "
                "cannot be added safely.",
            )
        )
    else:
        checks.append(
            Check("id namespace", True, f"all {len(raw_ids)} row(s) namespaced {account.slug}-*")
        )

    # 6. the opening balance, which is what makes the balance mean anything ---
    if len(openings) == 1 and not openings[0].get("external_id"):
        amount = Decimal(str(openings[0].get("amount") or "0")).quantize(_CENT)
        checks.append(Check("opening balance", True, f"present, {amount}, no external_id"))
    elif not openings:
        checks.append(
            Check(
                "opening balance",
                False,
                "MISSING — without it Firefly's balance cannot equal the bank's, "
                "and every figure on the account is short by the opening amount",
            )
        )
    elif len(openings) > 1:
        checks.append(
            Check("opening balance", False, f"{len(openings)} opening balances on one account")
        )
    else:
        checks.append(
            Check(
                "opening balance",
                False,
                "carries an external_id, so `purge` would delete it — that id is "
                "what makes the exclusion structural (§7.3)",
            )
        )

    return LedgerVerdict(checks)


# --- statements on disk ------------------------------------------------------


def archived_statements(archive: Path = Path("archive")) -> list[ParsedStatement]:
    """Every archived statement, parsed. A bad file is skipped, never fatal.

    Statements overlap by design (a weekly download re-covers earlier weeks), so
    callers dedupe on `txn_id` — the bank's own key (§6.1).
    """
    if not archive.is_dir():
        return []
    out = []
    for path in sorted(p for p in archive.rglob("*") if p.is_file() and not p.name.startswith(".")):
        try:
            out.append(parse_statement(path))
        except Exception as exc:  # one unreadable archive must not blank a page
            log.warning("skipping %s: %s", path.name, exc)
            continue
    return out


def statement_coverage(statements: Iterable[ParsedStatement]) -> tuple[date, date] | None:
    """The union of the statement periods — what the ledger can speak about.

    Deliberately the *periods*, not the first and last transaction dates: a
    quiet fortnight at the start of a range is covered, not missing, and using
    transaction dates would silently turn it into a shorter month.
    """
    periods = [(s.meta.period_from, s.meta.period_to) for s in statements]
    if not periods:
        return None
    return min(p[0] for p in periods), max(p[1] for p in periods)


def transaction_times(statements: Iterable[ParsedStatement]) -> dict[str, time | None]:
    """txn_id -> clock, for the Day Rail. The only place time of day exists.

    **Pass one account's statements.** The key is the bank's own id, which is
    sequenced per account and therefore collides between accounts (§21.1): mixing
    two accounts here would silently attach one account's clock to the other's
    transaction. `statements_for()` is how callers narrow it.
    """
    times: dict[str, time | None] = {}
    for statement in statements:
        for txn in statement.transactions:
            times.setdefault(txn.txn_id, txn.txn_time)
    return times


def account_transactions(
    account: Account,
    archive: Path = Path("archive"),
    extra: Iterable[ParsedStatement] = (),
) -> list[Transaction]:
    """Every transaction this account has archived, deduped within the account.

    Deduped on the bank's id, which is safe **because the set is already narrowed
    to one account**. Deduping across accounts on that key is exactly the data
    loss §21.1 exists to prevent: the two fixture statements share all 93 ids, so
    a naive merge keeps 93 of 186 rows and reports success.
    """
    statements = statements_for(account, [*archived_statements(archive), *extra])
    seen: dict[str, Transaction] = {}
    for statement in statements:
        for txn in statement.transactions:
            seen.setdefault(txn.txn_id, txn)
    return list(seen.values())


def sync_history(archive: Path = Path("archive"), limit: int = 10) -> list[dict]:
    """Recently archived statements, newest first. Filenames only, no contents."""
    if not archive.is_dir():
        return []
    files = [p for p in archive.rglob("*") if p.is_file() and not p.name.startswith(".")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "name": p.name,
            # Date only: with the time attached this column clipped at 390px
            # ("2026-08-08 0…"), and the minute a file was archived has never
            # been the question.
            "when": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d"),
            "folder": p.parent.name,
        }
        for p in files[:limit]
    ]
