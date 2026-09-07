/* Reports — the drill-downs. SPEC §64.
 *
 * Firefly ships Category, Double (expense/revenue account) and Tag as three
 * separate report screens with a controller each. They are **one question
 * asked three ways**: inside one thing, what were the others? So they are one
 * component here, switched by a tab, and all three come out of
 * `ledger_analysis` — which means they carry §8/§8.1's exclusions, and
 * Firefly's own versions do not.
 *
 * Budget is not here and will not be until there are budgets: 0 budgets, 0
 * bills, 0 piggy banks, measured. A report screen that can only ever render
 * empty is worse than a missing one, because it reads as broken.
 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../lib/api'
import type { Analysis, Breakdown } from '../lib/types'
import { Card, Notice } from '../components/ui'
import { Skeleton, Why, describe } from '../components/feedback'
import {
  BalanceLine,
  catColor,
  CategoryBars,
  CategoryHeat,
  Donut,
  MonthColumns,
  topWithOther,
  WeekdayBars,
} from '../components/charts'
import { HourHistogram } from '../components/DayRail'
import { formatINR } from '../lib/money'
import { useAccounts } from '../lib/account'
import { joinQuery, useRange } from '../lib/range'
import { RangePicker } from '../components/RangePicker'

type View = 'category' | 'payee' | 'tag' | 'trend' | 'rhythm'
type Flow = 'out' | 'in'

const VIEWS: Record<Flow, { key: View; label: string; blurb: string; empty: string }[]> = {
  out: [
    {
      key: 'category',
      label: 'By category',
      blurb: 'Inside each category, who the money actually went to.',
      empty: 'No categorised spending in this window.',
    },
    {
      key: 'payee',
      label: 'By payee',
      blurb: 'Inside each counterparty, what the money was for.',
      empty: 'No spending with a named counterparty in this window.',
    },
    {
      key: 'tag',
      label: 'By tag',
      blurb: 'Inside each roll-up tag, which categories make it up.',
      empty: 'No tagged spending in this window. Tags come from rules.yaml.',
    },
    {
      key: 'trend',
      label: 'Over time',
      blurb:
        'Every category month by month, and the shape of the whole window ' +
        'above it. Which categories are growing, and which month was the odd one.',
      empty: 'Only one month in this window — a trend needs at least two.',
    },
    {
      key: 'rhythm',
      label: 'Rhythm',
      blurb: 'When the money moves: by day of the week, and by hour.',
      empty: 'Nothing in this window.',
    },
  ],
  // §68. Reports answered only about money leaving, which is half the ledger.
  // There is no `tag` view here on purpose: the roll-up tags in rules.yaml are
  // all spend groupings, so an income-by-tag screen would render empty and an
  // always-empty screen reads as broken.
  in: [
    {
      key: 'category',
      label: 'By category',
      blurb: 'Inside each income category, who paid you.',
      empty: 'No categorised income in this window.',
    },
    {
      key: 'payee',
      label: 'By source',
      blurb: 'Inside each source, what the money was booked as.',
      empty: 'No income from a named source in this window.',
    },
    {
      key: 'trend',
      label: 'Over time',
      blurb: 'In and out, month by month, on one shared scale.',
      empty: 'Only one month in this window — a trend needs at least two.',
    },
  ],
}

export function Reports() {
  const { param, isAll } = useAccounts()
  const range = useRange()
  const [view, setView] = useState<View>('category')
  const [flow, setFlow] = useState<Flow>('out')
  // Free-text over the outer name, so a long list of counterparties is usable.
  const [find, setFind] = useState('')

  const { data, isPending, error } = useQuery({
    queryKey: ['analysis', param, range.key],
    queryFn: () => api.get<Analysis>(`/analysis${joinQuery(param, range.query)}`),
  })

  const views = VIEWS[flow]
  // Switching to income while on the tag view would leave nothing selected.
  const chosen = views.find((v) => v.key === view) ?? views[0]!
  // One filtered list, used by BOTH the ring and the cards. Computing it twice
  // is how they came to disagree.
  const needle = find.trim().toLowerCase()
  const shown = data
    ? pick(data, flow, chosen.key).filter(
        (r) =>
          !needle ||
          r.name.toLowerCase().includes(needle) ||
          r.parts.some((p) => p.name.toLowerCase().includes(needle)),
      )
    : []

  return (
    <div className="page">
      <h1>Reports</h1>

      <RangePicker
        window={data?.window}
        outside={data?.outsideWindow}
        noun="transaction"
      />

      {/* Direction first, then the dimension inside it. Two rows rather than
          five pills in one, because they are different questions: which half
          of the ledger, and then how to cut it. */}
      <div className="tabs tabs--inline">
        <div className="tabs__inner">
          <button
            type="button"
            className={`tab${flow === 'out' ? ' tab--on' : ''}`}
            onClick={() => setFlow('out')}
            aria-pressed={flow === 'out'}
          >
            Money out
          </button>
          <button
            type="button"
            className={`tab${flow === 'in' ? ' tab--on' : ''}`}
            onClick={() => setFlow('in')}
            aria-pressed={flow === 'in'}
          >
            Money in
          </button>
          <span className="tabs__gap" />
          <label className="filters__search filters__search--tight">
            <span className="visually-hidden">Find</span>
            <input
              type="search"
              value={find}
              onChange={(e) => setFind(e.target.value)}
              placeholder="Find a category, payee or source"
              autoComplete="off"
            />
          </label>
        </div>
      </div>

      <div className="tabs tabs--inline">
        <div className="tabs__inner">
          {views.map((v) => (
            <button
              key={v.key}
              type="button"
              className={`tab${v.key === view ? ' tab--on' : ''}`}
              onClick={() => setView(v.key)}
              aria-pressed={v.key === view}
            >
              {v.label}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <Notice kind="warn">
          <p>No reports: {describe(error).detail}</p>
        </Notice>
      )}
      {isPending && <Skeleton cards={2} rows={6} />}

      {data && (
        <>
          {/* §85. The four figures, side by side and named, because the
              operator asked three times what the difference between them is
              and the app had never put them in one place:

                Money out      every rupee that left
                Actually spent  …less the categories `not_spend` calls movement
                Money in       every rupee that arrived
                Earned         …less the deposits tagged not-earnings

              The pair on each side differ by exactly one exclusion, so showing
              them adjacent is the shortest possible explanation of it. */}
          <div className="kpis">
            <Kpi
              label="Money out"
              value={formatINR(data.grossSpend)}
              note={`${data.withdrawals} withdrawals, everything that left`}
              on={flow === 'out'}
            />
            <Kpi
              label="Actually spent"
              value={formatINR(data.spend)}
              note={
                Number(data.excludedSpendTotal) > 0
                  ? `less ${formatINR(data.excludedSpendTotal)} of movement`
                  : 'nothing excluded as movement'
              }
              on={flow === 'out'}
            />
            <Kpi
              label="Money in"
              value={formatINR(data.grossIncome)}
              note={`${data.deposits} deposits, everything that arrived`}
              on={flow === 'in'}
            />
            <Kpi
              label="Earned"
              value={formatINR(data.income)}
              note={
                Number(data.excludedIncome.amount) > 0
                  ? `less ${formatINR(data.excludedIncome.amount)} coming back`
                  : 'nothing excluded'
              }
              on={flow === 'in'}
            />
          </div>

          <p className="lede">
            {chosen.blurb}
            {isAll && ' Combined across accounts.'}
          </p>

          <div className="swap" key={`${flow}:${chosen.key}`}>
          {/* A ring for whichever half is in view. The Ledger has one for
              spend only; income deserved the same at-a-glance read.
              **Inside the swap and filtered by Find**, both of which it was
              not: typing narrowed the cards below while the ring above kept
              showing everything, so the two halves of the page disagreed
              about what was being looked at. */}
          {/* §89. A ring on EVERY breakdown view, not only the category one. The
              question "what dominates this list" is the same whether the list
              is categories, payees, sources or tags. Six plus Other, because
              past that a ring is a colour wheel rather than a reading. */}
          {['category', 'payee', 'tag'].includes(chosen.key) && shown.length > 1 && (
            <Card>
              <Donut
                slices={topWithOther(
                  shown.map((b) => ({ name: b.name, amount: b.amount, count: b.count })),
                  6,
                )}
                total={shown
                  .reduce((sum, b) => sum + Number(b.amount), 0)
                  .toFixed(2)}
                label={`${chosen.label} — share of what is shown`}
              />
            </Card>
          )}
          {chosen.key === 'trend' ? (
            <>
              <Card>
                <div className="flows">
                  <MonthColumns months={data.months} title="spend" />
                  <MonthColumns months={data.months} title="income" />
                </div>
              </Card>
              {flow === 'out' && data.categoryMonths.length > 0 && data.months.length > 1 && (
                <Card>
                  <CategoryHeat
                    rows={data.categoryMonths.slice(0, 14)}
                    months={data.months}
                  />
                </Card>
              )}
              <Card>
                <BalanceLine series={data.balances} />
              </Card>
            </>
          ) : chosen.key === 'rhythm' ? (
            <>
              <Card title="By day of the week">
                <WeekdayBars amounts={data.weekdaySpend} counts={data.weekdays} />
              </Card>
              <Card title="By hour">
                <HourHistogram
                  hours={data.hours}
                  label="Spending across the day"
                  count={data.counted}
                  height={130}
                  axis
                />
                <span className="railscale railscale--inset" aria-hidden="true">
                  <span>00</span>
                  <span>06</span>
                  <span>12</span>
                  <span>18</span>
                  <span>24</span>
                </span>
              </Card>
            </>
          ) : (
          <Breakdowns
            rows={shown}
            // `grossIncome`, not `income` — see §72. The Donut above already
            // used it, so the same card was showing two denominators.
            of={flow === 'out' ? data.spend : data.grossIncome}
            empty={find.trim() ? `Nothing matches ${find.trim()}.` : chosen.empty}
          />
          )}
          </div>

          <Why label="Why these are not the totals on the statement">
            <p>
              The ledger store counts every withdrawal and every deposit by type. These come
              from the same function every other figure in this app comes from, so
              the categories named in <code>not_spend</code> are left out of the out
              side, and deposits tagged <code>not-earnings</code> are left out of the
              in side — money coming back is not money earned.
            </p>
          </Why>
        </>
      )}
    </div>
  )
}

/** One of the four figures. `on` dims the half you are not looking at, so the
 *  row doubles as a reminder of which side the page is currently showing. */
function Kpi({
  label,
  value,
  note,
  on,
}: {
  label: string
  value: string
  note: string
  on: boolean
}) {
  return (
    <div className={`kpi${on ? '' : ' kpi--off'}`}>
      <span className="kpi__label">{label}</span>
      <span className="kpi__value num">{value}</span>
      <span className="kpi__note">{note}</span>
    </div>
  )
}

/** Which of the five breakdowns this view is asking for. */
function pick(data: Analysis, flow: Flow, view: View): Breakdown[] {
  if (flow === 'in')
    return view === 'category' ? data.sourcesByCategory : data.categoriesBySource
  if (view === 'category') return data.payeesByCategory
  if (view === 'payee') return data.categoriesByPayee
  return data.categoriesByTag
}

/** One card per outer thing, its inner slices as bars. */
function Breakdowns({
  rows,
  of,
  empty,
}: {
  rows: Breakdown[]
  of: string
  empty: string
}) {
  if (rows.length === 0)
    return (
      <Card>
        <p className="muted">{empty}</p>
      </Card>
    )

  return (
    <div className="rollups rollups--wide">
      {rows.map((row, index) => (
        <Card key={row.name}>
          <div className="bd__head">
            {/* The outer thing keeps ITS hue from the chart the reader just
                came from, so a category is the same colour here as on the
                Ledger. Identity has to survive the page change or the hue is
                decoration. */}
            <span
              className="bd__chip"
              style={{ background: catColor(index) }}
              aria-hidden="true"
            />
            <h2>{row.name}</h2>
            <span className="bd__total num">{formatINR(row.amount)}</span>
          </div>
          <div className="bars--compact">
            <CategoryBars slices={row.parts} of={of} />
          </div>
        </Card>
      ))}
    </div>
  )
}
