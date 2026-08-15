/* Re-apply, as a component rather than a destination. SPEC §18, §15.2.
 *
 * Aliases and rules apply at PUSH time, so editing config cannot reach rows
 * already in Firefly. Reconciling means purge + re-push — a delete, so it always
 * says what it will do first.
 *
 * This lives here because it now appears in two places and must behave
 * identically in both: inline on Payees' diff page the moment config is written
 * (which is where the step was being missed), and on `/reapply`, which keeps the
 * full row-by-row table for anyone who wants to read it before running.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '../lib/api'
import type { ReapplyPreview, ReapplyResult } from '../lib/types'
import { Notice, Tick } from './ui'
import { Progress, Why, describe, useToast } from './feedback'
import { count } from '../lib/money'

/** The purge-and-re-push mutation, with its toast and its navigation. */
export function useReapplyRun() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()

  return useMutation({
    mutationFn: () => api.post<ReapplyResult>('/reapply/run'),
    onSuccess: (result) => {
      queryClient.setQueryData(['reapplyResult'], result)
      queryClient.invalidateQueries({ queryKey: ['overview'] })
      queryClient.invalidateQueries({ queryKey: ['analysis'] })
      queryClient.invalidateQueries({ queryKey: ['reapply'] })
      toast({
        kind: result.reconciles ? 'ok' : 'bad',
        title: 'Re-applied',
        detail: result.reconciles
          ? `Balance reconciles at ₹${result.balance}.`
          : 'The balance does NOT reconcile. Investigate before pushing anything else.',
      })
      navigate('/reapply/done')
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })
}

/**
 * The count, the button, and what the button does.
 *
 * `showLink` is on where the row-by-row table is elsewhere (the diff page) and
 * off where it is already on screen (`/reapply`).
 */
export function ReconcileCall({
  data,
  showLink,
}: {
  data: ReapplyPreview
  showLink?: boolean
}) {
  const run = useReapplyRun()

  if (data.changes.length === 0)
    return (
      <Notice kind="ok">
        <p>
          All {data.considered} transaction{data.considered === 1 ? '' : 's'} in Firefly already
          match what the current config produces. Nothing to do.
        </p>
      </Notice>
    )

  return (
    <>
      <Notice kind="warn">
        <p>
          <strong>{count(data.changes.length, 'existing transaction')} would change</strong> —{' '}
          {count(data.renames, 'rename')}, {count(data.recats, 'category change')}, out of{' '}
          {data.considered} compared. Rules and aliases apply at push time, so the ledger still
          shows what it was pushed with.
        </p>
      </Notice>

      <DumpState dump={data.dump} rows={data.changes.length} />

      <div className="actions">
        <button
          type="button"
          className="danger"
          onClick={() => run.mutate()}
          disabled={run.isPending || !data.dump.fresh}
        >
          {run.isPending
            ? 'Running…'
            : `Purge and re-push ${count(data.changes.length, 'row')}`}
        </button>
        {showLink && (
          <Link className="button" to="/reapply">
            Review the rows first
          </Link>
        )}
      </div>
      {run.isPending && <Progress label="Copying config, purging, syncing rules, re-pushing" />}

      <Why label="What runs, in order">
        <p>
          A copy of <code>config/</code> to <code>backups/</code>; then the purge — only rows
          carrying an <code>external_id</code>, so the opening balance is excluded structurally,
          and trashed rows are force-deleted so the re-push is not rejected as duplicates; then
          rules are synced to Firefly <em>before</em> anything is re-pushed, because rules apply
          at store time and a rule the engine has not heard of cannot categorise; then every
          archived statement is pushed again; then the balance is checked.
        </p>
      </Why>

    </>
  )
}

/**
 * The precondition, stated where the destructive button is.
 *
 * The button used to read "Back up, then purge and re-push" **directly above** a
 * note explaining that this container cannot take a database dump — it would
 * need the Docker socket, which §15.1 deliberately withholds. It promised the
 * one thing the page had just said it could not do, on the only destructive
 * action in the app. What it actually backs up is `config/`.
 *
 * The container can *read* `backups/` even though it cannot write one, so the
 * dump stopped being a suggestion and became a requirement: no dump from the
 * last hour, no purge. `/api/reapply/run` refuses independently — a disabled
 * button is a courtesy, not a guard.
 */
function DumpState({
  dump,
  rows,
}: {
  dump: ReapplyPreview['dump']
  rows: number
}) {
  if (dump.fresh)
    return (
      <p className="muted dump">
        <Tick title="" /> Database dump <code>{dump.name}</code>,{' '}
        {count(dump.ageMinutes ?? 0, 'minute')} old — that is the way back. This page copies{' '}
        <code>config/</code> as well, but it cannot take the dump: no Docker socket.
      </p>
    )

  return (
    <Notice kind="warn">
      <p>
        <strong>Run <code>make backup</code> on the host first.</strong>{' '}
        {dump.ageMinutes === null
          ? 'There is no database dump in backups/ at all.'
          : `The newest dump (${dump.name}) is ${count(dump.ageMinutes, 'minute')} old; ` +
            `this needs one from the last ${count(dump.maxAgeMinutes, 'minute')}.`}{' '}
        This deletes {count(rows, 'row')} and pushes them again, and the dump is the only way
        back. The dump cannot be taken from here — that needs the Docker socket, which this
        container deliberately does not have — but it can be read, so it is required rather than
        suggested.
      </p>
    </Notice>
  )
}
