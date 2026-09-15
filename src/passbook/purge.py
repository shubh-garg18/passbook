"""Remove previously-written transactions from one asset account.

**Rarely the right tool.** A re-push skips rows already in the ledger by
`external_id`, so purging to make one "take" destroys a good ledger for
nothing; `passbook resync` applies config to existing rows in place. What
remains is the case where an account is being removed outright.

**The `external_id` is the safety mechanism, not a date range.** Every row this
tool wrote carries the bank's transaction id there; nothing else does. An
account's opening balance is a column on the account rather than a row, so it
is excluded structurally rather than by a guard that could be got wrong.

Three things this module used to carry are gone, and each is worth naming,
because each was a property of the store rather than of passbook:

* **The intent file.** Removing an account's rows was thousands of separate
  HTTP deletes that could die halfway, leaving a coherent-looking ledger that
  was missing an arbitrary prefix of a statement. The intent was written before
  the first delete so a resume could finish the job. It is one statement in one
  transaction now — it happens or it does not — so there is no half-finished
  state to record, resume, or check for.
* **The force-delete afterwards.** The old store soft-deleted, and its
  duplicate check searched trashed rows, so without a second pass a later push
  of identical content was rejected as a duplicate of a transaction that no
  longer visibly existed. Observed: after deleting 93 rows and re-pushing, only
  the 41 whose description had changed got through. A delete is a delete here.
* **`find_duplicates`.** The repair for an incident where a row was written
  twice. `external_id` is the primary key, so the state it repaired cannot be
  reached.

Nothing here runs without an explicit confirmation from the CLI layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from .store import LedgerError

log = logging.getLogger(__name__)

_CENT = Decimal("0.01")


@dataclass
class Candidate:
    external_id: str
    date: str
    description: str
    amount: Decimal


@dataclass
class PurgeResult:
    deleted: int = 0
    already_gone: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0


def find_candidates(store, account: str) -> tuple[list[Candidate], list[str]]:
    """Split an account's rows into (deletable, protected).

    Deletable = carries an `external_id`, i.e. passbook put it there.
    Protected = everything else. The list comes back empty in practice, because
    a row without an identity cannot be stored — but it is still returned, and
    still shown, so that "nothing is protected" is a thing the operator reads
    rather than a thing the code assumes.
    """
    candidates: list[Candidate] = []
    protected: list[str] = []

    for row in store.account_transactions(account):
        external = str(row.get("external_id") or "")
        if not external:
            protected.append(str(row.get("description") or "(unnamed row)"))
            continue
        day = row.get("txn_date")
        candidates.append(
            Candidate(
                external_id=external,
                date=day.isoformat() if hasattr(day, "isoformat") else str(day or "")[:10],
                description=str(row.get("description") or ""),
                amount=Decimal(str(row.get("amount") or "0")).quantize(_CENT),
            )
        )
    return candidates, protected


def purge(store, candidates: list[Candidate], on_progress=None) -> PurgeResult:
    """Delete each candidate. Never called without CLI-level confirmation.

    A failure is counted and named, never swallowed and never retried silently:
    the operator is about to be told how many rows went, and that number has to
    be one this function actually watched happen.
    """
    result = PurgeResult()
    for candidate in candidates:
        try:
            store.delete_transaction(candidate.external_id)
        except LedgerError as exc:
            result.failed += 1
            result.failures.append((candidate.external_id, str(exc)))
            log.warning("delete failed for %s: %s", candidate.external_id, exc)
        else:
            result.deleted += 1
        if on_progress:
            on_progress(result)
    return result
