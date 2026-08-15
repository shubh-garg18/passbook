/* The Ledger — the overview. SPEC §18.
 *
 * Phase 13 turned this from "balance, sync age, list of files" into the page the
 * project exists to produce. Three things it now carries that it did not:
 *
 * 1. **Charts that respect the exclusion semantics.** Every figure here comes
 *    from `service.ledger_analysis`, which applies §8 and §8.1. Measured on one
 *    real three-month ledger, the naive by-type reading was three times the true
 *    spend and 1.6 times the true earnings — and a chart of the naive numbers
 *    looks perfectly reasonable.
 * 2. **The Status page, as a strip.** Monitoring belongs where the operator
 *    already looks. The strip carries the states; the page it links to keeps the
 *    artefact tables that will not fit in a strip.
 * 3. **The Day Rail at ledger scale.** It was the signature element and existed
 *    only per-payee, which is the one place it explains the least.
 */

import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { api } from '../lib/api'
import type { Analysis, Overview, Status } from '../lib/types'
import { HourHistogram } from '../components/DayRail'
import { CategoryBars, FlowBar, MonthColumns, StackedBar } from '../components/charts'
import { Card, Cross, Money, Notice, Tick } from '../components/ui'
import { Skeleton, Why, describe } from '../components/feedback'
import { count, formatINR } from '../lib/money'
import { useAccounts } from '../lib/account'

const STATE_TO_CARD = { never: 'warn', ok: undefined, warn: 'warn', stale: 'bad' } as const

export function Home() {
  // Every query is keyed on the account as well as the endpoint, so switching
  // accounts refetches instead of showing the previous one's figures under a new
  // name — which would be the §19 failure mode again: plausible and wrong.
  const { param, isAll, label } = useAccounts()
  const { data, isPending, error } = useQuery({
    queryKey: ['overview', param],
    queryFn: () => api.get<Overview>(`/overview${param}`),
  })

  if (isPending)
    return (
      <div className="page">
        <h1>Ledger</h1>
        <Skeleton cards={2} rows={4} />
      </div>
    )
  if (error)
    return (
      <div className="page">
        <h1>Ledger</h1>
        <p className="lede">{describe(error).detail}</p>
      </div>
    )

  return (
    <div className="page">
      <h1>Ledger</h1>
      {label && <p className="lede">{label}</p>}

      <div className="cards">
        <Card title={isAll ? 'Balance, summed' : 'Balance'} state={data.balance ? undefined : 'bad'}>
          <p className="figure">
            {data.balance ? <Money value={data.balance} /> : 'unavailable'}
          </p>
          {/* The sum is a true figure — it is what these accounts hold together
              — but unlike a single account's balance it reconciles against no
              statement, and that reconciliation is what this card has implied
              since Phase 7. So it is labelled, and the parts are shown. */}
          {data.parts.length > 1 && (
            <ul className="parts">
              {data.parts.map((part) => (
                <li key={part.slug}>
                  <span>{part.label}</span>
                  <span className="num">{formatINR(part.balance)}</span>
                </li>
              ))}
            </ul>
          )}
          {/* Which account this is belongs in the cover, once, on every page.
              It was here AND in a page subtitle AND in the header — three
              statements of one fact on a 390px screen. Only the error, which
              is genuinely new information, stays. */}
          {data.fireflyError && <p className="muted">{data.fireflyError}</p>}
        </Card>

        <Card title="Last sync" state={STATE_TO_CARD[data.sync.state]}>
          <p className="figure">{data.sync.age !== null ? `${data.sync.age}d` : 'never'}</p>
          {/* The headline repeats the age in words and then the filename. The
              figure already says the age, so only the filename is new. */}
          <p className="muted">{data.sync.filename ?? 'nothing archived yet'}</p>
        </Card>
      </div>

      <StatusStrip />

      {data.sync.detail && (
        <Notice kind={data.sync.state === 'stale' ? 'bad' : 'warn'}>
          <p>{data.sync.detail}</p>
        </Notice>
      )}

      {data.pending && (
        <Notice kind="warn">
          <p>
            A statement is staged and not yet pushed.{' '}
            <Link to="/preview">Review and push it</Link>.
          </p>
        </Notice>
      )}

      <Charts />

      <h2 className="section">Recently archived</h2>
      <section className="sheet">
        <div className="sheet__scroll">
          <table>
            <caption className="visually-hidden">Recently archived statements</caption>
            <thead>
              <tr>
                <th scope="col">Statement</th>
                <th scope="col">Archived</th>
                <th scope="col">Folder</th>
              </tr>
            </thead>
            <tbody>
              {data.history.length === 0 && (
                <tr>
                  <td colSpan={3} className="muted">
                    Nothing archived yet. A file lands here only after a fully successful push.
                  </td>
                </tr>
              )}
              {data.history.map((h) => (
                <tr key={h.name + h.when}>
                  <td>
                    <span className="tok">{h.name}</span>
                  </td>
                  <td className="date">{h.when}</td>
                  <td className="date">{h.folder}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <div className="actions">
        <Link className="button button--primary" to="/upload">
          Upload a statement
        </Link>
      </div>
    </div>
  )
}

/**
 * Status, folded in. SPEC §18.
 *
 * Its own query, not part of `/overview`: this one calls Firefly for its version
 * and shells out to rclone for the off-site listing, and the balance must not
 * wait behind either. A failure here dims the strip and nothing else — the page
 * this sits on is the ledger, not the monitor.
 */
function StatusStrip() {
  const { param } = useAccounts()
  const { data, isPending, error } = useQuery({
    queryKey: ['status', param],
    queryFn: () => api.get<Status>(`/status${param}`),
  })

  if (isPending) return <div className="strip strip--loading" aria-hidden="true" />
  if (error)
    return (
      <p className="muted strip__error">
        Status unavailable — {describe(error).detail} <Link to="/status">Details</Link>
      </p>
    )

  const tokenBad = !data.token.shapeOk
  const tokenWarn = data.token.daysLeft !== null && data.token.daysLeft <= 30
  const backupWarn =
    data.backups.ageDays === null || data.backups.ageDays > data.backups.staleDays
  const codesWarn = data.auth.backupCodesLow

  return (
    <div className="strip">
      {/* First, and deliberately: this is the check that would have caught the
          2026-08-11 incident (§19, §20). Everything else on this strip passed
          while the ledger held 21 of 93 rows. */}
      <span
        className={`strip__item${
          data.ledger.ok === false ? ' bad strip__item--wide' : data.ledger.ok === null ? ' warn' : ''
        }`}
        title={data.ledger.checks.map((c) => `${c.name}: ${c.detail}`).join('\n')}
      >
        {data.ledger.ok === true ? <Tick title="" /> : data.ledger.ok === false ? <Cross title="" /> : null}{' '}
        Ledger {data.ledger.ok === false ? data.ledger.headline : data.ledger.ok === null ? 'unverified' : 'verified'}
      </span>
      <span className={`strip__item${data.firefly.about ? '' : ' bad'}`}>
        {data.firefly.about ? <Tick title="" /> : <Cross title="" />} Firefly{' '}
        {data.firefly.about ? `v${data.firefly.about.version}` : 'unreachable'}
      </span>
      <span className={`strip__item${tokenBad ? ' bad' : tokenWarn ? ' warn' : ''}`}>
        Token{' '}
        {tokenBad
          ? 'bad shape'
          : data.token.daysLeft === null
            ? 'expiry unknown'
            : `${data.token.daysLeft}d left`}
      </span>
      <span className={`strip__item${backupWarn ? ' warn' : ''}`}>
        Backup {data.backups.ageDays === null ? 'none' : `${data.backups.ageDays}d old`}
      </span>
      <span className={`strip__item${codesWarn ? ' warn' : ''}`}>
        {count(data.auth.backupCodesLeft, 'backup code')} left
      </span>
      {data.drift.length > 0 && (
        <span className="strip__item warn">{data.drift.length} alias drift</span>
      )}
      <Link className="strip__more" to="/status">
        Details
      </Link>
    </div>
  )
}

/** Everything drawn from `/api/analysis`, for the account in scope.
 *
 * **All accounts combines every chart on this page**, and that is the correct
 * reading rather than a convenience: spend, income, the category breakdown, the
 * roll-ups, the month buckets and the Day Rail are each a sum over transactions,
 * and §8/§8.1's exclusions are decided per transaction — so combining cannot
 * change what any figure means. Time of day is a property of the person, not of
 * the account, which makes the combined Day Rail the more useful of the two
 * readings. The balance is the one figure that does NOT combine cleanly; it is
 * summed, labelled as a sum, and shown with its parts. */
function Charts() {
  const { param, isAll } = useAccounts()
  const { data, isPending, error } = useQuery({
    queryKey: ['analysis', param],
    queryFn: () => api.get<Analysis>(`/analysis${param}`),
  })

  if (isPending)
    return (
      <>
        <h2 className="section">Where it went</h2>
        <Skeleton cards={2} rows={8} />
      </>
    )
  if (error)
    return (
      <>
        <h2 className="section">Where it went</h2>
        <Notice kind="warn">
          <p>
            No charts: {describe(error).detail} They are drawn from the ledger itself, so
            Firefly has to answer.
          </p>
        </Notice>
      </>
    )

  const complete = data.months.filter((m) => !m.partial).length
  const partial = data.months.filter((m) => m.partial).map((m) => m.month)
  // One scale for both flow bars, so "earned" and "spent" are comparable
  // without a second axis to read.
  const scale =
    Number(data.grossIncome) > Number(data.grossSpend) ? data.grossIncome : data.grossSpend

  return (
    <>
      <h2 className="section">In and out</h2>
      <Card>
        <div className="flows">
          <FlowBar
            label="Earned"
            counted={data.income}
            gross={data.grossIncome}
            excluded={data.excludedIncome.amount}
            scale={scale}
            excludedLabel={`money coming back, not earned (${data.excludedIncome.count} deposits)`}
            emptyLabel="nothing here is money coming back"
          />
          <FlowBar
            label="Spent"
            counted={data.spend}
            gross={data.grossSpend}
            excluded={data.excludedSpendTotal}
            scale={scale}
            /* `not_spend` is empty until the operator names their own
               categories (D10 ships none), which is every fresh install — so
               the join produced a dangling em-dash pair with nothing between
               it. Visible only in a screenshot of a new ledger. */
            excludedLabel={`${data.notSpend.join(', ')} — movement, not spending`}
            emptyLabel="name your movement categories in not_spend, in config/rules.yaml"
          />
        </div>
        <Why label="Why these are not the totals on the statement">
          <p>
            Firefly counts every deposit as income and every withdrawal as spend, by type.
            Read that way this ledger says {formatINR(data.grossSpend)} spent and{' '}
            {formatINR(data.grossIncome)} earned. Both are wrong, and not by a little.
          </p>
          <p>
            Money moving is not money leaving: {data.notSpend.join(', ')} are excluded from
            spend. Money coming back is not money earned: family support, repayments, refunds
            and penny-drop verifications carry the <code>not-earnings</code> tag, so earnings
            are Salary and Interest Income and nothing else. The hatched part of each bar is
            exactly what those two rules removed.
          </p>
          {Number(data.refunds.amount) > 0 && (
            <p>
              {data.refunds.count === 1 ? 'One refund' : `${data.refunds.count} refunds`} of{' '}
              {formatINR(data.refunds.amount)} sits in the excluded deposits. It is a spend
              coming back, so strictly it also reduces the spend figure; it is left in place
              rather than netted, because netting a refund against a month it did not happen in
              is the more misleading of the two.
            </p>
          )}
        </Why>
      </Card>

      <h2 className="section">Where it went</h2>
      <Card>
        <CategoryBars slices={data.categories} of={data.spend} />
        <p className="muted chart__note">
          {data.categories.length} categories, {formatINR(data.spend)} of real spend. Shading is
          rank, not identity — the darkest bar is the largest.
          {Number(data.uncategorised.amount) > 0 && (
            <>
              {' '}
              {data.uncategorised.count} row
              {data.uncategorised.count === 1 ? '' : 's'} worth{' '}
              {formatINR(data.uncategorised.amount)} still{' '}
              {data.uncategorised.count === 1 ? 'has' : 'have'} no category —{' '}
              <Link to="/payees">decide on Payees</Link>.
            </>
          )}
        </p>
        {data.excludedSpend.length > 0 && (
          <Why label={`What ${formatINR(data.excludedSpendTotal)} of excluded movement was`}>
            <ul className="plain">
              {data.excludedSpend.map((s) => (
                <li key={s.name}>
                  {s.name} — {formatINR(s.amount)} across {s.count} row
                  {s.count === 1 ? '' : 's'}
                </li>
              ))}
            </ul>
          </Why>
        )}
      </Card>

      {data.rollups.map((rollup) => (
        <div key={rollup.tag}>
          <h2 className="section">{rollup.tag}</h2>
          <Card>
            <p className="figure figure--small">{formatINR(rollup.amount)}</p>
            <p className="muted chart__note">
              across {rollup.count} transactions, {rollup.parts.length} categories. The total is
              the <code>{rollup.tag}</code> tag as Firefly stored it; the segments are the
              categories that carry that tag in <code>rules.yaml</code>.
            </p>
            <StackedBar parts={rollup.parts} total={rollup.amount} label={rollup.tag} />
          </Card>
        </div>
      ))}

      <h2 className="section">By month</h2>
      <Card>
        <div className="flows">
          <MonthColumns months={data.months} title="spend" />
          <MonthColumns months={data.months} title="income" />
        </div>
        {/* Every count here is pluralised properly, because the degenerate cases
            are real: a freshly restored ledger has ONE bucket, and "a slope
            through 1 points, 1 of them stubs" is what template concatenation
            produces. Seen in a screenshot of a half-restored ledger. */}
        <p className="muted chart__note">
          Both charts share one scale. {count(complete, 'complete month')} of{' '}
          {data.months.length}
          {partial.length > 0 &&
            ` — ${partial.join(' and ')} ${partial.length === 1 ? 'is' : 'are'} partial, ` +
              'marked with a dashed cap'}
          . <strong>No trend line is drawn</strong>, and will not be until there are enough
          complete months to support one: a slope through {count(data.months.length, 'point')}, of
          which {partial.length === 1 ? 'one is a stub' : `${partial.length} are stubs`}, is the
          most persuasive way to be wrong.
        </p>
      </Card>

      <h2 className="section">The day</h2>
      <Card>
        <HourHistogram
          hours={data.hours}
          label="Spending across the day"
          count={data.counted}
          height={110}
        />
        <span className="railscale" aria-hidden="true">
          <span>00</span>
          <span>06</span>
          <span>12</span>
          <span>18</span>
          <span>24</span>
        </span>
        <p className="muted chart__note">
          Every row that counts as spend, by hour. {data.clocked} of {data.counted} carry a
          clock; the shaded band is midnight to 6am.
          {isAll && ' Combined across accounts — time of day is a property of you, not of an account.'}
        </p>
        <Why label="Where the time comes from">
          <p>
            The bank gives no time column. It is parsed out of the UPI narration (§6.5), which
            means it exists in the statement and nowhere else — Firefly is never told what time
            of day anything happened. NEFT, bank charges, scheme debits and interest carry no
            clock at all, which is why the two numbers above differ.
          </p>
        </Why>
      </Card>
    </>
  )
}
