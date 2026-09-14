/* Payees. SPEC §16.1, D10.
 *
 * The one page where "just autofill a sensible default" would creep in. It
 * does not. The category control lists only categories that already have a
 * rule; there is no free-text field and no suggestion derived from the token
 * text. D10 measured a 40% error rate on inferring meaning from a ~10-char
 * fragment — a dropdown does not improve that number, so the operator decides.
 *
 * The hours column is the Day Rail at aggregate scale. It is the analysis that
 * split Day Canteen from Night Canteen by hand in Phase 4, now permanent.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '../lib/api'
import type {
  AttributionData,
  EarningsData,
  DiffResponse,
  Payees as PayeesData,
  ReapplyPreview,
} from '../lib/types'
import { HourHistogram } from '../components/DayRail'
import { Popover } from '../components/Popover'
import { Money, Notice, Token } from '../components/ui'
import { Skeleton, Why, describe, useToast } from '../components/feedback'
import { useLedgerSync } from '../components/reconcile'
import { formatINR, formatShortDate } from '../lib/money'
import { useAccounts } from '../lib/account'
import { joinQuery, useRange } from '../lib/range'
import { RangePicker } from '../components/RangePicker'

export function Payees() {
  const navigate = useNavigate()
  const toast = useToast()
  const [aliases, setAliases] = useState<Record<string, string>>({})
  const [categories, setCategories] = useState<Record<string, string>>({})

  const { param, scopeLabel } = useAccounts()
  const range = useRange()
  const { data, isPending, error } = useQuery({
    queryKey: ['payees', param, range.key],
    queryFn: () => api.get<PayeesData>(`/payees${joinQuery(param, range.query)}`),
  })

  // §105.2. The drift check costs three round trips to the store and the page
  // waited on it every visit. It is asked for now — except right after a write,
  // when drift is exactly what has just been created and the operator should
  // not have to think to look for it.
  const [askedDrift, setAskedDrift] = useState(false)

  const diff = useMutation({
    mutationFn: () => api.post<DiffResponse>('/payees/diff', { aliases, categories }),
    onSuccess: (response) => {
      setAskedDrift(true)
      navigate('/payees/diff', { state: { response, aliases, categories } })
    },
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
        {scopeLabel && <>{scopeLabel} — </>}
        {data.rows.length} distinct tokens across {data.total} transactions.{' '}
        {undecided > 0
          ? `${undecided} still ${undecided === 1 ? 'needs' : 'need'} a decision — ` +
            `${undecided === 1 ? 'it is' : 'they are'} listed first.`
          : 'Everything is categorised.'}
      </p>

      <RangePicker window={data.window} outside={data.outsideWindow} noun="transaction" />

      <PendingReapply asked={askedDrift} onAsk={() => setAskedDrift(true)} />

      <Why label="Why nothing is guessed here">
        <p>
          Banks truncate the counterparty — about ten characters here — and of ten tokens read
          from the fragment alone, four were wrong — <code>THE CASUA</code> is a clothing
          store, not casual dining. The category list offers only categories that already
          have a rule; it never proposes one.
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
                      {/* §103. "I want split option to be in dropdown category
                          in Payees in another dropdown in just Credit Card" —
                          so it is the second control in this cell, and only on
                          the rows whose category is a settlement one. */}
                      <Split category={categories[row.token] ?? row.category} />
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
          <NewCategory />
          <EarningsPolicy />
          <ManageCategories />
          <button
            type="submit"
            className="primary"
            disabled={diff.isPending}
            data-working={diff.isPending}
          >
            {diff.isPending ? 'Comparing…' : 'Review changes'}
          </button>
        </div>
      </form>

      <Why label="What Review changes does">
        <p>
          Shows a diff first. Nothing is written to <code>config/</code> until you approve it.
        </p>
      </Why>

      <Why label="Reading the hours column">
        <p className="muted">
          Shaded band is midnight to 6am. This is the only place time-of-day exists — the bank
          gives no time column, but {data.totalClocked} of {data.total} rows embed one in the
          narration. It is how Day Canteen and Night Canteen were told apart.
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
 * remind anyone. Now the page that owns the config says so on sight — and
 * fixes it here, because the fix is one non-destructive request.
 *
 * Silent when it cannot ask — an unreachable or unconfigured Firefly is not a
 * problem for the page whose job is editing a yaml file.
 */
function PendingReapply({ asked, onAsk }: { asked: boolean; onAsk: () => void }) {
  const sync = useLedgerSync()
  const { data, isFetching } = useQuery({
    queryKey: ['reapply'],
    queryFn: () => api.get<ReapplyPreview>('/reapply'),
    retry: false,
    // §105.2. Not on sight. Answering "does the ledger still match this config"
    // means reading every row out of the store — measured at three round trips
    // and ~900ms cold — and this page's own data arrives in 61ms. Asking on
    // every visit made the page feel slow for a question that is usually "no".
    //
    // The case §23.1 built this for is unaffected: drift is created by writing
    // config, and the write turns it on.
    enabled: asked,
  })

  if (!asked)
    return (
      <div className="actions actions--right">
        <button type="button" onClick={onAsk} title="Do the rows already pushed still match this config?">
          Check ledger
        </button>
      </div>
    )
  if (isFetching && !data) return <p className="muted">Reading the ledger…</p>
  if (!data) return null
  if (data.changes.length === 0)
    return (
      <p className="muted">
        All {data.considered} row{data.considered === 1 ? '' : 's'} in the ledger match this
        config.
      </p>
    )

  return (
    <Notice kind="warn">
      <p>
        <strong>
          {data.changes.length} transaction{data.changes.length === 1 ? '' : 's'} in the ledger
          {data.changes.length === 1 ? ' does' : ' do'} not match this config
        </strong>{' '}
        — {data.renames} name{data.renames === 1 ? '' : 's'}, {data.recats} categor
        {data.recats === 1 ? 'y' : 'ies'}. Rules and aliases apply at push time, so those rows
        still read the way they were pushed.
      </p>
      <div className="actions">
        <button
          type="button"
          className="primary"
          onClick={() => sync.mutate()}
          disabled={sync.isPending}
          data-working={sync.isPending}
        >
          {sync.isPending ? 'Updating…' : `Update ${data.changes.length} in the ledger`}
        </button>
        <Link className="button" to="/reapply">
          Review the rows
        </Link>
      </div>
    </Notice>
  )
}

/**
 * Create a category, from the page that needs one.
 *
 * **This is not D10 being relaxed.** D10 forbids *inferring* a category from a
 * ten-character fragment — deciding that `THE CASUA` is casual dining when it
 * is a clothing store. Typing one infers nothing: it records a decision the
 * operator has already made. Until now the only way to record it was to
 * hand-edit `rules.yaml`, so the dropdown went stale the moment their spending
 * changed, and the real-world outcome of that is a row left uncategorised.
 *
 * The rule is created **empty**. Nothing is assigned here; the operator then
 * picks it on a row and it goes through diff-then-write like everything else.
 */
/**
 * How much of a card bill belongs to the month it was paid in. SPEC §103.
 *
 * A bill paid on the 5th of August settles July's purchases, so §73 shifts it
 * to the previous month. The operator asked for the exception in the same
 * breath:
 *
 * > "Credit Card I say put in last month but give option to split some amount
 * >  if any in this month"
 *
 * `Attribution.keep` has existed since §73 and there was **no way to fill it
 * in** — it is keyed on a namespaced `external_id`, and setting one meant
 * opening a YAML file and pasting an id into it. That is the terminal this app
 * exists to replace.
 *
 * So it is a dropdown beside the category, on the rows the shift applies to and
 * nowhere else. It lists the operator's own bills — date, payee, amount — and
 * takes a figure per bill. Nothing else on the page can reach this state, which
 * is why it holds its own query rather than being threaded through `/payees`.
 *
 * **It moves no money.** A split changes which month a figure is reported in
 * and nothing else: not the date in the ledger, not the balance chart, not the
 * window, and not one total (non-negotiable 19).
 */
function Split({ category }: { category: string }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const { param } = useAccounts()
  const range = useRange()
  const [draft, setDraft] = useState<Record<string, string>>({})

  // Every figure that buckets by month. Omitting these is the §6b bug exactly:
  // the panel would show the new split and the charts the old one.
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['attribution'] })
    queryClient.invalidateQueries({ queryKey: ['analysis'] })
    queryClient.invalidateQueries({ queryKey: ['reports'] })
  }

  const { data } = useQuery({
    queryKey: ['attribution', param, range.key],
    queryFn: () => api.get<AttributionData>(`/attribution${joinQuery(param, range.query)}`),
    staleTime: 30_000,
  })

  const settle = useMutation({
    mutationFn: (settles: boolean) =>
      api.put<AttributionData>('/attribution/settlement', { category, settles }),
    onSuccess: (result) => {
      toast({
        kind: 'ok',
        title: result.configured ? `${category} settles the previous month` : 'Shift turned off',
        detail: result.configured
          ? `Bills paid on or before day ${data?.beforeDay ?? 10} now count against the month before.`
          : `${category} bills count in the month they were paid.`,
      })
      invalidate()
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  const save = useMutation({
    mutationFn: (body: { externalId: string; keep: string | null }) =>
      api.put<{ keep: string | null }>('/attribution', body),
    onSuccess: (result) => {
      toast({
        kind: 'ok',
        title: result.keep ? 'Split saved' : 'Split cleared',
        detail: result.keep
          ? `${formatINR(result.keep)} stays in the month it was paid.`
          : 'The whole bill settles the previous month.',
      })
      invalidate()
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  // Only on a settlement category, and only once the list has arrived — a
  // control that appears and then turns out to have nothing in it is worse
  // than one that appears a beat late.
  if (!data || !category || !data.categories.includes(category)) return null

  return (
    <Popover
      className="split__button"
      title={`Split ${category} bills between months`}
      panelClassName="split__panel"
      label={`Split${data.rows.some((r) => r.keep) ? ' ·' : ''}`}
    >
      {/* §103.3. The POLICY, on its own control. It applies to every
          qualifying bill, so switching it moves whole months at once — which
          is why it must not be a side effect of typing an amount into one
          box, which is what it was. */}
      <label className="split__policy">
        <input
          type="checkbox"
          checked={settle.isPending ? Boolean(settle.variables) : data.configured}
          disabled={settle.isPending}
          onChange={(e) => settle.mutate(e.target.checked)}
        />
        <span>
          {category} bills settle the <strong>previous</strong> month
          <span className="muted">
            {' '}— paid on or before day {data.beforeDay}, counted on the {data.toDay}
            {ordinal(data.toDay)}
          </span>
        </span>
      </label>
      <p className="muted">
        {data.configured
          ? 'Anything you keep below stays in the month it was paid.'
          : 'Off — every bill counts in the month it was paid. These are the bills it would move.'}
      </p>
      {data.rows.length === 0 ? (
        <p className="muted">
          No {category} payments in this window fall in the first {data.beforeDay} days of a
          month.
        </p>
      ) : (
        data.rows.map((row) => {
          const value = draft[row.externalId] ?? row.keep ?? ''
          return (
            <label key={row.externalId} className="split__row">
              <span className="split__when">
                {formatShortDate(row.date)}
                <span className="muted"> · {formatINR(row.amount)}</span>
                <span className="split__who">{row.payee}</span>
              </span>
              <input
                inputMode="decimal"
                placeholder="0"
                disabled={!data.configured}
                aria-label={`Amount of the ${formatShortDate(row.date)} bill that is this month's`}
                value={value}
                onChange={(e) => setDraft({ ...draft, [row.externalId]: e.target.value })}
                onBlur={() => {
                  const next = (draft[row.externalId] ?? '').trim()
                  if (draft[row.externalId] === undefined) return
                  if (next === (row.keep ?? '')) return
                  save.mutate({ externalId: row.externalId, keep: next || null })
                }}
              />
            </label>
          )
        })
      )}
    </Popover>
  )
}

/** `22` -> `nd`. Small enough to not be worth a dependency. */
function ordinal(day: number): string {
  if (day % 100 >= 11 && day % 100 <= 13) return 'th'
  return ['th', 'st', 'nd', 'rd'][day % 10] ?? 'th'
}

/**
 * What counts as **earned**. SPEC §112.
 *
 * > "Earning definition or any other definition is different for anyone so we
 * >  cant generalize instead give an option i guess"
 *
 * Right, and the shape of the rule is why it mattered. `not_earnings` is an
 * allow-list: `earnings_only` names what counts and everything else arriving is
 * tagged `not-earnings`. Safe against over-counting, and it means a freshly
 * registered account reports **₹0 earned** against real deposits — measured on
 * one, six deposits in, every one excluded.
 *
 * A refund is not income for anybody. Money from a parent is income for some
 * people and a transfer for others. So the list is offered here rather than
 * decided anywhere, and only for categories that have actually received money —
 * a category that has never taken a deposit is not a decision to make.
 */
function EarningsPolicy() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const { param } = useAccounts()
  const range = useRange()

  const { data } = useQuery({
    queryKey: ['earnings', param, range.key],
    queryFn: () => api.get<EarningsData>(`/earnings${joinQuery(param, range.query)}`),
    staleTime: 30_000,
  })

  const set = useMutation({
    mutationFn: (body: { category: string; counts: boolean }) =>
      api.put<{ counted: string[] }>('/earnings', body),
    onSuccess: () => {
      toast({ kind: 'ok', title: 'Saved', detail: 'Earnings updated.' })
      queryClient.invalidateQueries({ queryKey: ['earnings'] })
      queryClient.invalidateQueries({ queryKey: ['analysis'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  if (!data || data.rows.length === 0) return null
  const uncategorised = data.rows.find((r) => r.category === '(no category)')

  return (
    <Popover
      className="split__button"
      title="What counts as earnings"
      panelClassName="split__panel"
      label="What counts as earned"
    >
      <p className="muted">
        Money that arrived, by category. Tick what you count as earned — everything else
        is money coming back.
      </p>
      {uncategorised && (
        <p className="muted">
          {formatINR(uncategorised.amount)} arrived with no category. Give those payees one
          in the table above first; an uncategorised deposit has nothing to count as.
        </p>
      )}
      {data.rows
        .filter((row) => row.category !== '(no category)')
        .map((row) => (
          <label key={row.category} className="split__row">
            <span className="split__when">
              {row.category}
              <span className="muted"> · {formatINR(row.amount)}</span>
            </span>
            <input
              type="checkbox"
              checked={
                set.isPending && set.variables?.category === row.category
                  ? Boolean(set.variables?.counts)
                  : row.counts
              }
              disabled={set.isPending}
              onChange={(e) => set.mutate({ category: row.category, counts: e.target.checked })}
            />
          </label>
        ))}
    </Popover>
  )
}


function NewCategory() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [open, setOpen] = useState(false)

  const create = useMutation({
    mutationFn: () => api.post<{ categories: string[] }>('/categories', { name }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['payees'] })
      setName('')
      setOpen(false)
      toast({
        kind: 'ok',
        title: 'Category added',
        detail: `${result.categories.length} to choose from. Pick it on a row, then Review changes.`,
      })
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  if (!open)
    return (
      <button type="button" onClick={() => setOpen(true)}>
        New category
      </button>
    )

  return (
    <span className="field-row">
      <input
        autoFocus
        value={name}
        placeholder="Category name"
        aria-label="New category name"
        onKeyDown={(event) => {
          // Inside a <form> whose submit runs the diff — Enter here must create
          // the category, not jump to reviewing changes that do not exist yet.
          if (event.key === 'Enter') {
            event.preventDefault()
            if (name.trim()) create.mutate()
          }
          if (event.key === 'Escape') setOpen(false)
        }}
        onChange={(event) => setName(event.target.value)}
      />
      <button
        type="button"
        className="primary"
        disabled={!name.trim() || create.isPending}
        data-working={create.isPending}
        onClick={() => create.mutate()}
      >
        {create.isPending ? 'Adding…' : 'Add'}
      </button>
      <button type="button" onClick={() => setOpen(false)}>
        Cancel
      </button>
    </span>
  )
}

/**
 * Remove a category, for the typo that would otherwise be permanent.
 *
 * The asymmetry was the bug: the page could create one and nothing could take
 * one away. Three `E2E Probe` rules accumulated in the operator's own
 * `rules.yaml` before anyone noticed.
 *
 * Only ever offers the **empty** ones. A category with payees is not deleted
 * here and is not deleted by the server either — removing the rule does not
 * remove them, it strands them, and the rows quietly fall to uncategorised on
 * the next push with nothing saying so. The list makes that a non-choice
 * instead of an error to read.
 */
function ManageCategories() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)

  const { data } = useQuery({
    queryKey: ['categories', 'removable'],
    queryFn: () => api.get<{ removable: string[] }>('/categories/removable'),
    enabled: open,
  })

  const remove = useMutation({
    mutationFn: (name: string) =>
      api.del<{ categories: string[] }>(`/categories/${encodeURIComponent(name)}`),
    onSuccess: (_result, name) => {
      queryClient.invalidateQueries({ queryKey: ['payees'] })
      queryClient.invalidateQueries({ queryKey: ['categories'] })
      toast({ kind: 'ok', title: 'Removed', detail: `${name} is gone.` })
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  if (!open)
    return (
      <button type="button" onClick={() => setOpen(true)}>
        Remove a category
      </button>
    )

  const removable = data?.removable ?? []

  return (
    <span className="field-row">
      {removable.length === 0 ? (
        <span className="muted">
          {data
            ? 'Every category has payees assigned. Move them first.'
            : 'Checking…'}
        </span>
      ) : (
        removable.map((name) => (
          <button
            key={name}
            type="button"
            className="chipbtn"
            disabled={remove.isPending}
            onClick={() => remove.mutate(name)}
          >
            {name} ✕
          </button>
        ))
      )}
      <button type="button" onClick={() => setOpen(false)}>
        Done
      </button>
    </span>
  )
}
