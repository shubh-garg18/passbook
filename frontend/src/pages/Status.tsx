import { useQuery } from '@tanstack/react-query'

import { api } from '../lib/api'
import type { Status } from '../lib/types'
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

  const tokenState = !data.token.shapeOk
    ? 'bad'
    : data.token.daysLeft !== null && data.token.daysLeft <= 30
      ? 'warn'
      : undefined
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

        <Card title="Firefly token" state={tokenState}>
          {!data.token.shapeOk ? (
            <>
              <p className="figure">bad shape</p>
              <p className="muted">Not a JWT. The “Command line token” is a different credential.</p>
            </>
          ) : data.token.daysLeft === null ? (
            <>
              <p className="figure">unknown</p>
              <p className="muted">No readable <code>exp</code> claim.</p>
            </>
          ) : (
            <>
              <p className="figure">{data.token.daysLeft}d</p>
              <p className="muted">expires {data.token.expiry}</p>
            </>
          )}
        </Card>

        <Card title="Newest backup" state={backupState}>
          <p className="figure">
            {data.backups.ageDays !== null ? `${data.backups.ageDays}d` : 'none'}
          </p>
          <p className="muted">
            {data.backups.ageDays === null
              ? 'No database dump in backups/.'
              : data.backups.ageDays > data.backups.staleDays
                ? `Older than ${data.backups.staleDays} days. Run make backup.`
                : 'Recent.'}
          </p>
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

      <Card title="Firefly">
        {data.firefly.about ? (
          <p className="ok">
            <Tick title="reachable" /> reachable — v{data.firefly.about.version}, api v
            {data.firefly.about.api_version}, {data.firefly.about.driver}
          </p>
        ) : (
          <p className="bad">
            <Cross title="unreachable" /> unreachable — {data.firefly.error}
          </p>
        )}
        <p className="muted">
          Target account: {data.account.assetAccount ?? '(not set)'} · account assertion:{' '}
          {data.account.assertionConfigured ? 'configured' : 'NOT configured'}
        </p>
      </Card>

      <Card title="Alias drift">
        {data.drift.length === 0 ? (
          <p className="ok">
            <Tick title="none" /> None. payees.md's Alias column is generated from the yaml, so
            it cannot drift.
          </p>
        ) : (
          <>
            <p className="warn">{count(data.drift.length, 'genuine disagreement')}:</p>
            {data.drift.map((d) => (
              <p key={d} className="muted">{d}</p>
            ))}
          </>
        )}
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
