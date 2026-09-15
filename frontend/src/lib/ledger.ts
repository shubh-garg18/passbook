/* What goes stale when the ledger moves. SPEC §16.1.
 *
 * Every page here is a view of two things: the statements in `archive/` (plus
 * whatever is staged in the session) and the rows in the ledger. Anything that
 * changes either of those changes all of them — and the invalidation for that
 * was written out by hand at each call site, which meant it was different at
 * each call site:
 *
 *   * uploading a statement invalidated **nothing**, even though a staged file
 *     is included in `/payees`, `/overview` and `/analysis` the moment it is
 *     staged. Payees kept its cached caption — "59 distinct tokens across 113
 *     transactions" — while the page was already describing a different set.
 *   * discarding a staged file invalidated nothing either, so the inflated
 *     count stayed after the file was deleted.
 *   * pushing invalidated `overview` and `payees`, but not `analysis`,
 *     `status` or `reapply`, so the Ledger charts and the sync age kept the
 *     numbers from before the push.
 *
 * A cache that is stale in a way the operator cannot see is the same class of
 * problem as a green tick for something never checked: the screen is confident
 * and wrong. So there is one list, here, and the call sites say *what
 * happened* rather than enumerating what to expire.
 */

import type { QueryClient } from '@tanstack/react-query'

/**
 * Keys derived from the statements and the ledger.
 *
 * Prefixes: react-query matches `['payees']` against `['payees', param]`, so
 * the account-scoped variants are covered without listing them.
 *
 * Deliberately NOT here: `session` (auth, not data), `reminder` (a schedule
 * this cannot change), and `pending` (owned by the upload flow, which removes
 * it explicitly when the staged file is gone).
 */
const LEDGER_KEYS = [
  'overview',
  'analysis',
  'payees',
  'status',
  'reapply',
  'accounts',
  // §61. Added WITH the page, not after it: this list exists because the
  // invalidation used to be written out per call site and was therefore
  // different per call site, and a browser showing rows that were pushed an
  // upload ago is exactly that bug again.
  'transactions',
]

/** The statements or the ledger changed — re-read everything that reads them. */
export function invalidateLedger(client: QueryClient): void {
  for (const key of LEDGER_KEYS) {
    client.invalidateQueries({ queryKey: [key] })
  }
}
