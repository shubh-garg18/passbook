/* Re-apply config to existing transactions. SPEC §15.2, §18.
 *
 * Aliases and rules apply at PUSH time, so editing config leaves rows already
 * in Firefly untouched. Reconciling means deleting them and pushing again
 * through the same path `make sync` uses — safe and repeatable, but a delete,
 * so it asks first.
 *
 * **No longer a nav item, and not the primary way in.** The step is now offered
 * where it is needed: on Payees the moment config is written, and on Payees at
 * all times when the ledger disagrees with config. This page is what "Review the
 * rows first" opens — it exists for the row-by-row table, which is too much for
 * an inline card, and it shares its call to action with that card so the two
 * cannot say different things.
 */

import { useQuery } from '@tanstack/react-query'

import { api } from '../lib/api'
import type { ReapplyPreview } from '../lib/types'
import { ReconcileCall } from '../components/reconcile'
import { Arrow, Card, Money } from '../components/ui'
import { Skeleton, Why, describe } from '../components/feedback'
import { count, formatDayMonth } from '../lib/money'

export function Reapply() {
  const { data, isPending, error } = useQuery({
    queryKey: ['reapply'],
    queryFn: () => api.get<ReapplyPreview>('/reapply'),
  })

  if (isPending)
    return (
      <div className="page">
        <h1>Re-apply config</h1>
        <Skeleton cards={3} rows={6} />
      </div>
    )
  if (error)
    return (
      <div className="page">
        <h1>Re-apply config</h1>
        <p className="lede">{describe(error).detail}</p>
      </div>
    )

  return (
    <div className="page">
      <h1>Re-apply config</h1>

      <Why label="Why the ledger still shows the old names">
        <p>
          Aliases and rules are applied <em>when a statement is pushed</em>. Editing them on
          Payees changes what <em>future</em> pushes produce; it cannot reach back into rows
          already in Firefly. The ledger is a record of what config said at push time, so
          nothing is broken — this page is the reconciliation.
        </p>
      </Why>

      {data.changes.length === 0 ? (
        <ReconcileCall data={data} />
      ) : (
        <>
          <div className="cards">
            <Card title="Compared">
              <p className="figure">{data.considered}</p>
            </Card>
            <Card title="Would change" state="warn">
              <p className="figure">{data.changes.length}</p>
            </Card>
            <Card title="Renames">
              <p className="figure">{data.renames}</p>
              <p className="muted">{count(data.recats, 'category change')}</p>
            </Card>
          </div>

          <div className="sheet">
            <div className="sheet__scroll">
              <table>
                <caption className="visually-hidden">
                  Rows whose name or category would change
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Date</th>
                    <th scope="col" className="num">Amount</th>
                    <th scope="col">Name</th>
                    <th scope="col">Category</th>
                  </tr>
                </thead>
                <tbody>
                  {data.changes.map((c) => (
                    <tr key={c.externalId}>
                      <td className="date">{formatDayMonth(c.date)}</td>
                      <td className="num"><Money value={c.amount} plain /></td>
                      <td>
                        {c.nameChanged ? (
                          <>
                            <span className="was">{c.oldDescription || '—'}</span> <Arrow />{' '}
                            <span className="now">{c.newDescription}</span>
                          </>
                        ) : (
                          <span className="muted">{c.oldDescription}</span>
                        )}
                      </td>
                      <td>
                        {c.categoryChanged ? (
                          <>
                            <span className="was">{c.oldCategory || '(none)'}</span> <Arrow />{' '}
                            <span className="now">{c.newCategory || '(none)'}</span>
                          </>
                        ) : (
                          <span className="muted">{c.oldCategory || '(none)'}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <ReconcileCall data={data} />
        </>
      )}
    </div>
  )
}
