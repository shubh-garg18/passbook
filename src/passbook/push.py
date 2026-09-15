"""Turn a parsed transaction into a ledger row, and write it. DECISIONS.md §36.

What this module used to do was build a request for another application and
post it over HTTP. It now builds a row and writes it to passbook's own store,
and three things that were awkward became straightforward:

**The category is decided here.** It used to be set by a rules engine that ran
inside the other application, after the row was accepted, which is why
`large_oneoff.exclude_categories` was documented as inert — the category did
not exist yet when the rule that excluded it ran. `rules.predict_category` runs
before the write, so the row is stored already carrying the category it should
have, and the exclusion works.

**A repeat cannot be written.** It used to be caught two ways: by an identity
pre-read here, and by the other application's hash of the submitted payload.
The second one was never the guard it appeared to be — passbook rewrites that
payload for a living, so any alias or tag change moved the hash and the repeat
went in. `external_id` is the primary key now. The pre-read below still runs,
because *skipping* a row that is already there is a different and better
outcome than *failing* to write it, and the weekly overlap makes that the
normal case.

**The counterparty is a column.** The other application modelled every row as a
transfer between two accounts, so the payee's name lived in whichever of
`source_name`/`destination_name` the direction did not use, and a helper had to
work out which. A statement is movements against one account; the far side is a
name, and a name is a column.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from .identity import txn_id_of
from .models import Transaction
from .rules import predict_category, predict_large_oneoff, predict_tags
from .store import LedgerError

log = logging.getLogger(__name__)

CURRENCY = "INR"


def unknown_counterparty(txn: Transaction) -> str:
    """`"Unknown (<channel>)"` when the narration yielded no payee."""
    return f"Unknown ({txn.channel})"


def build_split(
    txn: Transaction,
    account: "Account | str",
    *,
    rules: dict | None = None,
    threshold: Decimal | None = None,
) -> dict:
    """One ledger row.

    Takes an `Account` so the `external_id` can be namespaced by its slug. A
    bare string is still accepted — the DR drill and the single-account path
    predate the registry — and then the id stays the bank's own,
    un-namespaced. Reads tolerate both forms (`identity.txn_id_of`); only
    writes are strict.

    `rules` and `threshold` are optional so the reconciliation preview can ask
    what a row *would* look like without also deciding what it would be
    tagged. Omit them and the row carries no predicted category and no
    `large-oneoff` — never a guessed one.
    """
    # display_payee applies config/payee_aliases.yaml; `notes` below still
    # carries the raw narration verbatim.
    name = txn.display_payee
    counterparty = name or unknown_counterparty(txn)
    description = f"{name} ({txn.channel})" if name else unknown_counterparty(txn)

    asset_account = getattr(account, "asset_account", account)
    if txn.debit is not None:
        kind, amount = "withdrawal", txn.debit
    else:
        kind, amount = "deposit", txn.credit

    tags: set[str] = set()
    if txn.is_reversal:
        # Reversals post as ordinary deposits — the balance nets them
        # correctly — but are tagged so they can be excluded from spend
        # analysis. A parser-derived fact, not a classification, so it belongs
        # here rather than in a rule, and no config change can move it.
        tags.add("reversal")

    category = ""
    if rules is not None:
        category = predict_category(description, txn.narration, rules)
        tags |= predict_tags(description, txn.narration, kind, rules)
        if threshold is not None:
            big = predict_large_oneoff(
                kind, amount, description, txn.narration, category, threshold, rules
            )
            if big:
                tags.add(big)

    return {
        # `<slug>-<txn_id>`, because the bank's id is sequenced PER ACCOUNT and
        # therefore collides between two accounts at the same bank.
        "external_id": (
            account.external_id(txn.txn_id) if hasattr(account, "external_id") else txn.txn_id
        ),
        "account": asset_account,
        "kind": kind,
        "txn_date": txn.txn_date,
        "amount": amount,  # positive; direction is `kind`. Decimal, never a float.
        "description": description,
        "counterparty": counterparty,
        "category": category,
        "notes": txn.narration,  # raw narration, verbatim, always
        "txn_time": txn.txn_time,
        "currency": CURRENCY,
        "tags": sorted(tags),
    }


@dataclass
class PushResult:
    pushed: int = 0
    #: Rows not written because this account's ledger already holds that
    #: `external_id`. The normal outcome on an overlapping download, and the
    #: number an operator reads as "skipped".
    already: int = 0
    #: Rows the primary key refused that the pre-read did not predict.
    #: **Expected to be zero**: it means the ledger changed under the push.
    #: Worth looking at, not worth failing over.
    duplicates: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def skipped(self) -> int:
        """Everything not written because it was already there, by either route."""
        return self.already + self.duplicates

    @property
    def ok(self) -> bool:
        return self.failed == 0


def ledger_identities(store, account) -> set[str]:
    """Every transaction identity this account's ledger already holds.

    Keyed on the bank's own id via `txn_id_of`, so a row written before the
    namespace migration and the same row written after it are one identity, not
    two — exactly as `verify_ledger` compares them.

    An unknown asset account yields an empty set: there are no rows on an
    account that does not exist yet. **Any other read failure is raised, never
    swallowed.** If the ledger cannot be read, whether a write would duplicate
    is unknown, and writing anyway is the bug this function exists to prevent.
    """
    name = getattr(account, "asset_account", account)
    return {txn_id_of(str(i)) for i in store.identities(name) if i}


def push_transactions(
    store,
    transactions: list[Transaction],
    account,
    *,
    rules: dict | None = None,
    threshold: Decimal | None = None,
    on_progress=None,
) -> PushResult:
    """Write each transaction that is not already in the ledger.

    `account` is an `Account` (so ids are namespaced) or a bare asset account
    name for the pre-registry path.

    **The overlap is skipped here, by identity.** Weekly downloads overlap by
    design, so most of a statement is usually already stored. For a long while
    the plan was to let the ledger reject the repeats on a content hash of the
    submitted payload, and that plan cost a real ledger: passbook rewrites that
    payload whenever an alias, a grammar, a tag or a rename changes, so the hash
    moved and the repeat was accepted as a new transaction. Seven rows posted
    twice, and the ledger was self-consistent the whole time.

    passbook owns `external_id`, so passbook decides what is a repeat — and the
    store now agrees, because that column is its primary key.
    """
    result = PushResult()
    # One read, before anything is written. Raised, never swallowed: a push
    # that could not see the ledger must not guess.
    known = ledger_identities(store, account)
    for txn in transactions:
        if txn.txn_id in known:
            result.already += 1
            log.debug("already in ledger: %s", txn.txn_id)
            if on_progress:
                on_progress(result)
            continue
        split = build_split(txn, account, rules=rules, threshold=threshold)
        try:
            store.store_transaction(split)
        except LedgerError as exc:
            if "already in the ledger" in str(exc) or "duplicate key" in str(exc).lower():
                # The identity was not in the pre-read, yet the key refused it.
                # That means the ledger moved underneath this push — count it,
                # do not fail.
                result.duplicates += 1
                log.debug("duplicate: %s", txn.txn_id)
            else:
                result.failed += 1
                result.failures.append((txn.txn_id, str(exc)))
                log.warning("push failed for %s: %s", txn.txn_id, exc)
        else:
            result.pushed += 1
            known.add(txn.txn_id)
        if on_progress:
            on_progress(result)
    return result
