/* Reconciling the ledger with the config. SPEC §23, §15.2, §18.
 *
 * Aliases and rules are applied at PUSH time, so editing config cannot reach
 * rows already in the ledger. There are two ways to reach them, and for a long
 * time only the second one existed:
 *
 *   1. **Update them.** Three fields change on rows that already exist, so
 *      `PUT /api/v1/transactions/{group}` is enough. Nothing is deleted, the
 *      write is idempotent, and no database dump is involved.
 *   2. **Purge and re-push.** Still necessary, and still gated on a dump: it
 *      is the only thing that can fix a row that is *missing*, or one whose
 *      amount or date is wrong. An update cannot create a row.
 *
 * The page used to offer only (2) for a rename. Deleting a ledger to correct a
 * payee name is not a trade anyone makes, so the ledger simply stayed stale —
 * which is the flaw this component exists to close.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '../lib/api'
import type { ReapplyPreview, ReapplyResult, SyncResult } from '../lib/types'
import { Notice, Tick } from './ui'
import { Progress, Why, describe, useToast } from './feedback'
import { count } from '../lib/money'
import { invalidateLedger } from '../lib/ledger'

/** Everything a preview says would move, as one sentence's worth of parts. */
function parts(data: ReapplyPreview): string {
  const bits = [
    data.renames && count(data.renames, 'rename'),
    data.recats && count(data.recats, 'category change'),
    data.counterparties && count(data.counterparties, 'payee account rename'),
    data.retags && count(data.retags, 'tag change'),
  ].filter(Boolean) as string[]
  return bits.join(', ')
}

/** The in-place update. Non-destructive, so it asks for nothing first. */
export function useLedgerSync() {
  const queryClient = useQueryClient()
  const toast = useToast()

  return useMutation({
    mutationFn: () => api.post<SyncResult>('/reapply/sync'),
    onSuccess: (result) => {
      invalidateLedger(queryClient)
      const clean = result.failed === 0 && result.remaining === 0
      toast(
        clean
          ? {
              kind: 'ok',
              title: 'Ledger updated',
              detail:
                `${count(result.updated, 'row')} ` +
                `${result.updated === 1 ? 'now matches' : 'now match'} the config.`,
            }
          : {
              kind: 'bad',
              title: result.remaining === null ? 'Synced, not verified' : 'Partly synced',
              detail:
                `${result.updated} updated, ${result.failed} failed, ` +
                (result.remaining === null
                  ? 'and the ledger stopped answering before this could be checked.'
                  : `${result.remaining} still differ.`),
            },
      )
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })
}

/**
 * Take a database dump. SPEC §37.
 *
 * This used to read "run `make backup` on the host — it cannot be taken from
 * here, that needs the Docker socket". True of the socket and false of the
 * conclusion: a dump needs a TCP connection to Postgres, which this container
 * has always had. The most destructive action in the app was gated behind a
 * terminal, which the operator who most needs a backup is least likely to open.
 */
export function useBackup() {
  const queryClient = useQueryClient()
  const toast = useToast()

  return useMutation({
    mutationFn: () => api.post<BackupRun>('/backup'),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['reapply'] })
      queryClient.invalidateQueries({ queryKey: ['status'] })
      queryClient.invalidateQueries({ queryKey: ['backup'] })
      toast({
        kind: 'ok',
        title: 'Backed up',
        detail:
          `${result.dump} — ${Math.round(result.dumpBytes / 1024)} KB. ` +
          (result.sourceBundle
            ? 'The ledger, your config and a bundle of the source.'
            : 'The ledger and your config — the source is on GitHub.'),
      })
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })
}

type BackupRun = {
  dump: string
  dumpBytes: number
  config: string | null
  sourceBundle: boolean
}

/** The purge-and-re-push mutation, with its toast and its navigation. */
export function useReapplyRun() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()

  return useMutation({
    mutationFn: () => api.post<ReapplyResult>('/reapply/run'),
    onSuccess: (result) => {
      queryClient.setQueryData(['reapplyResult'], result)
      invalidateLedger(queryClient)
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
  const sync = useLedgerSync()

  // Zero compared is not zero differing. This page reported a green "All 0
  // transactions already match" for a join that matched nothing at all, which
  // is the failure §23.1 exists to record — so the count of rows actually
  // looked at gates the tick (non-negotiable 11).
  if (data.considered === 0) return <NothingCompared />

  if (data.changes.length === 0)
    return (
      <Notice kind="ok">
        <p>
          All {data.considered} transaction{data.considered === 1 ? '' : 's'} in the ledger
          already match what the current config produces. Nothing to do.
        </p>
      </Notice>
    )

  return (
    <>
      <Notice kind="warn">
        <p>
          <strong>{count(data.changes.length, 'existing transaction')} do not match</strong> —{' '}
          {parts(data)}, out of {data.considered} compared. Rules and aliases apply at push
          time, so the ledger still shows what it was pushed with.
        </p>
      </Notice>

      <div className="actions">
        <button
          type="button"
          className="primary"
          onClick={() => sync.mutate()}
          disabled={sync.isPending}
          data-working={sync.isPending}
        >
          {sync.isPending
            ? 'Updating…'
            : `Update ${count(data.changes.length, 'row')} in the ledger`}
        </button>
        {showLink && (
          <Link className="button" to="/reapply">
            Review the rows first
          </Link>
        )}
      </div>
      {sync.isPending && <Progress label="Writing the new names and categories into the ledger" />}

      <Why label="What an update changes, and what it cannot">
        <p>
          Only the description, category, payee account and rule tags are sent. Amount,
          date and the raw narration are not in the request, so they cannot move. Nothing
          is deleted and running it twice is the same as running it once.
        </p>
        <p>
          It cannot create a row missing from the ledger or fix a wrong amount — those come
          from the statement, so they need a re-push. Whatever is left over is re-read
          afterwards and reported.
        </p>
      </Why>

      <Rebuild data={data} />
    </>
  )
}

/**
 * Nothing was compared — which is not the same as nothing differing.
 *
 * Stated in ochre, because it is the shape of the bug this phase fixed: rows
 * exist in the ledger and statements exist in `archive/`, but the join between
 * them produced no pairs. It says what to check rather than implying all is
 * well, and it deliberately offers no button: there is nothing to update, and
 * an update is not the remedy for a ledger nobody could read.
 */
export function NothingCompared() {
  return (
    <Notice kind="warn">
      <p>
        <strong>No rows were compared</strong> — nothing in <code>archive/</code> matched a
        row in the ledger. That is an unanswered question, not a clean bill of health. Check{' '}
        <code>passbook verify-ledger</code> and that <code>PASSBOOK_ASSET_ACCOUNT</code>{' '}
        names the account the rows went into.
      </p>
    </Notice>
  )
}

/**
 * The destructive path, demoted but not hidden.
 *
 * Kept because it is the only thing that fixes a row that is *absent* or whose
 * amount is wrong, and kept behind the dump requirement for the same reason as
 * before: it deletes every row on the account. It is now folded away, because
 * offering it as the answer to a rename is what made renames never happen.
 */
function Rebuild({ data }: { data: ReapplyPreview }) {
  const run = useReapplyRun()

  return (
    <Why label="Or rebuild the ledger from the statements (deletes and re-pushes)">
      <p>
        A rebuild deletes every row carrying an external id and pushes every archived
        statement again. Reach for it when a row is <em>missing</em> or its amount is
        wrong — not for a rename.
      </p>

      <DumpState dump={data.dump} rows={data.changes.length} />

      <div className="actions">
        <button
          type="button"
          className="danger"
          onClick={() => run.mutate()}
          disabled={run.isPending || !data.dump.fresh}
          data-working={run.isPending}
        >
          {run.isPending ? 'Running…' : 'Purge and re-push everything'}
        </button>
      </div>
      {run.isPending && <Progress label="Copying config, purging, syncing rules, re-pushing" />}

      <p className="muted">
        In order: copy <code>config/</code> to <code>backups/</code>; purge only rows with an{' '}
        <code>external_id</code>, so the opening balance is excluded structurally; sync rules
        <em> before</em> re-pushing, because a rule the store has not heard of cannot
        categorise; push every archived statement; check the balance.
      </p>
    </Why>
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
 * last hour, no purge. That sentence was written before the mount existed —
 * `backups/` was not in docker-compose.yml at all, so the container saw
 * nothing, reported "No backup" on a machine that had one, and refused the
 * rebuild unconditionally. Mounted read-only now (§31). `/api/reapply/run` refuses independently — a disabled
 * button is a courtesy, not a guard. None of this gates the in-place update,
 * which deletes nothing.
 */
function DumpState({
  dump,
  rows,
}: {
  dump: ReapplyPreview['dump']
  rows: number
}) {
  const takeBackup = useBackup()

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
        <strong>Take a backup first.</strong>{' '}
        {dump.ageMinutes === null
          ? 'There is no database dump in backups/ at all.'
          : `The newest dump (${dump.name}) is ${count(dump.ageMinutes, 'minute')} old; ` +
            `this needs one from the last ${count(dump.maxAgeMinutes, 'minute')}.`}{' '}
        This deletes {count(rows, 'row')} and pushes them again, and the dump is the only way
        back.
      </p>
      <div className="actions">
        <button
          type="button"
          className="primary"
          disabled={takeBackup.isPending}
          data-working={takeBackup.isPending}
          onClick={() => takeBackup.mutate()}
        >
          {takeBackup.isPending ? 'Backing up…' : 'Back up now'}
        </button>
      </div>
    </Notice>
  )
}
