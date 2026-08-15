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
    # which category a row will land in. Measured when it did: the two largest
    # withdrawals over the threshold were a mutual-fund purchase and a credit
    # card payment — the exact two rows the exclusions exist to skip.

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
    duplicates: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0


def push_transactions(
    client: FireflyClient,
    transactions: list[Transaction],
    account,
    *,
    on_progress=None,
) -> PushResult:
    """Post each transaction. Duplicates are counted, not raised. SPEC §7.2.

    `account` is an `Account` (so ids are namespaced, §21.1) or a bare asset
    account name for the pre-registry path.
    """
    result = PushResult()
    for txn in transactions:
        payload = build_payload(txn, account)
        try:
            client.store_transaction(payload)
        except DuplicateTransaction:
            # Expected on overlapping weekly downloads. That is what dedup is
            # for; do not treat it as an error.
            result.duplicates += 1
            log.debug("duplicate: %s", txn.txn_id)
        except FireflyError as exc:
            result.failed += 1
            result.failures.append((txn.txn_id, str(exc)))
            log.warning("push failed for %s: %s", txn.txn_id, exc)
        else:
            result.pushed += 1
        if on_progress:
            on_progress(result)
    return result
