/* The ledger browser. SPEC §61.
 *
 * The page passbook never had, and the last routine reason to open a second
 * application. Search, filter, and read one row.
 *
 * **There is deliberately no running balance column.** `components/Ledger.tsx`
 * has one because it renders a statement in sheet order, complete; §16.4
 * refuses one on any view that can be filtered or reordered, because a Balance
 * column over a search result asserts a continuity that is not there, and §6.6
 * is the spine of this project. That rule is the reason this page is allowed to
 * sort newest-first at all.
 */

import { useEffect, useMemo, useState } from 'react'
import { useQuery, keepPreviousData } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'

import { api } from '../lib/api'
import type { Analysis, Transactions as Rows } from '../lib/types'
import { Card, Notice } from '../components/ui'
import { Skeleton, describe } from '../components/feedback'
import { count, formatAmount, formatClock, formatShortDate } from '../lib/money'
import { useAccounts } from '../lib/account'
import { joinQuery, useRange } from '../lib/range'
import { RangePicker } from '../components/RangePicker'

export function TransactionsPage() {
  const { param, isAll } = useAccounts()
  const range = useRange()
  const [params, setParams] = useSearchParams()

  // The filters live in the URL, like the date window and unlike the account
  // choice (§25): a filtered list is part of what you are looking at, so it has
  // to survive a reload and be linkable. `q` is the exception while you type.
  const category = params.get('category') ?? ''
  const tag = params.get('tag') ?? ''
  const direction = params.get('direction') ?? ''
  const min = params.get('min') ?? ''
  const max = params.get('max') ?? ''
  const sort = params.get('sort') ?? 'date'
  const page = Number(params.get('page') ?? '1') || 1

  const [typed, setTyped] = useState(params.get('q') ?? '')
  const q = params.get('q') ?? ''

  // Debounced into the URL rather than fetched per keystroke: the endpoint
  // reads every row on the account, and 113 rows is one query.
  useEffect(() => {
    const timer = setTimeout(() => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (typed) next.set('q', typed)
          else next.delete('q')
          // Only when the query actually changed. Unconditionally deleting
          // here stripped `?page=` on every mount, 220ms in.
          if ((prev.get('q') ?? '') !== typed) next.delete('page')
          return next
        },
        { replace: true },
      )
    }, 220)
    return () => clearTimeout(timer)
  }, [typed, setParams])

  const filters = useMemo(() => {
    const parts: string[] = []
    if (q) parts.push(`q=${encodeURIComponent(q)}`)
    if (category) parts.push(`category=${encodeURIComponent(category)}`)
    if (tag) parts.push(`tag=${encodeURIComponent(tag)}`)
    if (direction) parts.push(`direction=${direction}`)
    if (min) parts.push(`min=${encodeURIComponent(min)}`)
    if (max) parts.push(`max=${encodeURIComponent(max)}`)
    if (sort && sort !== 'date') parts.push(`sort=${sort}`)
    if (page > 1) parts.push(`page=${page}`)
    return parts.join('&')
  }, [q, category, tag, direction, min, max, sort, page])

  const query = joinQuery(param, [range.query, filters].filter(Boolean).join('&'))
  const { data, isPending, error } = useQuery({
    queryKey: ['transactions', param, range.key, filters],
    queryFn: () => api.get<Rows>(`/transactions${query}`),
    // Without this the table blanks to a skeleton on every keystroke, which
    // makes a search feel broken even when it is fast.
    placeholderData: keepPreviousData,
  })

  // The filter options come from the analysis, so the category list here is the
  // same one the charts use and cannot drift from it.
  const { data: analysis } = useQuery({
    queryKey: ['analysis', param, range.key],
    queryFn: () => api.get<Analysis>(`/analysis${joinQuery(param, range.query)}`),
  })

  function set(key: string, value: string) {
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      if (value) next.set(key, value)
      else next.delete(key)
      // Changing a FILTER returns you to page one — page 3 of a result set you
      // just narrowed is usually empty. Changing the page obviously must not.
      // The unconditional delete that was here ran after the set and made the
      // pager inert: Next produced a URL with no `page` at all.
      if (key !== 'page') next.delete('page')
      return next
    })
  }

  const active = Boolean(q || category || tag || direction || min || max)

  return (
    <div className="page">
      <h1>Transactions</h1>

      <RangePicker
        window={data?.window}
        outside={data?.outsideWindow}
        noun="transaction"
      />

      {/* §84. A labelled grid, not a wrapping row.
          Every control was an unlabelled `<select>` whose only clue was its
          current value, and a flex row put each on a line of its own the
          moment the search box grew. A grid gives them names, keeps them
          side by side, and collapses to two columns on a phone rather than
          to one per line. */}
      <div className="filters">
        <label className="filters__field filters__field--wide">
          <span>Search</span>
          <input
            type="search"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder="Payee, category, amount, or the bank's own narration"
            autoComplete="off"
          />
        </label>

        <label className="filters__field">
          <span>Type</span>
          <select value={direction} onChange={(e) => set('direction', e.target.value)}>
            <option value="">In and out</option>
            <option value="out">Out only</option>
            <option value="in">In only</option>
          </select>
        </label>

        <label className="filters__field">
          <span>Category</span>
          <select value={category} onChange={(e) => set('category', e.target.value)}>
            <option value="">Every category</option>
            {analysis?.categories.map((c) => (
              <option key={c.name} value={c.name}>
                {c.name}
              </option>
            ))}
            {analysis?.excludedSpend.map((c) => (
              <option key={c.name} value={c.name}>
                {c.name} (movement)
              </option>
            ))}
            {/* The income categories. The table renders deposits and the
                server honours `?category=Salary`, but this list was built
                from the spend side only — so "In only" could not be narrowed
                at all. */}
            {analysis?.sourcesByCategory
              .filter(
                (b) =>
                  !analysis.categories.some((c) => c.name === b.name) &&
                  !analysis.excludedSpend.some((c) => c.name === b.name),
              )
              .map((b) => (
                <option key={`in:${b.name}`} value={b.name}>
                  {b.name} (income)
                </option>
              ))}
          </select>
        </label>

        <label className="filters__field">
          <span>Tag</span>
          <select value={tag} onChange={(e) => set('tag', e.target.value)}>
            <option value="">Any tag</option>
            {(data?.tags ?? []).map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>

        {/* A size band. The commonest analyst question this page could not
            express: "show me everything over ten thousand". */}
        <label className="filters__field filters__field--narrow">
          <span>Min ₹</span>
          <input
            type="number"
            inputMode="decimal"
            value={min}
            onChange={(e) => set('min', e.target.value)}
            placeholder="0"
          />
        </label>
        <label className="filters__field filters__field--narrow">
          <span>Max ₹</span>
          <input
            type="number"
            inputMode="decimal"
            value={max}
            onChange={(e) => set('max', e.target.value)}
            placeholder="any"
          />
        </label>

        <label className="filters__field">
          <span>Sort</span>
          <select value={sort} onChange={(e) => set('sort', e.target.value)}>
            <option value="date">Newest first</option>
            <option value="oldest">Oldest first</option>
            <option value="amount">Largest first</option>
            <option value="amount-asc">Smallest first</option>
          </select>
        </label>

        {active && (
          <div className="filters__field filters__field--action">
            <span aria-hidden="true">&nbsp;</span>
            <button
              type="button"
              className="button"
              onClick={() => setParams(new URLSearchParams())}
            >
              Clear filters
            </button>
          </div>
        )}
      </div>

      {error && (
        <Notice kind="warn">
          <p>
            No transactions: {describe(error).detail} They are read from the ledger itself,
            so the store has to answer.
          </p>
        </Notice>
      )}

      {isPending && !data && <Skeleton cards={1} rows={12} />}

      {data && (
        <>
          {/* A filtered list that does not say it is filtered is how a figure
              gets read as a total. Always stated, even when nothing is on. */}
          <p className="muted filters__count">
            {active
              ? `${count(data.matched, 'transaction')} of ${data.total} match`
              : count(data.matched, 'transaction')}
            {data.outsideWindow > 0 && ` · ${data.outsideWindow} outside the window`}
            {isAll && ' · combined across accounts'}
          </p>

          {data.rows.length === 0 ? (
            <Card>
              <p className="muted">
                Nothing matches. {active && 'Clear the filters to see everything in the window.'}
              </p>
            </Card>
          ) : (
            <section className="sheet">
              <div className="sheet__scroll">
                {/* §49. Explicit roles because the phone layout changes
                    `display` on every one of these elements, and Blink and
                    WebKit drop a table's implicit roles when it stops being
                    `display: table`. The rows become cards to the eye and stay
                    a table to a screen reader. */}
                <table className="txns" role="table">
                  <caption className="visually-hidden">
                    Transactions matching the current filters, newest first. No running
                    balance: this list can be filtered and reordered, and a balance over a
                    subset would assert a continuity that is not there.
                  </caption>
                  <thead role="rowgroup">
                    <tr role="row">
                      <th scope="col" role="columnheader">Date</th>
                      <th scope="col" role="columnheader">Payee</th>
                      <th scope="col" role="columnheader">Category</th>
                      {isAll && <th scope="col" role="columnheader">Account</th>}
                      <th scope="col" role="columnheader" className="num">
                        Out
                      </th>
                      <th scope="col" role="columnheader" className="num">
                        In
                      </th>
                    </tr>
                  </thead>
                  <tbody role="rowgroup">
                    {data.rows.map((row) => (
                      <tr key={row.id} role="row">
                        <td className="date" role="cell">
                          {formatShortDate(row.date)}
                          {row.time && <span className="txns__time">{formatClock(row.time)}</span>}
                        </td>
                        {/* §100. No tag chips. `family` and `not-earnings`
                            printed on every row they applied to, which on this
                            table is most of them — the operator's words were
                            "remove these caption in transaction table I dont
                            these there". They are still a filter in the strip
                            above, which is where a tag answers a question
                            instead of decorating an answer. */}
                        <td role="cell" data-label="Payee">
                          <span className="party__name">{row.description}</span>
                        </td>
                        <td role="cell" data-label="Category">
                          {row.category ? (
                            <button
                              type="button"
                              className="txns__cat"
                              onClick={() => set('category', row.category)}
                              title={`Filter to ${row.category}`}
                            >
                              {row.category}
                            </button>
                          ) : (
                            <span className="muted">—</span>
                          )}
                        </td>
                        {isAll && (
                          <td className="muted" role="cell" data-label="Account">
                            {row.accountLabel}
                          </td>
                        )}
                        {/* Direction by COLUMN, never by colour (§16.4). */}
                        {/* Direction stays a COLUMN here and becomes a
                            printed word on a phone, where there are no
                            columns. Never a colour. */}
                        <td className="num" role="cell" data-label="Out">
                          {row.kind === 'withdrawal' ? formatAmount(row.amount) : ''}
                        </td>
                        <td className="num" role="cell" data-label="In">
                          {row.kind === 'deposit' ? formatAmount(row.amount) : ''}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {data.pages > 1 && (
            <div className="pager">
              <button
                type="button"
                className="button"
                disabled={data.page <= 1}
                onClick={() => set('page', String(data.page - 1))}
              >
                Previous
              </button>
              <span className="muted">
                Page {data.page} of {data.pages}
              </span>
              <button
                type="button"
                className="button"
                disabled={data.page >= data.pages}
                onClick={() => set('page', String(data.page + 1))}
              >
                Next
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
