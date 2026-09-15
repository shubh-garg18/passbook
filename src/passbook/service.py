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
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
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
# The rules engine lives in `rules.py` now that passbook applies it itself
# (§36). Re-exported here because every caller was written against
# `service.predict_category`, and a rename would be a diff on all of them
# for no gain.
from .rules import (  # noqa: F401
    load_rules,
    managed_tags,
    predict_category,
    predict_tags,
    rule_categories,
)
from .push import PushResult, push_transactions
from .store import LedgerError, LedgerStore, open_ledger
from .identity import (  # noqa: F401  (re-exported, §119)
    _NAMESPACED,
    _TXN_ID_FORM,
    is_namespaced,
    slug_of,
    txn_id_of,
)
from . import index as index_mod
from . import parsecache
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


def parse_statement(
    path: Path, aliases: dict[str, str] | None = None, password: str | None = None
) -> ParsedStatement:
    """Load, enrich and validate. Raises rather than exiting.

    Propagates ParseError, BalanceBreak and IntegrityError untouched — the
    balance invariant is never softened for a caller's convenience, web
    included. CLAUDE.md non-negotiable #3.
    """
    # §110. The memo covers the FILE PARSE and nothing after it. Enrichment and
    # validation still run every time, so a new alias, a new narration grammar
    # or a changed rule takes effect on the next page load rather than on the
    # next time somebody remembers to clear a cache — which is the seam that
    # makes this safe to keep forever.
    remembered = parsecache.load(path)
    if remembered is None:
        meta, transactions = load_statement(path, password)
        parsecache.store(path, meta, transactions)
    else:
        meta, transactions = remembered
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
    store: LedgerStore | None = None,
    allow_register: bool = True,
) -> Account:
    """Route a statement to its account, registering the FIRST one. SPEC §21.2-3.

    Three outcomes, and only the first two are silent:

    * the account is registered — route to it;
    * the registry is **empty** and this is the first statement — register it, so
      a single-account operator never learns this feature exists. The asset
      account is taken from `PASSBOOK_ASSET_ACCOUNT` if set, else from the
      ledger itself when it holds exactly one. It is never guessed between
      several — `doctor` has refused to do that since the first release, and
      posting a statement into the wrong account is tedious to undo;
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
    if not asset and store is not None:
        names = [a["name"] for a in store.asset_accounts()]
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
    # ISO, or None when nothing has ever been pushed. The masthead stamps it,
    # so it travels with the age rather than being derived from a second field.
    date: str | None = None


def _days(n: int) -> str:
    """`1 day` / `2 days`. "day(s)" is a form field, not a sentence."""
    return f"{n} day" if n == 1 else f"{n} days"


#: How bad each state is, worst first. `stale` outranks `never` because it is
#: the one with a clock on it: rows that age out of the bank's download window
#: are gone from every copy, and an account nobody has pushed to yet is a thing
#: you can still do at leisure.
_SYNC_SEVERITY = {"stale": 3, "never": 2, "warn": 1, "ok": 0}


def _worst(statuses) -> "SyncStatus":
    ordered = sorted(statuses, key=lambda s: -_SYNC_SEVERITY.get(s.state, 0))
    return ordered[0] if ordered else SyncStatus("never", None, None, "no accounts")


def scoped_paths(
    accounts: "list[Account] | None", archive: Path = Path("archive")
) -> set[Path] | None:
    """The archived files belonging to these accounts. SPEC §104.

    None for "every file", which is what an unscoped caller wants and what a
    single-account install has always had.

    **Decided by reading the statement, not by where it sits.** `archive/` is
    per-account by directory (§21.6), so a path test would nearly always agree —
    and it would be wrong twice: for the pre-registry layout, where files sit
    directly under `archive/<month>/` with no slug in the path at all, and for a
    file moved by hand into the wrong folder. `statements_for` already answers
    this question by account number, and having one rule rather than two is the
    whole point. It is cheap now: the archive parses once and is cached (§101).
    """
    if accounts is None:
        return None
    # §118. Out of the index. The attribution is the same one — decided by what
    # the statement SAYS, at the moment it was indexed — but asking for it no
    # longer costs a parse of the whole archive on every page load.
    numbers = [a.account_number.strip() for a in accounts]
    try:
        with index_mod.connect() as conn:
            index_mod.sync(archive, conn, parse_statement)
            return index_mod.paths(conn, numbers)
    except Exception as exc:
        log.warning("index unavailable, reading the archive directly: %s", exc)
        statements = archived_statements(archive)
        keep: set[Path] = set()
        for account in accounts:
            keep.update(s.path for s in statements_for(account, statements))
        return keep


def sync_status(
    accounts: "list[Account] | None" = None, archive: Path = Path("archive")
) -> SyncStatus:
    """How long since a statement last reached the ledger. SPEC §104.

    Scoped to `accounts` when given. Unscoped it answered for the whole archive,
    which on a two-account install put a Canara download warning on the Union
    tab — *"this shouldnt be seen on ledger when union is selected"*. The
    warning is about one bank's download window, so it belongs to that bank's
    account and not to the page.

    **Over several accounts it aggregates to the WORST, never the newest file.**
    Taking the newest across a combined scope is how a three-week-old Canara gap
    disappears behind a Union statement pushed this morning — one fresh account
    silently answering for a stale one, which is the exact dilution §18 warned
    about and which this function reintroduced the day it learned to scope.
    """
    if accounts is not None and len(accounts) > 1:
        return _worst(sync_status([a], archive) for a in accounts)

    synced = last_sync(archive, only=scoped_paths(accounts, archive))
    if synced is None:
        # Named, when the question is about one account. "Nothing in archive/"
        # on a two-account install reads as a broken page rather than as an
        # account nobody has pushed to yet — which is a thing the operator can
        # act on in one click, and the reason the sentence exists.
        one = accounts[0] if accounts and len(accounts) == 1 else None
        return SyncStatus(
            state="never",
            age=None,
            filename=None,
            headline=(
                f"no statement has been pushed for {one.display} yet"
                if one
                else "nothing in archive/ — no statement has been pushed yet"
            ),
            detail=(
                "Its rows are not in the ledger, so every figure on this page is "
                "empty rather than zero. Upload a statement to fill it."
                if one
                else ""
            ),
        )

    name, age, when = synced
    stamped = when.isoformat()
    if age <= SYNC_STALE_DAYS:
        return SyncStatus(
            "ok", age, name, f"last sync {_days(age)} ago ({name})", date=stamped
        )

    if age <= SYNC_URGENT_DAYS:
        return SyncStatus(
            "warn",
            age,
            name,
            f"last successful sync was {_days(age)} ago ({name})",
            "Canara only serves statements going back so far, so a gap is data loss "
            "rather than lateness — rows that age out of the download window are gone "
            "from every copy, including the backups. Download this week.",
            date=stamped,
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
        date=stamped,
    )


# --- push --------------------------------------------------------------------


def push_statement(
    parsed: ParsedStatement,
    settings: Settings,
    store: LedgerStore | None = None,
    *,
    account: Account | None = None,
) -> PushResult:
    """Write one parsed statement. Identical semantics to `passbook sync`.

    `account` decides both the asset account and the `external_id` namespace.
    Without it the statement is routed by its own metadata, which is the only
    source that cannot disagree with itself.

    The rules run here rather than at the ledger, so every row is stored already
    carrying the category and the tags config says it should have.
    """
    owned = store is None
    store = store or open_ledger(settings)
    try:
        target = account or resolve_account(parsed.meta, settings, store=store)
        return push_transactions(
            store,
            parsed.transactions,
            target,
            rules=load_rules(),
            threshold=settings.large_txn_threshold,
        )
    finally:
        if owned:
            store.close()


def archive_statement(
    parsed: ParsedStatement,
    archive: Path = Path("archive"),
    account: Account | None = None,
    *,
    password: str | None = None,
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

    **An encrypted PDF is stored decrypted.** SPEC §105.1. Measured, on the
    first non-Canara statement to reach the archive: the file went in still
    locked and `archived_statements` skipped it with
    `This PDF is encrypted and needs its password` — so the rows were in the
    ledger and every figure derived from the *statement* was empty. The balance
    chart, the Day Rail, the time-of-day column and the payee inventory all read
    the archive, and `verify_ledger` compares against it, which means §20 could
    not check the account at all and said so as a warning nobody would connect
    to a password.

    The alternative was storing the password, and a password kept for the life
    of an archive is a credential with no expiry. The protection it offered was
    for the file **in transit** — this one arrives by email — and it buys
    nothing here: `archive/` already sits beside `config/accounts.yaml`, which
    holds account numbers in full, and `backups/`, which holds the whole ledger.
    Both are gitignored and neither is encrypted.

    So the archive holds a readable copy and the operator's own download stays
    exactly as the bank sent it. `passbook verify-ledger` re-reads what is
    archived, which is the whole reason it has to be readable.
    """
    target = archive / (account.slug if account else "") / f"{parsed.meta.period_to:%Y-%m}"
    target.mkdir(parents=True, exist_ok=True)
    destination = target / parsed.path.name
    if _unlock_into(parsed.path, destination, password):
        parsed.path.unlink(missing_ok=True)
    else:
        shutil.move(str(parsed.path), str(destination))
    return destination


def _unlock_into(source: Path, destination: Path, password: str | None) -> bool:
    """Write `source` to `destination` without its password. SPEC §105.1.

    False when there is nothing to unlock — not a PDF, or a PDF that was never
    encrypted — and the caller moves the file as it always did.

    Raises nothing it can avoid raising: a file this cannot rewrite is better
    archived locked than not archived at all, because the push has already
    happened and losing the statement is the worse outcome.
    """
    if source.suffix.lower() != ".pdf":
        return False
    try:
        import pikepdf
    except ImportError:  # pragma: no cover — pikepdf is a hard dependency
        return False
    try:
        with pikepdf.open(source) as document:
            if not document.is_encrypted:
                return False
    except pikepdf.PasswordError:
        pass
    except Exception as exc:
        log.warning("could not inspect %s before archiving: %s", source.name, exc)
        return False

    from .loaders.pdf import _decrypt

    try:
        # The caller's password, because only the caller has it: it lives for
        # one request and is never stored (§30). `_decrypt` falls back to the
        # configured one when this is None, which is the Canara path.
        buffer = _decrypt(source, password)
    except Exception as exc:
        log.warning(
            "archiving %s still encrypted: %s. Every figure read from the "
            "statement will be empty for this account until it is replaced with "
            "a readable copy.",
            source.name,
            exc,
        )
        return False

    destination.write_bytes(buffer.getvalue() if hasattr(buffer, "getvalue") else buffer.read())
    log.info("archived %s with its password removed (§105.1)", source.name)
    return True


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
                # Predicted the same way the ledger decides, so a notes-matched
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


def ledger_balance(settings: Settings, store: LedgerStore | None = None) -> Decimal | None:
    """Current balance of the configured asset account, or None if unavailable."""
    owned = store is None
    store = store or open_ledger(settings)
    try:
        for account in store.asset_accounts():
            if account["name"] == settings.passbook_asset_account:
                return Decimal(str(account["current_balance"]))
        return None
    finally:
        if owned:
            store.close()


@dataclass
class ReapplyChange:
    """One live row, and what the current config says it should be.

    Addressed by `external_id`, which is what makes the join provable: a change
    carrying an id the ledger does not hold never matched a live row, and must
    never be reported as one. It used to carry a second id as well — the group
    an update had to be aimed at — and that id is gone, because the identity is
    the address now.
    """

    external_id: str
    date: str
    amount: Decimal
    old_description: str
    new_description: str
    old_category: str
    new_category: str
    old_counterparty: str = ""
    new_counterparty: str = ""
    old_tags: tuple[str, ...] = ()
    new_tags: tuple[str, ...] = ()
    kind: str = "withdrawal"

    @property
    def name_changed(self) -> bool:
        return self.old_description != self.new_description

    @property
    def category_changed(self) -> bool:
        return self.old_category != self.new_category

    @property
    def counterparty_changed(self) -> bool:
        return self.old_counterparty != self.new_counterparty

    @property
    def tags_changed(self) -> bool:
        return set(self.old_tags) != set(self.new_tags)

    @property
    def changed(self) -> bool:
        return (
            self.name_changed
            or self.category_changed
            or self.counterparty_changed
            or self.tags_changed
        )


def _live_splits(store, account: str) -> dict[str, dict]:
    """`external_id -> row`, keyed on the id **as the ledger holds it**.

    Keyed on the whole `external_id`, never on the bank's bare `txn_id`
    (non-negotiable 10). The lookup side does the tolerating, in `_match`.

    It used to return `(group id, row)`, because an update had to be addressed
    to the group the row lived in. There are no groups: the identity is the
    address.
    """
    return {
        str(row["external_id"]): row
        for row in store.account_transactions(account)
        if row.get("external_id")
    }


def rows_in_ledger(store, asset_account: str) -> int:
    """How many rows the ledger holds against this asset account, by name.

    Public because removing an account has to say how many rows it is about to
    stop managing, and a management screen reaching into `_live_splits` to find
    that out would be the second caller of a private helper — which is how a
    private helper stops being one without anybody deciding it should.

    An asset account the ledger has never heard of is 0, not an error: the
    registry can name one that no longer exists, and that is precisely a state
    the operator is entitled to clean up from here.
    """
    return len(store.identities(asset_account))


def _match(
    live: dict[str, dict], account: Account | None, txn_id: str
) -> tuple[str, dict] | None:
    """Find one row, namespaced form first, bare form second. §21.1.

    Both forms are tried because a ledger may hold rows from before the
    namespacing migration alongside rows from after it, and this is one asset
    account's transactions — so a bare-id fallback cannot reach across
    accounts the way a bare-id *key* did.

    This function exists because the join was wrong and silently so: `live` was
    keyed on the namespaced id and looked up with the bare one, which matched
    **nothing**. On the reference ledger that is 0 of 113 rows compared, and the
    page said "All 0 transactions already match. Nothing to do."
    """
    for candidate in ([account.external_id(txn_id)] if account else []) + [txn_id]:
        found = live.get(candidate)
        if found is not None:
            return candidate, found
    return None


def _counterparty(split: dict) -> str:
    """The name on the other side.

    It used to be whichever of `source_name`/`destination_name` the direction
    did not use, because the previous store modelled every row as a transfer
    between two accounts and this had to work out which side was the payee. It
    is a column now, and this is the one line left of that.
    """
    return str(split.get("counterparty") or "")


def reapply_preview(
    store: LedgerStore,
    settings: Settings,
    archive: Path = Path("archive"),
    *,
    aliases: dict[str, str] | None = None,
    rules: dict | None = None,
    accounts: list[Account] | None = None,
) -> tuple[list[ReapplyChange], int]:
    """What the current config would change in the ledger. Reads only.

    Aliases and rules are applied **when a row is written**, so editing config
    leaves rows already in the ledger untouched. This compares what is in the
    ledger against what the current config would produce.

    `aliases` and `rules` override what is on disk, so the confirm screen can
    show the consequence of a config change *before* it is written rather than
    after.
    """
    from .push import build_split

    aliases = load_payee_aliases() if aliases is None else aliases
    rules = load_rules() if rules is None else rules
    registry = load_accounts(settings=settings) if accounts is None else accounts
    managed = managed_tags(rules)

    known = {str(a["name"]) for a in store.asset_accounts()}

    statements = archived_statements(archive)
    changes: list[ReapplyChange] = []
    considered = 0

    for account in registry or [None]:
        target = account.asset_account if account else settings.passbook_asset_account
        if target not in known:
            # Skipped, but never silently: `considered` then stays 0, and every
            # caller is required to read that as "nothing was compared" rather
            # than as a pass. The log says which account went missing.
            log.warning(
                "no asset account named %r; %s compared nothing",
                target,
                account.slug if account else "the unregistered ledger",
            )
            continue
        live = _live_splits(store, target or "")
        mine = statements_for(account, statements) if account else statements

        # Statements overlap by design — a weekly download re-covers earlier
        # weeks — so the same row appears in several files. Count it once,
        # keyed on the id it carries in the ledger.
        seen: set[str] = set()
        for parsed in mine:
            try:
                enriched = parse_statement(parsed.path, aliases)
            except Exception:  # an unreadable archive must not blank the page
                continue
            for txn in enriched.transactions:
                found = _match(live, account, txn.txn_id)
                if found is None:
                    continue
                external_id, current = found
                if external_id in seen:
                    continue
                seen.add(external_id)

                split = build_split(txn, account or (target or ""))

                # Only the managed tags are reconciled; everything else the row
                # carries is preserved verbatim. `reversal` is the pusher's and
                # `large-oneoff` is set once at write time — see `managed_tags`.
                held = {str(t) for t in (current.get("tags") or [])}
                wanted = (held - managed) | predict_tags(
                    split["description"], txn.narration, split["kind"], rules
                )

                change = ReapplyChange(
                    external_id=external_id,
                    date=txn.txn_date.isoformat(),
                    amount=(txn.debit or txn.credit or Decimal(0)),
                    old_description=str(current.get("description") or ""),
                    new_description=split["description"],
                    old_category=str(current.get("category") or ""),
                    new_category=predict_category(
                        split["description"], txn.narration, rules
                    ),
                    old_counterparty=_counterparty(current),
                    new_counterparty=_counterparty(split),
                    old_tags=tuple(sorted(held)),
                    new_tags=tuple(sorted(wanted)),
                    kind=split["kind"],
                )
                if change.changed:
                    changes.append(change)
        considered += len(seen)

    changes.sort(key=lambda c: (c.date, c.external_id))
    return changes, considered


@dataclass
class SyncResult:
    """What an in-place sync actually did. SPEC §23."""

    updated: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0


def sync_ledger(
    store: LedgerStore,
    changes: list[ReapplyChange],
    *,
    on_progress=None,
) -> SyncResult:
    """Write the current config onto rows already in the ledger.

    **The non-destructive half of re-apply.** A rename or a re-categorisation
    changes three fields on an existing row and nothing else, so it does not
    need the row deleted and written again. That keeps the database dump off
    the critical path: nothing is deleted, the write is idempotent, and config
    is the source of truth — so a failed run is re-run, not recovered.

    **It cannot move money, and that is enforced rather than observed.** The
    update used to be safe because the other application's update happened to
    be sparse, and an absent field happened to be an untouched one. The store
    refuses `amount`, `txn_date`, `kind`, `external_id`, `notes` and `account`
    outright: an update that *could* move money is one that eventually does.

    An empty category clears the category. It does not create one named `""` —
    no category is ever invented from a row.
    """
    result = SyncResult()
    for change in changes:
        if not change.external_id:
            # A change with no identity never matched a live row. Refusing it
            # is the point: the alternative is a write to a guessed address.
            result.failed += 1
            result.failures.append(("", "no ledger identity — not matched"))
            continue

        fields: dict = {"description": change.new_description}
        if change.category_changed:
            fields["category"] = change.new_category
        if change.counterparty_changed:
            fields["counterparty"] = change.new_counterparty
        if change.tags_changed:
            # The whole list, not a delta: tags are replaced rather than
            # appended, so anything omitted here is removed. `new_tags` is
            # built to carry the row's unmanaged tags through.
            fields["tags"] = list(change.new_tags)

        try:
            store.update_transaction(change.external_id, fields)
        except LedgerError as exc:
            result.failed += 1
            result.failures.append((change.external_id, str(exc)))
            log.warning("in-place update failed for %s: %s", change.external_id, exc)
        else:
            result.updated += 1
        if on_progress:
            on_progress(result)
    return result


# --- the ledger, aggregated --------------------------------------------------
# SPEC §18. One definition of what counts as spend and what counts as earnings,
# used by every figure and every chart. There was no such definition before this
# phase: §8 and §8.1 established the semantics in the *rules*, and every reader
# of the totals had to remember to apply them.
#
# Getting this wrong is not a rounding error. Measured on one real three-month
# ledger, the naive by-type reading was **three times** the true spend and
# **1.6 times** the true earnings. A chart drawn on the naive numbers is not
# roughly right, it is three times wrong, and it looks entirely plausible.

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
    # Every rupee in minus every rupee out, exclusions and all — which is the
    # change in the balance over the window and the only figure that deserves
    # the word "net". `income - spend` is NOT that: both sides already have
    # §8/§8.1's movement taken out, so their difference counts nothing that
    # moved. Measured on one window: `income - spend` read more than three
    # times what the balance actually moved, under a card captioned "earned
    # less spent, over this window" — true of the arithmetic, and read by a
    # person as "what I kept".
    net: Decimal
    withdrawals: int
    deposits: int
    categories: list[Slice]       # real spend, largest first
    payees: list[Slice]           # real spend by counterparty, largest first
    sources: list[Slice]          # EVERY deposit by counterparty; sums to gross_income
    # The ledger's Category, Double and Tag reports, which are all the same
    # question asked three ways: within one thing, what were the others? §64.
    payees_by_category: list["Breakdown"]   # a category -> who you paid
    categories_by_payee: list["Breakdown"]  # a payee -> what it was for
    categories_by_tag: list["Breakdown"]    # a tag -> which categories
    # The income side. Reports covered only spending, and "Salary or any other
    # In out entites" is half the ledger — a report screen that can only answer
    # about money leaving is answering half the question. §68.
    sources_by_category: list["Breakdown"]  # an income category -> who paid you
    categories_by_source: list["Breakdown"] # a source -> what it was booked as
    # Five-number summaries for a box plot. §74.
    spread: list["Spread"]
    excluded_spend: list[Slice]   # what `not_spend` kept out, largest first
    excluded_income: Slice        # what the not-earnings tag kept out
    refunds: Slice                # reversals: deposits that undo a spend
    rollups: list[RollUp]
    months: list[MonthTotals]
    hours: list[int]              # 24 buckets, real spend only
    # 7 buckets, Monday first — `date.weekday()`'s own numbering. §76.
    weekdays: list[int]
    weekday_spend: list[Decimal]
    clocked: int                  # spend rows with a clock — sums `hours`
    counted: int                  # spend rows in total. NOT the same number.
    uncategorised: Slice
    not_spend: list[str]
    # One row per category in `categories`, same order, each carrying that
    # category's real spend in every month of `months`. §57.
    category_months: list["CategorySeries"]


@dataclass(frozen=True)
class Spread:
    """A category's transaction sizes, as a five-number summary. SPEC §74.

    The question a bar chart cannot answer: is this category one big payment or
    forty small ones? Two categories with the same total look identical on
    every other chart here.

    **Only computed where it means something.** `MIN_FOR_SPREAD` rows are
    required, because a quartile over three transactions is arithmetic
    pretending to be a statistic — and a box plot is unusually good at looking
    authoritative. Categories below the floor are named instead, not drawn.
    """

    name: str
    count: int
    low: Decimal
    q1: Decimal
    median: Decimal
    q3: Decimal
    high: Decimal


# Below this a five-number summary is noise wearing a chart's clothes.
MIN_FOR_SPREAD = 5


def _quantile(ordered: list[Decimal], q: float) -> Decimal:
    """Linear interpolation between order statistics (the R-7 / numpy default).

    Written out rather than pulled in: `statistics.quantiles` works on floats,
    and every value here is money. Interpolating between two Decimals with a
    Decimal weight keeps it exact.
    """
    if not ordered:
        return Decimal(0)
    if len(ordered) == 1:
        return ordered[0]
    pos = Decimal(str(q)) * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return (ordered[lo] + (ordered[hi] - ordered[lo]) * frac).quantize(_CENT)


@dataclass(frozen=True)
class Breakdown:
    """One thing, and what it is made of. SPEC §64.

    Most tools ship this as three separate report screens — Category, payee
    (expense/revenue account) and Tag — with a controller each. They are one
    shape: a named total, and the slices of some *other* dimension inside it.
    Computing them here means all three carry §8/§8.1's exclusions, which
    the previous store's versions do not.
    """

    name: str
    amount: Decimal
    count: int
    parts: list[Slice]


@dataclass(frozen=True)
class CategorySeries:
    """One category's spend across the months of the analysis. SPEC §57.

    Aligned with `LedgerAnalysis.months` **by position** and padded with zeros,
    so a month a category never appears in is a gap in the line rather than a
    missing point that shifts every later one left.
    """

    name: str
    amounts: list[Decimal]
    # The row total, carried rather than left to the client to add up. Summing
    # `amounts` in JavaScript would put money through a float on its way to
    # being displayed, which §16.1 forbids — and this figure already exists
    # exactly, as the matching entry in `categories`.
    total: Decimal


@dataclass(frozen=True)
class BalancePoint:
    day: str  # ISO date
    balance: Decimal


@dataclass(frozen=True)
class BalanceSeries:
    """One account's balance over time, from the bank's own running figure."""

    slug: str
    label: str
    points: list[BalancePoint]
    # The last balance recorded strictly BEFORE the window, with its real date.
    # Without it a windowed line starts at the first transaction of the window
    # and reads as the opening balance, which it is not.
    opening: BalancePoint | None


def balance_series(
    transactions: Iterable[Transaction],
    *,
    start: date | None = None,
    end: date | None = None,
) -> tuple[list[BalancePoint], BalancePoint | None]:
    """The balance path, one point per day: that day's closing figure. SPEC §57.

    **The balance is the bank's, never accumulated here.** Every statement row
    carries the running balance the bank printed (`Transaction.balance`), and
    §6.6's continuity check is what guarantees the chain closes — so reading the
    last figure of each day is exact. Adding up debits and credits to draw the
    same line would be a second implementation of the one invariant this project
    is built on, and it would agree with the check right up until it did not.

    **Pass one account's transactions**, already deduped — `account_transactions`
    does both, and deduping across accounts on `txn_id` is the data loss §21.1
    exists to prevent (non-negotiable 10).

    One point per *day*, not per row. Within a day the rows are ordered by
    `txn_id`, which is `YYYYMMDD` + a daily sequence and therefore sheet order
    for a bank that numbers its rows. For a bank whose ids are derived (§44.4)
    that intraday order is arbitrary, so the day's figure is *a* balance recorded
    that day rather than provably the last one — which is why this is a day
    resolution chart and not an intraday one.
    """
    ordered = sorted(transactions, key=lambda t: (t.txn_date, t.txn_id))

    closing: dict[date, Decimal] = {}
    for txn in ordered:
        closing[txn.txn_date] = txn.balance  # later row on the same day wins

    opening: BalancePoint | None = None
    points: list[BalancePoint] = []
    for day in sorted(closing):
        point = BalancePoint(day.isoformat(), closing[day])
        if start and day < start:
            opening = point  # keeps advancing; the last one before the window
            continue
        if end and day > end:
            continue
        points.append(point)
    return points, opening


def _split_amount(split: dict) -> Decimal:
    """The row's amount, to the paisa. Decimal, never float (non-negotiable 1).

    `NUMERIC` already reads back as `Decimal`, so this is a quantize rather
    than a parse — but it still goes through `str`, because a row can reach
    here from a test fixture or from an archive rebuild as well as from the
    database.
    """
    return Decimal(str(split.get("amount") or "0")).quantize(_CENT)


def _split_day(split: dict) -> date | None:
    """The row's date. A `date`, and `None` when there isn't one.

    It used to arrive as a timestamp string that had to be sliced to ten
    characters at four separate call sites. The column is a `DATE` now, so the
    slicing is gone and a malformed value cannot quietly become a valid date
    with the wrong day in it.
    """
    value = split.get("txn_date")
    if isinstance(value, date):
        return value
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def _within_coverage(month: str, coverage: tuple[date, date] | None) -> bool:
    """Does the analysis window reach into this `YYYY-MM` at all? §73."""
    if coverage is None:
        return True
    year, number = int(month[:4]), int(month[5:7])
    first = date(year, number, 1)
    last = date(year, number, calendar.monthrange(year, number)[1])
    return coverage[0] <= last and coverage[1] >= first


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


#: The category the settlement shift exists for, used **only** to populate the
#: picker before anything is configured. SPEC §103.
#:
#: Without it the feature could never be switched on from the UI: the list of
#: rows you can split is filtered by `Attribution.categories`, which is empty
#: until `config/attribution.yaml` exists, which is written by splitting a row.
#: So the picker offers this one, says it is not configured yet, and the first
#: split writes the file — with this same list in it, which is why the constant
#: is here rather than spelled out twice.
#:
#: It is a default and not an inference: the operator's file wins the moment
#: there is one, and configuring nothing still shifts nothing.
DEFAULT_SETTLEMENT_CATEGORIES = ("Credit Card",)


@dataclass(frozen=True)
class Attribution:
    """When a payment settles spending from another month. SPEC §73.

    > "I pay Credit Card bill in first 10days of the month but it is of
    >  previous month ... in Aug currently [the bill] in this [part of it]
    >  should be of Aug only I pay for that"

    A card bill paid on the 5th of August settles July's purchases. Bucketed on
    its own date it puts July's spending in August, and every month chart is
    then wrong by a bill.

    **This moves a row's REPORTING month and nothing else.** Not its date in
    The ledger — non-negotiable 14 forbids an update touching `date`, and the
    balance line is the bank's own running figure on the bank's own days, which
    must keep matching the statement. Not the window filter either: a row is
    still in scope on the day the money actually left, because that is the day
    the balance moved. Only the month buckets and the category grid move.

    `keep` is the exception the operator asked for: part of a bill can be this
    month's own spending, and that part stays where it is. It is keyed on the
    namespaced `external_id`, never the bare `txn_id` (non-negotiable 10).
    """

    # Categories whose rows settle the previous month.
    categories: frozenset[str] = frozenset()
    # Only rows paid on or before this day of the month are shifted — a payment
    # made late in the month is this month's, not a settlement of the last.
    before_day: int = 10
    # Which day of the previous month to attribute to.
    to_day: int = 22
    # external_id -> the amount that is THIS month's own spending and stays.
    keep: dict[str, Decimal] = field(default_factory=dict)

    def split(self, external_id: str, category: str, day: date, amount: Decimal):
        """`(this_month_part, previous_month_part, shifted_month)`.

        Returns the whole amount as `this_month_part` and no shift when the row
        is not a settlement — which is every row for an operator who configures
        nothing, so the default is exactly today's behaviour.

        **§94: the DAY is always `to_day`, only the month moves.**

        > "always put credit card on 22 of the month whether current or
        >  previous"

        A bill is not spent on the day it is paid; it is the month's card
        activity, and the operator's mental anchor for that is the statement
        date. So the settled part lands on the 22nd of the previous month and
        the kept part on the 22nd of *this* one — not on the payment date,
        which is an artefact of when they happened to sit down and pay.

        This only affects which MONTH BUCKET each part falls in, and at day
        resolution the two land in the same buckets they would have anyway. It
        is recorded because it is what was asked for and because it becomes
        load-bearing the moment anything here reports by week or by day.
        """
        if category not in self.categories or day.day > self.before_day:
            return amount, Decimal(0), None
        kept = self.keep.get(external_id, Decimal(0))
        if kept < 0:
            kept = Decimal(0)
        if kept > amount:
            # A `keep` bigger than the bill is a config error, not a licence to
            # invent money. Clamp and keep the total intact.
            kept = amount
        moved = amount - kept
        if moved == 0:
            return amount, Decimal(0), None
        first = day.replace(day=1)
        previous = first - timedelta(days=1)
        target = previous.replace(day=min(self.to_day, calendar.monthrange(previous.year, previous.month)[1]))
        return kept, moved, target.strftime("%Y-%m")

    def anchor(self, category: str, day: date) -> date:
        """Where a settlement's KEPT part is dated: `to_day` of its own month.

        Separate from `split` because it answers a different question — which
        day, not which month — and only the day-resolution views need it.
        """
        if category not in self.categories or day.day > self.before_day:
            return day
        last = calendar.monthrange(day.year, day.month)[1]
        return day.replace(day=min(self.to_day, last))


def ledger_analysis(
    splits: Iterable[dict],
    *,
    times: dict[str, time | None] | None = None,
    coverage: tuple[date, date] | None = None,
    rules: dict | None = None,
    attribution: "Attribution | None" = None,
) -> LedgerAnalysis:
    """Aggregate the previous store's splits under §8/§8.1's exclusions.

    **the ledger is the source for money and category, the statement for the
    clock.** The category is assigned by the rules engine at store time (D5), so
    reading it back from the ledger is the only way to report it without
    re-implementing categorisation. `txn_time` is parser-derived and is never
    pushed, so it exists only in the statement — hence `times`, keyed on
    `external_id`, which is the bank's transaction id (§6.1).

    A pure function over data: no HTTP, no file reads. The tests feed it splits.
    """
    rules = rules if rules is not None else load_rules()
    attribution = attribution or Attribution()
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
    # the previous store's "expense/revenue account" report, computed here rather than
    # from the archive so it carries §8/§8.1's exclusions like every other
    # figure on the page (non-negotiable 9). The counterparty is the name
    # `build_payload` pushed, which is the alias the operator chose.
    by_payee: dict[str, list[Decimal]] = {}
    by_source: dict[str, list[Decimal]] = {}
    by_category_month: dict[tuple[str, str], Decimal] = {}
    # The three drill-downs, accumulated in the one pass that already has the
    # exclusions applied — a second pass would be a second implementation of
    # §8/§8.1 and would drift from it.
    cross_cat_payee: dict[tuple[str, str], list] = {}
    cross_tag_cat: dict[tuple[str, str], list] = {}
    cross_cat_source: dict[tuple[str, str], list] = {}
    amounts_by_category: dict[str, list[Decimal]] = {}
    months: dict[str, list[Decimal]] = {}
    hours = [0] * 24
    # The hour histogram needs a clock, which only 81 of 84 rows carry. A
    # weekday needs only the DATE, which every row has — so this covers the
    # whole ledger where the hour chart covers most of it, and the two are
    # counted separately for that reason. §76.
    weekdays = [0] * 7
    weekday_spend = [Decimal(0)] * 7
    clocked = counted = 0

    for split in splits:
        kind = split.get("kind")
        amount = _split_amount(split)
        day = _split_day(split)
        month = day.isoformat()[:7] if day else ""
        tags = split.get("tags") or []

        if kind == "withdrawal":
            withdrawals += 1
            gross_spend += amount
            category = split.get("category") or "(no category)"
            if category in not_spend:
                bucket = excluded.setdefault(category, [Decimal(0), 0])
                bucket[0] += amount
                bucket[1] += 1
                continue

            spend += amount
            bucket = by_category.setdefault(category, [Decimal(0), 0])
            bucket[0] += amount
            bucket[1] += 1

            # §73. Where does this row's spending BELONG, as opposed to where
            # the money moved? For everything but a configured settlement those
            # are the same day, `here` is the whole amount and `shifted` is
            # None — so an operator who configures nothing sees no change.
            here, moved, shifted = attribution.split(
                str(split.get("external_id") or ""),
                category,
                day or date.min,
                amount,
            )

            if month and here:
                # Real spend only, so this sums to the category bar beside it.
                # An excluded category `continue`s above and never reaches here.
                by_category_month[(category, month)] = (
                    by_category_month.get((category, month), Decimal(0)) + here
                )
            if shifted and moved:
                by_category_month[(category, shifted)] = (
                    by_category_month.get((category, shifted), Decimal(0)) + moved
                )
            amounts_by_category.setdefault(category, []).append(amount)

            payee = _counterparty(split) or "(unnamed)"
            slot = by_payee.setdefault(payee, [Decimal(0), 0])
            slot[0] += amount
            slot[1] += 1

            pair = cross_cat_payee.setdefault((category, payee), [Decimal(0), 0])
            pair[0] += amount
            pair[1] += 1
            for tag in tags:
                if tag in rollup_members:
                    cell = cross_tag_cat.setdefault((str(tag), category), [Decimal(0), 0])
                    cell[0] += amount
                    cell[1] += 1
            for tag in tags:
                if tag in rollup_members:
                    slot = by_tag.setdefault(tag, [Decimal(0), 0])
                    slot[0] += amount
                    slot[1] += 1
            # Same split as above: the month buckets and the category grid have
            # to agree, so they read one decision rather than making two.
            if month and here:
                months.setdefault(month, [Decimal(0), Decimal(0)])[0] += here
            if shifted and moved:
                # **Only into a month the window already covers.** `/analysis`
                # filters splits to the window and only then calls this, so a
                # shift that mints its own bucket grows a column for a month
                # the range picker says is excluded — seeded by one settled
                # bill and nothing else. When the target is outside, the money
                # stays where it was paid: visibly in the wrong month beats
                # invisibly in a month you did not ask for, and the total is
                # unchanged either way.
                if shifted in months or _within_coverage(shifted, coverage):
                    months.setdefault(shifted, [Decimal(0), Decimal(0)])[0] += moved
                else:
                    months.setdefault(month, [Decimal(0), Decimal(0)])[0] += moved
                    here, moved, shifted = here + moved, Decimal(0), None

            counted += 1
            if day:
                wd = day.weekday()
                weekdays[wd] += 1
                weekday_spend[wd] += amount
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
            # **Who paid you is asked of EVERY deposit, before the earnings
            # test.** §72: this used to sit after the `continue` below, so a
            # deposit tagged `not-earnings` never reached it — and the operator
            # noticed exactly the right thing, that money from their mother was
            # missing from a report headed "inside each source, what the money
            # was booked as". "Who paid you" and "what did you earn" are two
            # questions and only the second one excludes.
            #
            # So `sources` sums to `gross_income`, not to `income`. The tile
            # beside the earnings figure says which it is, and a test pins it.
            source = _counterparty(split) or "(unnamed)"
            slot = by_source.setdefault(source, [Decimal(0), 0])
            slot[0] += amount
            slot[1] += 1
            in_cat = split.get("category") or "(no category)"
            cell = cross_cat_source.setdefault((str(in_cat), source), [Decimal(0), 0])
            cell[0] += amount
            cell[1] += 1

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

    ordered_months = sorted(months)

    def breakdowns(cross: dict[tuple[str, str], list], outer_first: bool) -> list[Breakdown]:
        """Group a two-key accumulator into `outer -> [inner slices]`.

        `outer_first` says which half of the key is the thing being broken
        down: the category->payee map is keyed (category, payee) and the
        payee->category view is the SAME map read the other way round, so it is
        built once and inverted rather than accumulated twice.
        """
        grouped: dict[str, dict[str, list]] = {}
        for (a, b), (total, n) in cross.items():
            outer, inner = (a, b) if outer_first else (b, a)
            slot = grouped.setdefault(outer, {}).setdefault(inner, [Decimal(0), 0])
            slot[0] += total
            slot[1] += int(n)
        out = [
            Breakdown(
                name=outer,
                amount=sum((v[0] for v in inners.values()), Decimal(0)),
                count=sum(int(v[1]) for v in inners.values()),
                parts=sorted(
                    (Slice(k, v[0], int(v[1])) for k, v in inners.items()),
                    key=lambda s: (-s.amount, s.name),
                ),
            )
            for outer, inners in grouped.items()
        ]
        return sorted(out, key=lambda b: (-b.amount, b.name))

    return LedgerAnalysis(
        gross_spend=gross_spend,
        spend=spend,
        gross_income=gross_income,
        income=income,
        net=gross_income - gross_spend,
        withdrawals=withdrawals,
        deposits=deposits,
        categories=slices(by_category),
        payees=slices(by_payee),
        sources=slices(by_source),
        payees_by_category=breakdowns(cross_cat_payee, outer_first=True),
        categories_by_payee=breakdowns(cross_cat_payee, outer_first=False),
        categories_by_tag=breakdowns(cross_tag_cat, outer_first=True),
        sources_by_category=breakdowns(cross_cat_source, outer_first=True),
        categories_by_source=breakdowns(cross_cat_source, outer_first=False),
        spread=sorted(
            (
                Spread(
                    name=name,
                    count=len(values),
                    low=min(values),
                    q1=_quantile(sorted(values), 0.25),
                    median=_quantile(sorted(values), 0.5),
                    q3=_quantile(sorted(values), 0.75),
                    high=max(values),
                )
                for name, values in amounts_by_category.items()
                if len(values) >= MIN_FOR_SPREAD
            ),
            key=lambda s: (-s.median, s.name),
        ),
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
            MonthTotals(month, months[month][0], months[month][1], _month_partial(month, coverage))
            for month in ordered_months
        ],
        category_months=[
            CategorySeries(
                name=s.name,
                amounts=[by_category_month.get((s.name, m), Decimal(0)) for m in ordered_months],
                total=s.amount,
            )
            for s in slices(by_category)
        ],
        hours=hours,
        weekdays=weekdays,
        weekday_spend=weekday_spend,
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

# Identity lives in `identity.py` so that `push` can import it —
# `service` imports the pusher, so the pusher cannot import `service`. The
# names are re-exported here because every caller already spells them
# `service.txn_id_of` (§21.1, §119).
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
        label=default_label(meta, bank),
    )
    accounts.append(account)
    save_accounts(accounts)
    log.warning("registered account %s (%s) -> %r", account.slug, account.masked, account.asset_account)
    return account


def default_label(meta: StatementMeta, bank: str) -> str:
    """The name to put on this account's tab. SPEC §100.

    `account_name` is read off the statement's preamble, and it is the one
    metadata field with no floor under it — the account number routes every row
    and is refused when absent (§21.7), while this one only names a button. So
    it is allowed to be wrong, and on two of three real banks it is:

        Canara   a real name
        Union    `Address :`
        SBI      nothing at all

    Union's preamble puts `Name` and `Address` in one line, so `_find_metadata`'s
    prefix strategy takes the label as the value; SBI prints no name. Both gave
    the operator a chip they could not read — one blank, one saying `Address :`.

    **A trailing colon is the tell**, and it is deliberately not stripped in
    `_find_metadata`: it is the evidence that a label was read where a value
    should be, and losing it there would only make `Address` look like a name.

    The fallback names the account the way the bank does, which is always true
    and never mysterious. The operator can rename it from Accounts either way —
    this is a default, not a decision.
    """
    name = meta.account_name.strip()
    if name.endswith(":") or not any(character.isalpha() for character in name):
        name = ""
    return name[:40] or f"{bank.title()} ****{meta.account_number.strip()[-4:]}"


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
# purge and a re-push that stopped after 21 of 93 rows left the ledger holding a
# self-consistent balance, and 349 tests, `doctor`, `make check` and the status
# strip all passed while the ledger was a third of itself (§19).
#
# So: compare the ledger against the statements that built it. This is the one
# check that catches that corruption whatever caused it — an interrupted purge, a
# hand-deleted row in its own UI, a restore of the wrong dump.


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
    store: LedgerStore,
    account: "Account | Settings",
    archive: Path = Path("archive"),
) -> LedgerVerdict:
    """Assert the live ledger still matches the statements that built it.

    **Reads the ledger, not a view of it.** This function's entire job is to say
    what the ledger holds right now, and a check that compares `archive/`
    against something cached has not checked the ledger. Non-negotiable 11: a
    green tick for something you did not check is a lie.
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

    # Every figure below is read from the ledger here and now. Non-negotiable
    # 11: a check that reads a cache has not checked.
    balance: Decimal | None = None
    opening: Decimal | None = None
    opening_on = None
    found = False
    for live_account in store.asset_accounts():
        if live_account["name"] == account.asset_account:
            found = True
            balance = Decimal(str(live_account["current_balance"])).quantize(_CENT)
            opening = Decimal(str(live_account["opening_balance"])).quantize(_CENT)
            opening_on = live_account.get("opening_on")

    if not found:
        return LedgerVerdict([
            Check(
                "account",
                False,
                f"no asset account named {account.asset_account!r} — "
                "nothing can be verified against it",
            )
        ])

    splits = store.account_transactions(account.asset_account)
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
                    else f"the ledger says {balance}, {newest.path.name} closes at "
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
        # **Count, do not compare sets.** §119: this check compared
        # `set(live) == set(archived)` and therefore could not see a row posted
        # twice — it reported "133 rows, one per archived transaction", in
        # green, while the ledger held 141 splits behind those 133 identities
        # and the balance was out by the extra copies. A set says which
        # identities are present; only a count says how many times. That is
        # non-negotiable 11 in its most literal form.
        seen = Counter(txn_id_of(external) for external in raw_ids)
        duplicated = sorted(i for i, times in seen.items() if times > 1)
        extra = sum(times - 1 for times in seen.values() if times > 1)
        if not missing and not unexpected and not duplicated:
            checks.append(
                Check(
                    "rows",
                    True,
                    f"{len(raw_ids)} rows, one per archived transaction",
                )
            )
        else:
            parts = []
            if missing:
                sample = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
                parts.append(f"{len(missing)} archived row(s) MISSING from the ledger ({sample})")
            if unexpected:
                sample = ", ".join(unexpected[:5]) + (" …" if len(unexpected) > 5 else "")
                parts.append(f"{len(unexpected)} row(s) in the ledger with no statement ({sample})")
            if duplicated:
                sample = ", ".join(duplicated[:5]) + (" …" if len(duplicated) > 5 else "")
                parts.append(
                    f"{len(duplicated)} transaction(s) are in the ledger MORE THAN "
                    f"ONCE ({sample}) — {extra} extra row(s), and every figure drawn "
                    "from this account counts them. **This should not be reachable**: "
                    "`external_id` is the primary key, so the only way two rows share "
                    "an identity is two different namespaces mapping to one bank id — "
                    "see the id namespace check below, which names the account they "
                    "came from. This check will not repair it (non-negotiable 12)"
                )
            checks.append(
                Check("rows", False, f"{len(raw_ids)} live vs {len(expected_ids)} archived — "
                                     + "; ".join(parts))
            )

    # 3. no row is in the ledger twice ---------------------------------------
    # Kept as a check even though the primary key makes it unwritable, because
    # a check that cannot fail is the cheapest possible evidence that the
    # guarantee is still the one being relied on. Two checks that used to live
    # here are gone and neither was a passbook concern:
    #
    #   * soft-deleted journals. The previous store deleted a row by hiding it,
    #     and a hidden row still refused a re-push of the same transaction as a
    #     duplicate. A delete is a delete here.
    #   * an unfinished purge. Removing an account's rows was thousands of
    #     separate HTTP deletes that could die halfway, so the intent was
    #     written to a file first and resumed. It is one statement in one
    #     transaction now: it happens or it does not.

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
    #
    # A column on the account rather than a row in the ledger, and that change
    # closes a whole class of failure: it used to be a transaction with no
    # `external_id`, which is what kept `purge` from deleting it — a structural
    # exclusion, but one that depended on a row staying shaped a certain way.
    # A column cannot be purged, cannot be duplicated, and cannot acquire an
    # id. What is left to check is whether it was ever set.
    earliest = min(statements, key=lambda s: s.meta.period_from) if statements else None
    expected_opening = earliest.meta.opening_balance if earliest else None
    if opening is None:
        checks.append(Check("opening balance", None, "the account could not be read"))
    elif opening == 0 and expected_opening not in (None, Decimal(0)):
        checks.append(
            Check(
                "opening balance",
                False,
                "zero, but the earliest statement opens at "
                f"{expected_opening} — every figure on this account is short by "
                "that amount, and the balance cannot equal the bank's",
            )
        )
    else:
        when = f" on {opening_on}" if opening_on else ""
        checks.append(Check("opening balance", True, f"{opening}{when}"))

    return LedgerVerdict(checks)


# --- statements on disk ------------------------------------------------------


#: Parsed archives, keyed on what the directory looked like. SPEC §101.
#:
#: **The key is every file's path, size and mtime**, so this is not a guess with
#: a timeout on it: a file that changes changes the key, and a file that appears
#: or disappears changes it too. Nothing has to remember to invalidate this, and
#: nothing can serve a statement that is no longer on disk.
#:
#: It exists because it was measured. `archived_statements` re-parses every
#: `.xls` and `.pdf` under `archive/` on **every call**, and it is called by
#: `/overview`, `/analysis`, `/transactions`, `/status`, `verify-ledger` and the
#: reminder — 171ms for three statements, several times per page load, growing
#: linearly with every week the operator downloads. §6k forbids a cache without
#: a measurement demanding one; this is the measurement.
_ARCHIVE_CACHE: dict[tuple, list["ParsedStatement"]] = {}

#: How many directory states to remember. Small on purpose: the useful entry is
#: almost always the current one, and the only other states worth holding are
#: the ones either side of a sync.
_ARCHIVE_CACHE_MAX = 4


def _archive_key(paths: list[Path]) -> tuple:
    out = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            # Vanished between the listing and the stat. Fold the miss into the
            # key rather than raising: the next call will see the new listing.
            out.append((str(path), -1, -1.0))
            continue
        out.append((str(path), stat.st_size, stat.st_mtime))
    return tuple(out)


def archived_statements(archive: Path = Path("archive")) -> list[ParsedStatement]:
    """Every archived statement, parsed. A bad file is skipped, never fatal.

    Statements overlap by design (a weekly download re-covers earlier weeks), so
    callers dedupe on `txn_id` — the bank's own key (§6.1).

    Cached on the directory's own contents — see `_ARCHIVE_CACHE`. The returned
    list is shared, so **callers must not mutate it**; every one of them either
    reads it or builds something new from it, and `dedupe_transactions` already
    copies (§57).
    """
    if not archive.is_dir():
        return []
    paths = sorted(p for p in archive.rglob("*") if p.is_file() and not p.name.startswith("."))
    key = (str(archive), _archive_key(paths))
    hit = _ARCHIVE_CACHE.get(key)
    if hit is not None:
        return hit

    out = []
    for path in paths:
        try:
            out.append(parse_statement(path))
        except Exception as exc:  # one unreadable archive must not blank a page
            log.warning("skipping %s: %s", path.name, exc)
            continue

    if len(_ARCHIVE_CACHE) >= _ARCHIVE_CACHE_MAX:
        # Oldest first: dicts keep insertion order, so this is a plain FIFO and
        # not an LRU. A cache of four does not need the bookkeeping.
        del _ARCHIVE_CACHE[next(iter(_ARCHIVE_CACHE))]
    _ARCHIVE_CACHE[key] = out
    log.info("parsed %d archived statement(s) (%d cached states)", len(out), len(_ARCHIVE_CACHE))
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
    return dedupe_transactions(
        statements_for(account, [*archived_statements(archive), *extra])
    )


def dedupe_transactions(statements: Iterable[ParsedStatement]) -> list[Transaction]:
    """Every transaction in these statements, deduped on the bank's id.

    **Pass one account's statements**, the same requirement `transaction_times`
    carries and for the same reason: the key is the bank's id, which is unique
    only within an account (§21.1). Split out of `account_transactions` so a
    caller that has already parsed the archive — `/analysis` parses it for the
    Day Rail — does not parse it a second time to get the same rows, and so
    there is still only one implementation of the dedupe.
    """
    seen: dict[str, Transaction] = {}
    for statement in statements:
        for txn in statement.transactions:
            seen.setdefault(txn.txn_id, txn)
    return list(seen.values())


def archived_transactions(
    accounts: "list[Account]",
    archive: Path = Path("archive"),
    start: date | None = None,
    end: date | None = None,
) -> list[Transaction]:
    """The deduped rows for these accounts, from the index. SPEC §114.

    The same answer `dedupe_transactions` gives over `statements_for`, and a
    test asserts that row for row on the real archive — but read out of SQLite,
    so the cost is the rows in the window rather than every row ever archived.

    **Deduped per account and then concatenated** (§21.1). The bank sequences
    `txn_id` per account, so a dedupe across accounts is the silent data loss
    this rule exists to prevent: two accounts, 186 rows, 93 survive, no error.
    The index partitions on `(account, txn_id)` for exactly that reason.

    Falls back to reading the files if the index cannot be opened. A cache that
    can take the app down is worse than no cache, and this one is a view over
    something that is still there.
    """
    numbers = [a.account_number.strip() for a in accounts]
    if not numbers:
        return []
    try:
        with index_mod.connect() as conn:
            index_mod.sync(archive, conn, parse_statement)
            return index_mod.transactions(conn, numbers, start, end)
    except Exception as exc:
        log.warning("index unavailable, reading the archive directly: %s", exc)
        statements = archived_statements(archive)
        out: list[Transaction] = []
        for account in accounts:
            out.extend(dedupe_transactions(statements_for(account, statements)))
        return out


def archived_coverage(
    accounts: "list[Account]", archive: Path = Path("archive")
) -> tuple[date, date] | None:
    """The window these accounts' statements cover, from the index. §114.2.

    `statement_coverage` over `statements_for` gives the same answer and has to
    load every statement to do it. This reads two columns.
    """
    numbers = [a.account_number.strip() for a in accounts]
    if not numbers:
        return None
    try:
        with index_mod.connect() as conn:
            index_mod.sync(archive, conn, parse_statement)
            return index_mod.coverage(conn, numbers)
    except Exception as exc:
        log.warning("index unavailable, reading the archive directly: %s", exc)
        statements = archived_statements(archive)
        spans = [
            span
            for account in accounts
            if (span := statement_coverage(statements_for(account, statements)))
        ]
        if not spans:
            return None
        return min(s[0] for s in spans), max(s[1] for s in spans)


def sync_history(
    archive: Path = Path("archive"),
    limit: int = 10,
    accounts: "list[Account] | None" = None,
) -> list[dict]:
    """Recently archived statements, newest first. Filenames only, no contents.

    Scoped to `accounts` when given (§104): "Recently archived" listing another
    account's downloads is the same mistake as the sync warning, one column
    over.
    """
    if not archive.is_dir():
        return []
    files = [p for p in archive.rglob("*") if p.is_file() and not p.name.startswith(".")]
    only = scoped_paths(accounts, archive)
    if only is not None:
        files = [p for p in files if p in only]
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
