"""Delete previously-pushed transactions from one asset account.

Not in SPEC §3's layout — added when a re-push was needed after aliases changed
the display names, since aliases apply at push time and re-pushing hits dedup.

**The `external_id` is the safety mechanism, not a date range.** Every row this
tool pushed carries the bank's transaction ID there; nothing else on the account
does. So an account's opening balance — which has no `external_id` — is excluded
structurally rather than by a guard that could be got wrong. Verified on the
live account: 94 groups, 93 with an external_id, 1 without.

Nothing here runs without an explicit confirmation from the CLI layer.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .client import FireflyClient, FireflyError

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    group_id: str
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
    # Whether the follow-up force-delete ran. Without it the deletion is only
    # half done — see purge() for why.
    hard_purged: bool = False
    # Where the intent was recorded (§19.7). The caller advances it to
    # "repushing" and clears it only once the ledger verifies.
    intent: "Path | None" = None

    @property
    def ok(self) -> bool:
        return self.failed == 0


def find_candidates(
    client: FireflyClient, account_id: str | int
) -> tuple[list[Candidate], list[str]]:
    """Split an account's groups into (deletable, protected).

    Deletable = carries an `external_id`, i.e. passbook put it there.
    Protected = everything else, most importantly the opening balance.
    """
    candidates: list[Candidate] = []
    protected: list[str] = []

    for group in client.account_transactions(account_id):
        splits = group.get("attributes", {}).get("transactions", []) or [{}]
        external = next((s.get("external_id") for s in splits if s.get("external_id")), None)
        first = splits[0]
        if not external:
            protected.append(first.get("description") or f"group {group['id']}")
            continue
        candidates.append(
            Candidate(
                group_id=str(group["id"]),
                external_id=str(external),
                date=str(first.get("date", ""))[:10],
                description=first.get("description") or "",
                # Firefly returns amounts as 12-decimal strings ("48.000000000000").
                # Quantise for display; this value is never pushed anywhere.
                amount=Decimal(str(first.get("amount", "0"))).quantize(Decimal("0.01")),
            )
        )
    return candidates, protected


def purge(
    client: FireflyClient,
    candidates: list[Candidate],
    on_progress=None,
    *,
    account: str = "",
    statements: list[str] | None = None,
    intent: "Path | None" = None,
    slug: str = "",
) -> PurgeResult:
    """Delete each candidate. Never called without CLI-level confirmation.

    **Intent is recorded before the first delete** (§19.7). If the caller did not
    already write one, this writes it — so no caller can forget, which is the
    property that matters: a purge that dies mid-flight leaves a *coherent*
    ledger, and the only thing that distinguishes it from a healthy one is the
    record that a cycle was started and never finished.

    `statements` is what a resume needs to push back; a caller that knows them
    should pass them, and `passbook purge` does.
    """
    from .. import ops

    own_intent = intent is None
    if own_intent:
        intent = ops.write_purge_intent(
            account or "(unnamed account)",
            [c.external_id for c in candidates],
            statements or [],
            slug=slug,
        )

    result = PurgeResult(intent=intent)
    for candidate in candidates:
        try:
            client.delete_transaction(candidate.group_id)
        except FireflyError as exc:
            if exc.status == 401:
                # Firefly returns 401 both for "not yours / already gone" and
                # for a dead token. Distinguish them rather than guessing: if
                # the token still works, the group is simply already absent.
                if client.token_is_valid():
                    result.already_gone += 1
                    log.debug("already gone: %s", candidate.group_id)
                    continue
                raise FireflyError(
                    "the token stopped authenticating partway through the purge; "
                    f"{result.deleted} group(s) were already deleted. Re-run after "
                    "issuing a new token — it is safely resumable, since deleted "
                    "groups report as already gone."
                ) from exc
            result.failed += 1
            result.failures.append((candidate.external_id, str(exc)))
            log.warning("delete failed for %s: %s", candidate.group_id, exc)
        else:
            result.deleted += 1
        if on_progress:
            on_progress(result)

    # Deleting is only half the job. Firefly soft-deletes, and its duplicate
    # check explicitly searches trashed rows, so without this a later push of
    # identical content is rejected as a duplicate of a transaction that no
    # longer visibly exists. Observed: after deleting 93 and re-pushing, only
    # the 41 whose description had changed got through; the other 52 collided
    # with their own tombstones.
    if result.deleted or result.already_gone:
        log.info("force-deleting trashed records so re-pushes are not blocked")
        client.purge_trashed()
        result.hard_purged = True

    # The stage is advanced only once the force-delete has actually run, so an
    # intent still reading "purging" means tombstones may remain — which is
    # exactly what the next re-push needs to know.
    if intent is not None:
        ops.update_purge_intent(
            intent,
            stage="purged" if result.hard_purged else "purging",
            deleted=result.deleted,
            already_gone=result.already_gone,
        )

    return result
