/* Preview: what is about to be pushed. SPEC §16.1.
 *
 * Rows are shown here — approved in Stage 1 — but **no category column.**
 * Rules are applied by Firefly at store time, so at preview no category
 * exists. Showing one would be a guess, and D10 measured a 40% error rate on
 * guessing from a truncated token.
 *
 * The row list is complete and in sheet order, because it carries a Balance
 * column. See components/Ledger.tsx.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '../lib/api'
import type { Parsed, PushResult } from '../lib/types'
import { Ledger } from '../components/Ledger'
import { Card, Money, Notice, Tick, Token } from '../components/ui'
import { Progress, Skeleton, Why, describe, useToast } from '../components/feedback'
import { count, formatDate } from '../lib/money'

export function Preview() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()

  const { data, isPending, error } = useQuery({
    queryKey: ['pending'],
    queryFn: () => api.get<Parsed>('/statement/pending'),
  })

  const push = useMutation({
    mutationFn: () => api.post<PushResult>('/statement/confirm'),
    onSuccess: (result) => {
      queryClient.setQueryData(['result'], result)
      queryClient.invalidateQueries({ queryKey: ['overview'] })
      queryClient.invalidateQueries({ queryKey: ['payees'] })
      toast({
        kind: result.failed ? 'warn' : 'ok',
        title: 'Pushed',
        detail:
          `${result.pushed} added, ${count(result.duplicates, 'duplicate')} skipped` +
          (result.failed ? `, ${result.failed} failed` : '.'),
      })
      navigate('/result')
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  const discard = useMutation({
    mutationFn: () => api.del('/statement/pending'),
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: ['pending'] })
      toast({ kind: 'ok', title: 'Discarded', detail: 'The staged file was deleted.' })
      navigate('/upload')
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  if (isPending)
    return (
      <div className="page">
        <h1>Preview</h1>
        <Skeleton cards={3} rows={8} />
      </div>
    )
  if (error) {
    return (
      <div className="page">
        <h1>Nothing staged</h1>
        <p className="lede">{describe(error).detail}</p>
        <Link className="button button--primary" to="/upload">
          Upload a statement
        </Link>
      </div>
    )
  }

  return (
    <div className="page">
      <h1>Preview</h1>
      <p className="lede">
        <span className="tok">{data.filename}</span> — parsed and validated.{' '}
        <strong>Nothing has been pushed.</strong>
      </p>

      {/* Which account it will land in, said before the push rather than after.
          Routing is by the statement's own account number (§21.7), so it is
          deliberately independent of the switcher: a statement belongs to one
          account as a matter of fact, and the one thing worse than routing it
          silently would be routing it silently to the account you happened to be
          looking at. Only shown once more than one account exists — a
          single-account operator has nothing to disambiguate. */}
      {data.routed?.registered && (
        <Notice>
          <p>
            Routes to <strong>{data.routed.label}</strong> ({data.routed.account}) — from the
            account number in the statement, not from the account you are viewing.
          </p>
        </Notice>
      )}

      <div className="cards">
        <Card title="Rows parsed">
          <p className="figure">{data.count}</p>
          <p className="muted">
            {formatDate(data.meta.periodFrom)} to {formatDate(data.meta.periodTo)}
          </p>
        </Card>
        <Card title="Withdrawn">
          <p className="figure"><Money value={data.withdrawn} /></p>
          <p className="muted">deposited <Money value={data.deposited} /></p>
        </Card>
        <Card title="Closes at">
          <p className="figure"><Money value={data.meta.closingBalance} /></p>
          <p className="muted">opened at <Money value={data.meta.openingBalance} /></p>
        </Card>
      </div>

      <Card title="Checks">
        <table className="kv">
          <tbody>
            <tr>
              <th scope="row">Continuity</th>
              <td className="ok">
                <Tick title="passed" /> 0 breaks — every row chains from the opening sentinel
                to the closing one
              </td>
            </tr>
            <tr>
              <th scope="row">Account assertion</th>
              <td className="ok">
                <Tick title="passed" /> passes ({data.meta.account})
              </td>
            </tr>
            <tr>
              <th scope="row">Warnings</th>
              <td className="plain">{data.warnings.length === 0 ? 'none' : data.warnings.length}</td>
            </tr>
          </tbody>
        </table>
        {data.warnings.map((w) => (
          <p key={w} className="warn">{w}</p>
        ))}
      </Card>

      <Card title={`New payee tokens (${data.unknown.length})`}>
        {data.unknown.length === 0 ? (
          <p className="muted">None — every token is already known to <code>config/</code>.</p>
        ) : (
          <>
            <p className="muted">
              Not in <code>config/</code> yet. Name them on <Link to="/payees">Payees</Link>.
            </p>
            <ul className="chips">
              {data.unknown.map((t) => (
                <li key={t} className="chip chip--warn">
                  <Token token={t} />
                </li>
              ))}
            </ul>
          </>
        )}
      </Card>

      <h2>Every row, in sheet order</h2>
      <Ledger transactions={data.transactions} />

      <div className="actions">
        <button
          type="button"
          className="primary"
          onClick={() => push.mutate()}
          disabled={push.isPending}
        >
          {push.isPending ? 'Pushing…' : 'Push to Firefly'}
        </button>
        <button type="button" onClick={() => discard.mutate()} disabled={discard.isPending}>
          Discard
        </button>
      </div>
      {push.isPending && <Progress label={`Pushing ${data.count} transactions to Firefly`} />}

      <Why label="What happens on push">
        <p>
          The same path <code>make sync</code> takes. Duplicates are expected on overlapping
          downloads — counted, not errors. The file is archived only if the push fully
          succeeds.
        </p>
      </Why>
    </div>
  )
}
