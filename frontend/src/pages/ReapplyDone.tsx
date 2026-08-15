import { useQueryClient } from '@tanstack/react-query'
import { Link, Navigate } from 'react-router-dom'

import type { ReapplyResult } from '../lib/types'
import { Card, Cross, Money, Notice, Tick } from '../components/ui'

export function ReapplyDone() {
  const queryClient = useQueryClient()
  const result = queryClient.getQueryData<ReapplyResult>(['reapplyResult'])
  if (!result) return <Navigate to="/reapply" replace />

  return (
    <div className="page">
      <h1>Re-apply result</h1>

      <Card>
        {result.steps.map((step) => (
          <p key={step.message} className={step.state === 'ok' ? 'ok' : 'bad'}>
            {step.state === 'ok' ? <Tick title="done" /> : <Cross title="failed" />}{' '}
            {step.message}
          </p>
        ))}
      </Card>

      {result.reconciles ? (
        <Notice kind="ok">
          <p>
            <Tick title="reconciles" />{' '}
            <strong>
              Balance reconciles: <Money value={result.balance} />.
            </strong>
          </p>
        </Notice>
      ) : (
        <Notice kind="bad">
          <p>
            <strong>The balance does not reconcile.</strong> Firefly reports{' '}
            <Money value={result.balance} />; the newest statement closes at{' '}
            <Money value={result.expected} />. Do not push anything else until this is
            understood. A backup was taken before the purge and every statement is still in{' '}
            <code>archive/</code>, so this is recoverable — but investigate before continuing.
          </p>
        </Notice>
      )}

      <div className="actions">
        <Link className="button" to="/status">Status</Link>
        <Link className="button" to="/">Ledger</Link>
      </div>
    </div>
  )
}
