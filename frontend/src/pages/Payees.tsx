/* Payees. SPEC §16.1, D10.
 *
 * The one page where "just autofill a sensible default" would creep in. It
 * does not. The category control lists only categories that already have a
 * rule; there is no free-text field and no suggestion derived from the token
 * text. D10 measured a 40% error rate on inferring meaning from a ~10-char
 * fragment — a dropdown does not improve that number, so the operator decides.
 *
 * The hours column is the Day Rail at aggregate scale. It is the analysis that
 * split Morning Stall from Late Counter by hand in Phase 4, now permanent.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '../lib/api'
import type { DiffResponse, Payees as PayeesData, ReapplyPreview } from '../lib/types'
import { HourHistogram } from '../components/DayRail'
import { Money, Notice, Token } from '../components/ui'
import { Skeleton, Why, describe, useToast } from '../components/feedback'
import { formatShortDate } from '../lib/money'
import { useAccounts } from '../lib/account'

export function Payees() {
  const navigate = useNavigate()
  const toast = useToast()
  const [aliases, setAliases] = useState<Record<string, string>>({})
  const [categories, setCategories] = useState<Record<string, string>>({})

  const { param, label } = useAccounts()
  const { data, isPending, error } = useQuery({
    queryKey: ['payees', param],
    queryFn: () => api.get<PayeesData>(`/payees${param}`),
  })

  const diff = useMutation({
    mutationFn: () => api.post<DiffResponse>('/payees/diff', { aliases, categories }),
    onSuccess: (response) =>
      navigate('/payees/diff', { state: { response, aliases, categories } }),
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  const undecided = useMemo(
    () => data?.rows.filter((r) => r.needsDecision).length ?? 0,
    [data],
  )

  if (isPending)
    return (
      <div className="page">
        <h1>Payees</h1>
        <Skeleton rows={10} />
      </div>
    )
  if (error)
    return (
      <div className="page">
        <h1>Payees</h1>
        <p className="lede">{describe(error).detail}</p>
      </div>
    )

  return (
    <div className="page">
      <h1>Payees</h1>
      <p className="lede">
        {label && <>{label} — </>}
        {data.rows.length} distinct tokens across {data.total} transactions.{' '}
        {undecided > 0
          ? `${undecided} still ${undecided === 1 ? 'needs' : 'need'} a decision — ` +
            `${undecided === 1 ? 'it is' : 'they are'} listed first.`
          : 'Everything is categorised.'}
      </p>

      <PendingReapply />

      <Why label="Why nothing is guessed here">
        <p>
          Canara truncates the counterparty to about ten characters, and of ten tokens read
          from the fragment alone, four were wrong — a token reading like a restaurant was a
          clothing shop, and one reading like a person's name was a fast-food franchise. The
          category list offers only categories that already have a rule; it never proposes
          one.
        </p>
      </Why>

      <form
        onSubmit={(event) => {
          event.preventDefault()
          diff.mutate()
        }}
      >
        <div className="sheet">
          <div className="sheet__scroll">
            <table className="payees">
              <caption className="visually-hidden">
                Every payee token with its alias, category, hour-of-day spread and totals.
              </caption>
              {/* Fixed widths: the browser was starving the two columns the
                  page exists for. Alias and Category now get 22% each. */}
              <colgroup>
                <col className="col-token" />
                <col className="col-alias" />
                <col className="col-cat" />
                <col className="col-hours" />
                <col className="col-n" />
                <col className="col-money" />
                <col className="col-money" />
                <col className="col-date" />
                <col className="col-date" />
              </colgroup>
              <thead>
                <tr>
                  <th scope="col">Token</th>
                  <th scope="col">Alias</th>
                  <th scope="col">Category</th>
                  <th scope="col">
                    Hours
                    <span className="railscale" aria-hidden="true">
                      <span>00</span>
                      <span>12</span>
                      <span>23</span>
                    </span>
                  </th>
                  <th scope="col" className="num">Txns</th>
                  <th scope="col" className="num">Withdrawn</th>
                  <th scope="col" className="num">Deposited</th>
                  <th scope="col">First</th>
                  <th scope="col">Last</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((row) => (
                  <tr key={`${row.token}|${row.channel}`}>
                    <td>
                      <Token token={row.token} />
                      {row.needsDecision && (
                        <span className="chip chip--warn chip--inline">decide</span>
                      )}
                    </td>
                    <td>
                      <input
                        aria-label={`Alias for ${row.token}`}
                        value={aliases[row.token] ?? row.alias}
                        placeholder="—"
                        onChange={(e) =>
                          setAliases({ ...aliases, [row.token]: e.target.value })
                        }
                      />
                    </td>
                    <td>
                      <select
                        aria-label={`Category for ${row.token}`}
                        value={categories[row.token] ?? row.category}
                        onChange={(e) =>
                          setCategories({ ...categories, [row.token]: e.target.value })
                        }
                      >
                        <option value="">— none —</option>
                        {data.categories.map((c) => (
                          <option key={c} value={c}>
                            {c}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      <HourHistogram
                        hours={row.hours}
                        label={row.alias || row.token}
                        count={row.count}
                        height={26}
                      />
                      {row.clocked < row.count && (
                        <span className="railnote">
                          {row.clocked === 0
                            ? 'no clock'
                            : `${row.clocked}/${row.count} timed`}
                        </span>
                      )}
                    </td>
                    <td className="num">{row.count}</td>
                    <td className="num">
                      <Money value={row.withdrawn === '0.00' ? null : row.withdrawn} plain />
                    </td>
                    <td className="num col-deposit">
                      <Money value={row.deposited === '0.00' ? null : row.deposited} plain />
                    </td>
                    <td className="date">{formatShortDate(row.first)}</td>
                    <td className="date">{formatShortDate(row.last)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="actions">
          <button type="submit" className="primary" disabled={diff.isPending}>
            {diff.isPending ? 'Comparing…' : 'Review changes'}
          </button>
        </div>
        <p className="muted">
          Shows a diff first. Nothing is written to <code>config/</code> until you approve it.
        </p>
      </form>

      <Why label="Reading the hours column">
        <p className="muted">
          Shaded band is midnight to 6am. This is the only place time-of-day exists — the bank
          gives no time column, but {data.totalClocked} of {data.total} rows embed one in the
          narration. It is how one vendor's morning trade was told apart from
          another's after midnight.
        </p>
        <p className="muted">
          The bars count only the transactions that carry a clock, which is not always the
          whole row — NEFT, bank charges, scheme debits and interest have none. Where the two
          differ the row says so, and the chart's accessible label states both numbers.
        </p>
      </Why>
    </div>
  )
}

/**
 * Drift between this config and the ledger, surfaced where the config is edited.
 *
 * The reconcile step also appears immediately after a write (see
 * `PayeesDiff`), but that only helps the session that made the change. This is
 * the case that actually went wrong: payees edited last week, config written,
 * the ledger at :8080 still showing the old names, and no nav item left to
 * remind anyone. Now the page that owns the config says so on sight.
 *
 * Silent when it cannot ask — an unreachable or unconfigured Firefly is not a
 * problem for the page whose job is editing a yaml file.
 */
function PendingReapply() {
  const { data } = useQuery({
    queryKey: ['reapply'],
    queryFn: () => api.get<ReapplyPreview>('/reapply'),
    retry: false,
  })

  if (!data || data.changes.length === 0) return null

  return (
    <Notice kind="warn">
      <p>
        <strong>
          {data.changes.length} transaction{data.changes.length === 1 ? '' : 's'} in the ledger
          {data.changes.length === 1 ? ' does' : ' do'} not match this config
        </strong>{' '}
        — {data.renames} name{data.renames === 1 ? '' : 's'}, {data.recats} categor
        {data.recats === 1 ? 'y' : 'ies'}. Rules and aliases apply at push time, so an edit
        cannot reach rows already pushed. <Link to="/reapply">Review and apply</Link>.
      </p>
    </Notice>
  )
}
