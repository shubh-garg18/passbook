/* Review changes — and then reconcile. SPEC §23, §18.
 *
 * This page used to end at "Written." and send you back to Payees with a toast
 * mentioning a Re-apply page you had to go and find. That failed in the way
 * that matters: payees were renamed, the ledger kept showing the old
 * names, and nothing on screen said a second step existed.
 *
 * Then the second step moved here, which helped — but the only step on offer
 * was purge-and-re-push, gated on a database dump taken on the host. Nobody
 * deletes a ledger to fix a payee name, so the names still did not move.
 *
 * Now writing config and updating the rows is **one action**, and the count is
 * on screen *before* the button rather than after it: the same question was
 * always answerable first, it just needed the preview to accept the submitted
 * config as an overlay instead of reading the files it is about to replace.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'

import { api } from '../lib/api'
import type { DiffResponse, ReapplyPreview, SyncResult } from '../lib/types'
import { NothingCompared, ReconcileCall } from '../components/reconcile'
import { Card, Diff, Notice, StampImpression } from '../components/ui'
import { Progress, Skeleton, Why, describe, useToast } from '../components/feedback'
import { count, formatDayMonth } from '../lib/money'
import { invalidateLedger } from '../lib/ledger'

type State = {
  response: DiffResponse
  aliases: Record<string, string>
  categories: Record<string, string>
}

type Applied = { summary: string; synced: SyncResult | null }

export function PayeesDiff() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()
  const state = useLocation().state as State | null
  const [applied, setApplied] = useState<Applied | null>(null)
  // The two remedies §33 needs. Default ON: keeping a roll-up working and not
  // leaving a category that can never match are what the operator almost
  // always wants, and the failure of NOT doing them is silent.
  const [keepTags, setKeepTags] = useState(true)
  const [removeEmptied, setRemoveEmptied] = useState(true)

  const apply = useMutation({
    mutationFn: () =>
      api.post<{ summary: string; synced: SyncResult | null }>('/payees/apply', {
        aliases: state?.aliases ?? {},
        categories: state?.categories ?? {},
        // Which category gets which tag, worked out from what would be lost.
        keepTags: keepTags ? tagRemedy(state?.response) : {},
        removeEmptied: removeEmptied ? (state?.response.emptied ?? []) : [],
      }),
    onSuccess: (result) => {
      invalidateLedger(queryClient)
      const synced = result.synced
      toast({
        kind: synced && (synced.failed || synced.remaining) ? 'bad' : 'ok',
        title: synced && synced.updated ? 'Written and synced' : 'Written',
        detail: result.summary,
      })
      setApplied({ summary: result.summary, synced })
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  if (!state) return <Navigate to="/payees" replace />
  const { response } = state
  const aliasCount = Object.keys(response.aliasChanges).length
  const categoryCount = Object.keys(response.categoryChanges).length
  const rows = response.ledger?.changes.length ?? 0

  if (applied) return <Applied applied={applied} />

  return (
    <div className="page">
      <h1>Review changes</h1>

      {response.changes.length === 0 ? (
        <>
          <p className="lede">Nothing would change.</p>
          <button type="button" onClick={() => navigate('/payees')}>
            Back
          </button>
        </>
      ) : (
        <>
          <p className="lede">
            {aliasCount} alias change{aliasCount === 1 ? '' : 's'}, {categoryCount} category
            change{categoryCount === 1 ? '' : 's'}. Comments in the files are preserved.
          </p>

          {response.changes.map((change) => (
            <Card key={change.path} title={change.path}>
              <Diff text={change.diff} />
            </Card>
          ))}

          <Consequences
            response={response}
            keepTags={keepTags}
            setKeepTags={setKeepTags}
            removeEmptied={removeEmptied}
            setRemoveEmptied={setRemoveEmptied}
          />

          <h2 className="section">And in the ledger</h2>
          <LedgerImpact ledger={response.ledger} />

          <div className="actions">
            <button
              type="button"
              className="primary"
              onClick={() => apply.mutate()}
              disabled={apply.isPending}
              data-working={apply.isPending}
            >
              {apply.isPending
                ? 'Writing…'
                : rows > 0
                  ? `Write config and update ${count(rows, 'row')}`
                  : 'Write config and sync rules'}
            </button>
            <button type="button" onClick={() => navigate('/payees')}>
              Cancel
            </button>
          </div>
          {apply.isPending && (
            <Progress label="Writing config, syncing rules, updating the ledger" />
          )}
        </>
      )}
    </div>
  )
}

/**
 * The two consequences a diff of payee lists cannot show. SPEC §33.
 *
 * Both of these actually happened, silently, and were found weeks later from a
 * The ledger report that was empty:
 *
 *   * moving Day Canteen, Night Canteen and Mess into a new College Expense
 *     category left those three rules with no payees. They still exist — in
 *     this dropdown and in the ledger — and can never match anything again, so a
 *     report on one is permanently blank.
 *   * College Expense carries no `tag:`, so 29 rows lost `food`. The food
 *     roll-up dropped rows from the total and went on looking entirely plausible.
 *
 * Neither is visible in a YAML diff of payee lists, which is exactly why they
 * went unnoticed. Neither is forbidden either — they may be what the operator
 * wants. They just have to be said out loud first.
 */
function Consequences({
  response,
  keepTags,
  setKeepTags,
  removeEmptied,
  setRemoveEmptied,
}: {
  response: DiffResponse
  keepTags: boolean
  setKeepTags: (on: boolean) => void
  removeEmptied: boolean
  setRemoveEmptied: (on: boolean) => void
}) {
  const lost = response.ledger?.tagsLost ?? []
  const remedy = tagRemedy(response)
  const targets = Object.keys(remedy)
  if (response.emptied.length === 0 && lost.length === 0) return null

  return (
    <Notice kind="warn">
      {lost.map((entry) => (
        <p key={entry.tag}>
          <strong>
            {count(entry.rows, 'row')} would lose the <code>{entry.tag}</code> tag.
          </strong>{' '}
          The <code>{entry.tag}</code> roll-up on the Ledger is built from that tag, so it
          would drop those rows — and still look like a plausible number.
        </p>
      ))}
      {targets.length > 0 && (
        <label className="field field--check">
          <input
            type="checkbox"
            checked={keepTags}
            onChange={(event) => setKeepTags(event.target.checked)}
          />
          <span>
            Keep {lost.map((l) => l.tag).join(', ')} counting — tag{' '}
            {targets.join(', ')} too
          </span>
        </label>
      )}

      {response.emptied.length > 0 && (
        <>
          <p>
            <strong>{response.emptied.join(', ')} would be left with no payees.</strong>{' '}
            {response.emptied.length === 1 ? 'It' : 'They'} would keep existing — in this
            dropdown and in the ledger — and never match anything again, so a report on{' '}
            {response.emptied.length === 1 ? 'it' : 'them'} would be permanently empty.
          </p>
          <label className="field field--check">
            <input
              type="checkbox"
              checked={removeEmptied}
              onChange={(event) => setRemoveEmptied(event.target.checked)}
            />
            <span>Remove {response.emptied.join(', ')} as well</span>
          </label>
        </>
      )}
    </Notice>
  )
}

/**
 * `{ category: tag }` — which category needs which tag so nothing stops counting.
 *
 * Read off the change set rather than guessed: a row losing `food` is moving
 * INTO some category, and that is the one that needs the tag. If a single
 * change spans two target categories, both get it.
 */
function tagRemedy(response?: DiffResponse): Record<string, string> {
  const out: Record<string, string> = {}
  for (const change of response?.ledger?.changes ?? []) {
    const [first] = change.oldTags.filter((t) => !change.newTags.includes(t))
    if (first && change.newCategory) out[change.newCategory] = first
  }
  return out
}

/**
 * What the write means for rows already in the ledger — stated BEFORE the write.
 *
 * The rows are listed, not just counted. A count invites a click; a list of
 * "this becomes that" is the thing that catches an alias typed into the wrong
 * row, which is the mistake this page is the last chance to catch.
 */
function LedgerImpact({ ledger }: { ledger: ReapplyPreview | null }) {
  if (ledger === null)
    return (
      <Notice kind="warn">
        <p>
          The ledger could not be asked what this would change, so the config will
          be written and the rows already pushed will be left alone. Open{' '}
          <Link to="/reapply">Re-apply</Link> once it is reachable.
        </p>
      </Notice>
    )

  if (ledger.considered === 0) return <NothingCompared />

  if (ledger.changes.length === 0)
    return (
      <Notice kind="ok">
        <p>
          Nothing already in the ledger changes. All {ledger.considered} pushed row
          {ledger.considered === 1 ? '' : 's'} already read the way this config would produce
          them — these edits affect future pushes only.
        </p>
      </Notice>
    )

  return (
    <>
      <Notice kind="warn">
        <p>
          <strong>
            {count(ledger.changes.length, 'row')} already in the ledger will be updated
          </strong>{' '}
          in place, out of {ledger.considered} compared. Nothing is deleted.
        </p>
      </Notice>

      <div className="sheet">
        <div className="sheet__scroll">
          <table className="changes">
            <caption className="visually-hidden">
              Rows this config change would rewrite
            </caption>
            <thead>
              <tr>
                <th scope="col">Date</th>
                <th scope="col">Now</th>
                <th scope="col">Becomes</th>
                <th scope="col">Category</th>
                <th scope="col">Tags</th>
              </tr>
            </thead>
            <tbody>
              {ledger.changes.map((c) => (
                <tr key={c.externalId}>
                  <td className="date">{formatDayMonth(c.date)}</td>
                  <td className="was">{c.oldDescription || '—'}</td>
                  <td className="now">{c.newDescription}</td>
                  <td>
                    {c.categoryChanged ? (
                      <>
                        <span className="was">{c.oldCategory || '(none)'}</span>{' '}
                        <span className="now">{c.newCategory || '(none)'}</span>
                      </>
                    ) : (
                      <span className="muted">{c.oldCategory || '(none)'}</span>
                    )}
                  </td>
                  <td>
                    {c.tagsChanged ? (
                      <>
                        <span className="was">{c.oldTags.join(', ') || '(none)'}</span>{' '}
                        <span className="now">{c.newTags.join(', ') || '(none)'}</span>
                      </>
                    ) : (
                      <span className="muted">{c.oldTags.join(', ') || '—'}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <Why label="Why these rows do not already say this">
        <p>
          Aliases and rules apply <em>when a statement is pushed</em>, so a row keeps the
          name it was pushed with until something rewrites it. That rewrite is part of this
          button now — rules first, then the rows.
        </p>
      </Why>
    </>
  )
}

/** After the write: what actually happened to the ledger, re-read from the ledger. */
function Applied({ applied }: { applied: Applied }) {
  const { summary, synced } = applied

  return (
    <div className="page">
      <h1>
        Config written
        <StampImpression label="recorded" />
      </h1>
      <p className="lede">{summary}</p>

      {synced && synced.updated > 0 && (
        <Notice kind="ok">
          <p>
            {count(synced.updated, 'row')} {synced.updated === 1 ? 'was' : 'were'}{' '}
            rewritten in place. The ledger shows the new names now; nothing was deleted.
          </p>
        </Notice>
      )}

      {synced && synced.remaining === null && (
        <Notice kind="warn">
          <p>
            The rows were written, but the ledger stopped answering before it could be
            checked — so whether any still differ is <strong>unverified</strong>, not
            clean. Reload, or run <code>passbook resync</code> on the host.
          </p>
        </Notice>
      )}

      {synced && synced.failures.length > 0 && (
        <Notice kind="warn">
          <p>{count(synced.failed, 'row')} could not be updated:</p>
          <ul>
            {synced.failures.map((f) => (
              <li key={f.externalId}>
                <code>{f.externalId}</code> — {f.message}
              </li>
            ))}
          </ul>
        </Notice>
      )}

      <Residual />

      <div className="actions">
        <Link className="button" to="/payees">
          Back to Payees
        </Link>
      </div>
    </div>
  )
}

/**
 * Whatever the update could not reach.
 *
 * Re-queried rather than inferred from the response we just received. A row an
 * update cannot fix — one missing from the ledger, one with the wrong amount —
 * looks exactly like a row it did fix if you only count the requests that
 * returned 200 (non-negotiable 11).
 */
function Residual() {
  const { data, isPending, error } = useQuery({
    queryKey: ['reapply'],
    queryFn: () => api.get<ReapplyPreview>('/reapply'),
  })

  if (isPending)
    return (
      <>
        <h2 className="section">Existing rows</h2>
        <Skeleton rows={3} />
      </>
    )
  // A ledger this cannot reach is not an error on this page: the config write
  // succeeded, which is what the operator just asked for.
  if (error)
    return (
      <Notice kind="warn">
        <p>
          Config is written. Whether any rows still differ could not be checked —{' '}
          {describe(error).detail}
        </p>
      </Notice>
    )

  return <ReconcileCall data={data} showLink />
}
