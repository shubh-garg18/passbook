import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { api } from '../lib/api'
import { useBackup } from '../components/reconcile'
import type { Status, BackupState } from '../lib/types'
import { Card, Cross, Tick } from '../components/ui'
import { Skeleton, Why, describe } from '../components/feedback'
import { count } from '../lib/money'

export function StatusPage() {
  const { data, isPending, error } = useQuery({
    queryKey: ['status'],
    queryFn: () => api.get<Status>('/status'),
  })

  if (isPending)
    return (
      <div className="page">
        <h1>Status</h1>
        <Skeleton cards={4} rows={5} />
      </div>
    )
  if (error)
    return (
      <div className="page">
        <h1>Status</h1>
        <p className="lede">{describe(error).detail}</p>
      </div>
    )

  const backupState =
    data.backups.ageDays === null || data.backups.ageDays > data.backups.staleDays
      ? 'warn'
      : undefined

  return (
    <div className="page">
      <h1>Status</h1>


      <div className="cards">
        <Card
          title="Last sync"
          state={data.sync.state === 'stale' ? 'bad' : data.sync.state === 'ok' ? undefined : 'warn'}
        >
          <p className="figure">{data.sync.age !== null ? `${data.sync.age}d` : 'never'}</p>
          <p className="muted">{data.sync.headline}</p>
        </Card>

        <Card title="Newest backup" state={backupState}>
          <p className="figure">
            {data.backups.ageDays !== null ? `${data.backups.ageDays}d` : 'none'}
          </p>
          <p className="muted">
            {data.backups.ageDays === null
              ? 'No database dump in backups/.'
              : data.backups.ageDays > data.backups.staleDays
                ? `Older than ${data.backups.staleDays} days.`
                : 'Recent.'}
          </p>
          <BackupNow />
        </Card>

        <Card title="Second factor" state={data.auth.backupCodesLow ? 'warn' : undefined}>
          <p className="figure">{data.auth.backupCodesLeft}</p>
          <p className="muted">
            backup codes left · {count(data.auth.rememberedDevices, 'remembered device')}
          </p>
          {data.auth.backupCodesLow && (
            <p className="warn">
              {data.auth.backupCodesLeft === 0
                ? 'None left: lose the phone now and the only way back in is make web-totp RESET=yes on the host.'
                : 'Running low. Re-issue a full set from Account while you can still sign in.'}
            </p>
          )}
        </Card>
      </div>

      <Card title="Ledger">
        {data.store.error === null ? (
          <p className="ok">
            <Tick title="reachable" /> reachable — {data.store.accounts} asset account
            {data.store.accounts === 1 ? '' : 's'}
          </p>
        ) : (
          <p className="bad">
            <Cross title="unreachable" /> unreachable — {data.store.error}
          </p>
        )}
        <p className="muted">
          Target account: {data.account.assetAccount ?? '(not set)'} · account assertion:{' '}
          {data.account.assertionConfigured ? 'configured' : 'NOT configured'}
        </p>
        <Why label="Where the ledger lives">
          <p>
            In this app's own tables, on the database beside it. There is no second
            application to sign into and no token to keep alive. Everything it holds is
            on these pages: charts and reports on <Link to="/">Overview</Link> and{' '}
            <Link to="/reports">Reports</Link>, the rows on{' '}
            <Link to="/transactions">Transactions</Link>, names and categories on{' '}
            <Link to="/payees">Payees</Link>.
          </p>
          <p>
            Categories and tags are applied when a row is written, from{' '}
            <code>config/rules.yaml</code>. Editing a payee or a category on{' '}
            <Link to="/payees">Payees</Link> rewrites the rows already stored, in the
            same request — there is nothing to synchronise afterwards.
          </p>
        </Why>
      </Card>


      <Card title="Local backups">
        {data.backups.local.length === 0 ? (
          <p className="muted">Nothing in <code>backups/</code>.</p>
        ) : (
          <ArtefactTable rows={data.backups.local} showAge />
        )}
      </Card>

      <Card title="Off-site (Google Drive)">
        {data.backups.remote.length > 0 ? (
          <ArtefactTable rows={data.backups.remote} />
        ) : (
          <p className="muted">{data.backups.remoteError}</p>
        )}
      </Card>

      <Why label="Why backups cannot be run from here">
        <p>
          It would need the Docker socket — <code>make backup</code> shells into the database
          container and <code>verify-backup</code> starts a scratch one. Mounting the socket
          into the container that listens on a port and parses uploads would make a web
          compromise a host compromise, including the power to delete these archives. Run
          them from the host; this page tells you whether you need to.
        </p>
      </Why>
    </div>
  )
}

function ArtefactTable({
  rows,
  showAge,
}: {
  rows: Status['backups']['local']
  showAge?: boolean
}) {
  return (
    <div className="sheet__scroll">
      <table>
        <thead>
          <tr>
            <th scope="col">File</th>
            <th scope="col" className="num">Size</th>
            <th scope="col">Modified</th>
            {showAge && <th scope="col" className="num">Age</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((a) => (
            <tr key={a.name}>
              <td><span className="tok">{a.name}</span></td>
              <td className="num">{a.humanSize}</td>
              <td className="date">{a.modified}</td>
              {showAge && <td className="num">{a.ageDays}d</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * Take a backup, here. SPEC §37.
 *
 * The card said "Run make backup" — an instruction, on a page, to go and open a
 * terminal. It is a button now, because a backup nobody takes is the only
 * failure in this app with no recovery from it.
 *
 * Honest about what it is not: `make backup` on the host also writes a verified
 * git bundle of the source, and there is no repository in this image. The
 * source is on GitHub; the ledger is not anywhere else.
 */
function BackupNow() {
  const backup = useBackup()
  const { data } = useQuery({
    queryKey: ['backup'],
    queryFn: () => api.get<BackupState>('/backup'),
    retry: false,
  })

  if (data && !data.available)
    return <p className="muted">Cannot back up from here — {data.reason}</p>

  return (
    <>
      <div className="actions">
        <button
          type="button"
          className="primary"
          disabled={backup.isPending}
          data-working={backup.isPending}
          onClick={() => backup.mutate()}
        >
          {backup.isPending ? 'Backing up…' : 'Back up now'}
        </button>
      </div>
      <Why label="What a backup covers">
        <p>
          The ledger and your config, written to <code>backups/</code>. For the source too,
          and to send it off-site encrypted, use the <strong>Backup</strong> desktop shortcut.
        </p>
      </Why>
    </>
  )
}
