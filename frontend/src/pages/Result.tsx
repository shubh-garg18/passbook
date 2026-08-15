import { useQueryClient } from '@tanstack/react-query'
import { Link, Navigate } from 'react-router-dom'

import type { PushResult } from '../lib/types'
import { Card, Cross, Notice, Tick } from '../components/ui'

export function Result() {
  const queryClient = useQueryClient()
  const result = queryClient.getQueryData<PushResult>(['result'])

  // Reloading /result after the fact has nothing to show; the ledger does.
  if (!result) return <Navigate to="/" replace />

  return (
    <div className="page">
      <h1>Sync result</h1>

      <div className="cards">
        <Card title="Pushed" state="ok">
          <p className="figure">{result.pushed}</p>
          <p className="muted">of {result.parsed} parsed</p>
        </Card>
        <Card title="Duplicates skipped">
          <p className="figure">{result.duplicates}</p>
          <p className="muted">expected on overlapping downloads</p>
        </Card>
        <Card title="Failed" state={result.failed ? 'bad' : undefined}>
          <p className="figure">{result.failed}</p>
        </Card>
      </div>

      {result.failures.length > 0 && (
        <Card title="Failures">
          {result.failures.map((f) => (
            <p key={f.id} className="bad">
              <Cross /> <span className="tok">{f.id}</span> {f.message}
            </p>
          ))}
        </Card>
      )}

      {result.archived ? (
        <Notice kind="ok">
          <p>
            <Tick title="archived" /> Archived to <code>{result.archived}</code>.
          </p>
        </Notice>
      ) : (
        <Notice kind="warn">
          <p>
            Left in <code>inbox/</code>. Archiving happens only after a fully successful push,
            so the file stays put and the run can be retried.
          </p>
        </Notice>
      )}

      <div className="actions">
        <Link className="button" to="/">Ledger</Link>
        <Link className="button" to="/payees">Payees</Link>
      </div>
    </div>
  )
}
