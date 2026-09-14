"""Build and post transaction payloads. SPEC §7.2.

**Verified against the running instance (v6.6.6), not from memory.** CLAUDE.md
requires this; the instance serves no OpenAPI document at any path, so the
shape was read from the code that actually validates the request:

  * `app/Api/V1/Requests/Models/Transaction/StoreRequest.php` — confirms the
    top-level keys (`error_if_duplicate_hash`, `apply_rules`, `fire_webhooks`,
    `group_title`, `transactions[]`) and every per-split field used below,
    including that `type` must be one of
    withdrawal/deposit/transfer/opening-balance/reconciliation, that `amount`
    must be a positive amount, and that `currency_code` must exist in
    `transaction_currencies.code`.
  * `app/Factory/TransactionJournalFactory.php:443` — throws
    `DuplicateTransactionException("Duplicate of transaction #N.")`.
  * `app/Api/V1/Controllers/Models/Transaction/StoreController.php:96` —
    converts that into a Laravel `ValidationException`, i.e. **HTTP 422**, with
    the message placed under `errors["transactions.0.description"]`.

The last point is the trap: a genuine validation failure lands under the *same*
key. An empty POST to the live instance returns 422 with
`errors["transactions.0.description"] = ["Need at least one transaction."]`.
So duplicates are detected by message, never by key.
"""

import logging
from dataclasses import dataclass, field
from ..identity import txn_id_of
from ..models import Transaction
from .client import DuplicateTransaction, FireflyClient, FireflyError

log = logging.getLogger(__name__)

CURRENCY = "INR"


def unknown_counterparty(txn: Transaction) -> str:
    """SPEC §7.2: `"Unknown (<channel>)"` when the narration yielded no payee."""
    return f"Unknown ({txn.channel})"


def build_payload(txn: Transaction, account: "Account | str") -> dict:
    """One transaction group with a single split. SPEC §7.2, §21.1.

    Takes an `Account` so the `external_id` can be namespaced by its slug. A bare
    string is still accepted — the DR drill and the single-account path predate
    the registry — and then the id stays the bank's own, un-namespaced. Reads
    tolerate both forms (`service.txn_id_of`); only writes are strict, so the
    migration in §21.2 can be run when it suits rather than being forced.
    """
    # display_payee applies config/payee_aliases.yaml; `notes` below still
    # carries the raw narration verbatim. SPEC D10.
    name = txn.display_payee
    counterparty = name or unknown_counterparty(txn)
    description = f"{name} ({txn.channel})" if name else unknown_counterparty(txn)

    asset_account = getattr(account, "asset_account", account)
    if txn.debit is not None:
        kind, amount = "withdrawal", txn.debit
        source, destination = asset_account, counterparty
    else:
        kind, amount = "deposit", txn.credit
        source, destination = counterparty, asset_account

    tags: list[str] = []
    if txn.is_reversal:
        # Reversals post as ordinary deposits — Firefly nets them correctly —
        # but are tagged so they can be excluded from spend analysis. SPEC §7.2.
        # This one is a parser-derived fact, not a classification, so it belongs
        # here rather than in a rule.
        tags.append("reversal")

    # `large-oneoff` is deliberately NOT set here. It is a classification, and
    # SPEC D5 puts classification in Firefly's rules engine. Tagging it
    # client-side also cannot honour §8's exclusions: the pusher has no idea
    # which category a row will land in, so it tagged the fund purchase and the
    # card payment — the exact two rows meant to be skipped.

    split = {
        "type": kind,
        "date": txn.txn_date.isoformat(),
        "amount": str(amount),  # positive string; Decimal never becomes a float
        "description": description,
        "source_name": source,
        "destination_name": destination,
        "currency_code": CURRENCY,
        "notes": txn.narration,  # raw narration, verbatim, always
        # `<slug>-<txn_id>`, because the bank's id is sequenced PER ACCOUNT and
        # therefore collides between two accounts at the same bank (§21.1).
        "external_id": (
            account.external_id(txn.txn_id) if hasattr(account, "external_id") else txn.txn_id
        ),
    }
    if tags:
        split["tags"] = tags

    return {
        "error_if_duplicate_hash": True,
        "apply_rules": True,
        "fire_webhooks": False,
        "transactions": [split],
    }


@dataclass
class PushResult:
    pushed: int = 0
    #: Rows not posted because this account's ledger already holds that
    #: `external_id`. The normal outcome on an overlapping download, and the
    #: number an operator reads as "skipped".
    already: int = 0
    #: Rows Firefly itself refused on its content hash. **Expected to be zero
    #: now**, because `already` catches an overlap first. A non-zero value here
    #: means two different identities carry byte-identical payloads — worth
    #: looking at, not worth failing over.
    duplicates: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def skipped(self) -> int:
        """Everything not posted because it was already there, by either route."""
        return self.already + self.duplicates

    @property
    def ok(self) -> bool:
        return self.failed == 0


def ledger_identities(client: FireflyClient, account) -> set[str]:
    """Every transaction identity this account's ledger already holds. §21.1.

    Read **fresh** (§101): a push that consults a cached view of the ledger has
    not consulted the ledger, and the whole point of the read is to decide
    whether posting would duplicate something.

    Keyed on the bank's own id via `txn_id_of`, so a row pushed before the
    namespace migration and the same row pushed after it are one identity, not
    two — exactly as `verify_ledger` compares them.

    An unknown asset account yields an empty set: there are no rows on an
    account that does not exist yet. Any other read failure is raised, never
    swallowed. If the ledger cannot be read, whether a post would duplicate is
    unknown, and pushing anyway is the bug this function exists to prevent.
    """
    name = getattr(account, "asset_account", account)
    with client.fresh() as fresh:
        account_id = next(
            (a["id"] for a in fresh.asset_accounts() if a["attributes"]["name"] == name),
            None,
        )
        if account_id is None:
            return set()
        return {
            txn_id_of(str(split["external_id"]))
            for group in fresh.account_transactions(account_id)
            for split in group["attributes"]["transactions"]
            if split.get("external_id")
        }


def push_transactions(
    client: FireflyClient,
    transactions: list[Transaction],
    account,
    *,
    on_progress=None,
) -> PushResult:
    """Post each transaction that is not already in the ledger. SPEC §7.2, §119.

    `account` is an `Account` (so ids are namespaced, §21.1) or a bare asset
    account name for the pre-registry path.

    **The overlap is skipped here, by identity — not by Firefly, on content.**
    Weekly downloads overlap by design, so most of a statement is usually
    already posted, and for a long time the plan was to let Firefly reject the
    repeats: `error_if_duplicate_hash` is set below and stays set.

    That plan was wrong, and it cost a real ledger. Read the authority —
    `app/Factory/TransactionJournalFactory.php::hashArray()` on the pinned tag:

        unset($row['import_hash_v2'], $row['original_source']);
        $json = json_encode($row, JSON_THROW_ON_ERROR);
        $hash = hash('sha256', $json);

    It hashes the **submitted payload**, not the row's identity. So the check
    catches a byte-identical resubmission and nothing else — and passbook
    changes those bytes for a living. A new payee alias, a narration grammar, a
    tag, a rename: any of them rewrites the description of a row already in the
    ledger, the hash moves, and the repeat is accepted as a new transaction.
    Measured on the operator's Canara account after a config change and a
    re-upload of an overlapping period: 7 rows posted twice, 141 splits behind
    133 identities, the balance out by the sum of the extra copies. Nothing
    raised, and the ledger was self-consistent the whole time.

    **passbook owns `external_id` (§21.1), so passbook decides what is a
    repeat.** Firefly's hash stays on as a backstop for the case this cannot
    see — a row with no `external_id` at all — but it is no longer the thing
    being relied on.
    """
    result = PushResult()
    # One fresh read, before anything is posted. Raised, never swallowed: a
    # push that could not see the ledger must not guess.
    known = ledger_identities(client, account)
    for txn in transactions:
        if txn.txn_id in known:
            result.already += 1
            log.debug("already in ledger: %s", txn.txn_id)
            if on_progress:
                on_progress(result)
            continue
        payload = build_payload(txn, account)
        try:
            client.store_transaction(payload)
        except DuplicateTransaction:
            # Now an oddity rather than the norm: this row's identity was NOT
            # in the ledger, yet Firefly found a byte-identical payload. Count
            # it, do not fail — but `duplicates` staying at 0 is the expected
            # shape since §119.
            result.duplicates += 1
            log.debug("duplicate: %s", txn.txn_id)
        except FireflyError as exc:
            result.failed += 1
            result.failures.append((txn.txn_id, str(exc)))
            log.warning("push failed for %s: %s", txn.txn_id, exc)
        else:
            result.pushed += 1
            known.add(txn.txn_id)
        if on_progress:
            on_progress(result)
    return result
