/* Review changes — and then reconcile. SPEC §18.
 *
 * This page used to end at "Written." and send you back to Payees with a toast
 * mentioning a Re-apply page you had to go and find. That failed in the way
 * that matters: payees were renamed, the ledger at :8080 kept showing the old
 * names, and nothing on screen said a second step existed.
 *
 * Re-apply is not a destination, it is the second half of editing a payee. So
 * the write is now followed, in place, by the only question left worth asking:
 * N existing transactions would change — apply now?
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'

import { api } from '../lib/api'
import type { DiffResponse, ReapplyPreview } from '../lib/types'
import { ReconcileCall } from '../components/reconcile'
import { Card, Diff, Notice } from '../components/ui'
import { Progress, Skeleton, describe, useToast } from '../components/feedback'

type State = {
  response: DiffResponse
  aliases: Record<string, string>
  categories: Record<string, string>
}

export function PayeesDiff() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()
  const state = useLocation().state as State | null
  const [written, setWritten] = useState<string | null>(null)

  const apply = useMutation({
    mutationFn: () =>
      api.post<{ summary: string; reapplyHint: boolean }>('/payees/apply', {
        aliases: state?.aliases ?? {},
        categories: state?.categories ?? {},
      }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['payees'] })
      queryClient.invalidateQueries({ queryKey: ['reapply'] })
      toast({ kind: 'ok', title: 'Written', detail: result.summary })
      // Stay here. The next step is the reconcile card below, not a page the
      // operator has to know about.
      setWritten(result.summary)
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  if (!state) return <Navigate to="/payees" replace />
  const { response } = state
  const aliasCount = Object.keys(response.aliasChanges).length
  const categoryCount = Object.keys(response.categoryChanges).length

  if (written)
    return (
      <div className="page">
        <h1>Config written</h1>
        <p className="lede">{written}</p>
        <Reconcile />
        <div className="actions">
          <Link className="button" to="/payees">
            Back to Payees
          </Link>
        </div>
      </div>
    )

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
            change{categoryCount === 1 ? '' : 's'}. Comments in the files are preserved — they
            carry the knowledge that stops a token being misread a second time.
          </p>

          {response.changes.map((change) => (
            <Card key={change.path} title={change.path}>
              <Diff text={change.diff} />
            </Card>
          ))}

          <div className="actions">
            <button
              type="button"
              className="primary"
              onClick={() => apply.mutate()}
              disabled={apply.isPending}
            >
              {apply.isPending ? 'Writing…' : 'Write config and sync rules'}
            </button>
            <button type="button" onClick={() => navigate('/payees')}>
              Cancel
            </button>
          </div>
          {apply.isPending && <Progress label="Writing config and syncing rules to Firefly" />}

          <Notice>
            <p>
              Writing config alone changes nothing already in Firefly — rules and aliases apply
              at push time. Rows already pushed keep the names and categories they were pushed
              with. The next screen offers to reconcile them.
            </p>
          </Notice>
        </>
      )}
    </div>
  )
}

/**
 * The second half: what the write means for rows already in the ledger.
 *
 * Shown here rather than linked to, because the count is the whole point and it
 * is only knowable after the write. The call to action itself is
 * `ReconcileCall`, shared with `/reapply` so the two cannot drift.
 */
function Reconcile() {
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
          Config is written. Whether existing rows would change could not be checked —{' '}
          {describe(error).detail}
        </p>
      </Notice>
    )

  return <ReconcileCall data={data} showLink />
}
