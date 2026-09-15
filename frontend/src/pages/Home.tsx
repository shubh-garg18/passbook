/* The Ledger — the overview. SPEC §18.
 *
 * Phase 13 turned this from "balance, sync age, list of files" into the page the
 * project exists to produce. Three things it now carries that it did not:
 *
 * 1. **Charts that respect the exclusion semantics.** Every figure here comes
 *    from `service.ledger_analysis`, which applies §8 and §8.1. Measured on one
 *    real three-month ledger, the naive by-type reading was three times the
 *    true spend and 1.6 times the true earnings. A chart of the naive numbers
 *    would be three times wrong and look perfectly reasonable.
 * 2. **The Status page, as a strip.** Monitoring belongs where the operator
 *    already looks. The strip carries the states; the page it links to keeps the
 *    artefact tables that will not fit in a strip.
 * 3. **The Day Rail at ledger scale.** It was the signature element and existed
 *    only per-payee, which is the one place it explains the least.
 */

import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { api } from '../lib/api'
import type { Analysis, Overview, Status, SyncStatus, UpdateState } from '../lib/types'
import {
  BalanceLine,
  CategoryBars,
  Donut,
  FlowBar,
  topWithOther,
} from '../components/charts'
import { Card, Cross, Notice } from '../components/ui'
import { Skeleton, Why, describe } from '../components/feedback'
import { count, formatINR } from '../lib/money'
import { Counter } from '../components/Counter'
import { useAccounts } from '../lib/account'
import { joinQuery, useRange } from '../lib/range'
import { RangePicker } from '../components/RangePicker'

export function Home() {
  // Every query is keyed on the account as well as the endpoint, so switching
  // accounts refetches instead of showing the previous one's figures under a new
  // name — which would be the §19 failure mode again: plausible and wrong.
  const { param, isAll, scopeLabel, accounts, isPending: accountsPending } = useAccounts()
  const { data, isPending, error } = useQuery({
    queryKey: ['overview', param],
    queryFn: () => api.get<Overview>(`/overview${param}`),
  })

  // **A fresh install gets a welcome, not a fault report.**
  //
  // This page used to open, on somebody's very first sign-in, with `BALANCE
  // unavailable` in red, `Ledger unverified ✗ No backup`, and the sentence
  // "Set the missing value in .env on the host, then reload." Every one of
  // those was technically accurate and all three were wrong: nothing is
  // broken, nothing needs editing, and the only thing to do is upload a
  // statement. A person who has done nothing yet should not be shown a
  // diagnosis of having done nothing.
  if (!accountsPending && accounts.length === 0) return <FirstRun />

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
      <Masthead data={data} isAll={isAll} scope={scopeLabel} />

      <StatusStrip />

      {/* §113.1. The staleness warning is NOT here any more.
          §109.1 shortened it and moved its reasoning behind a disclosure and
          the operator's answer was the same both times: it does not look good
          on the Ledger. Two rounds of restyling a thing that should not be
          there is the signal that it should not be there.

          It is a *status*, and this page already has a strip for those — one
          that renders only problems and nothing when all is well (§18). So the
          sync age is a chip in that strip, beside the token and the backups,
          and the page opens on the balance. */}

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
 * The masthead. SPEC §39.
 *
 * This is the passbook's own front matter, and it replaces two equal cards —
 * `Balance` and `Last sync` — sitting side by side as if they were two readings
 * of the same kind. They are not. One is the number the app is opened for; the
 * other is a date, and in a passbook a date is not a figure at all, it is
 * something a teller *stamps* on the page.
 *
 * So the balance is set large on the ruled stock and the sync is a stamp beside
 * it, angled the way a hand-held stamp lands. The palette has carried
 * `.impress` since Phase 26 and this is the first place it says something the
 * page would otherwise have had to spell out.
 *
 * **The stamp prints a date, never a verdict.** "Synced" would be a claim about
 * The ledger, and whether the ledger is right is §20's question, answered by
 * `verify-ledger` and reported by the strip below with its tri-state intact
 * (non-negotiable 11). The date is a fact about a file in `archive/`, which is
 * all this has ever known. Its ink follows the escalation tiers that already
 * exist: `--ochre` asks, `--alarm` is losing data.
 */
function Masthead({
  data,
  isAll,
  scope,
}: {
  data: Overview
  isAll: boolean
  scope: string | null
}) {
  return (
    <header className="masthead">
      <div className="masthead__main">
        {/* The page's `h1`, and it is not the word "Ledger".
            A masthead reading ₹5,068.09 does not need a caption saying which
            page it is — the nav already marks that — but the document still
            needs a top-level heading, and a screen reader announcing "Balance,
            heading level 1" followed by the figure is the page's actual lede. */}
        <h1 className="masthead__label">
          {isAll ? 'Balance, summed' : 'Balance'}
          {scope && <span className="masthead__scope"> · {scope}</span>}
        </h1>
        <p className="masthead__figure">
          {data.balance ? (
            // Counting is the passbook equivalent of a teller thumbing to the
            // last page. `formatINR` takes the decimal STRING; `toFixed(2)`
            // reproduces it exactly on the final frame, and the frames before
            // it are display-only.
            <Counter value={data.balance} format={(n) => formatINR(n.toFixed(2))} />
          ) : (
            <span className="masthead__missing">unavailable</span>
          )}
        </p>
        {/* The sum is a true figure — it is what these accounts hold together —
            but unlike one account's balance it reconciles against no statement,
            and that reconciliation is what a balance has implied since Phase 7.
            So it is labelled, and the parts are shown. */}
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
        {data.ledgerError && <p className="muted">{data.ledgerError}</p>}
      </div>

      <SyncStamp sync={data.sync} />
    </header>
  )
}

const STAMP_TONE = { never: 'bad', ok: '', warn: 'warn', stale: 'bad' } as const

/** The teller's date stamp: what was last recorded, and when. */
function SyncStamp({ sync }: { sync: SyncStatus }) {
  const tone = STAMP_TONE[sync.state]
  return (
    <div className="masthead__stamp">
      <div className={`stamp${tone ? ` stamp--${tone}` : ''}`}>
        <p className="stamp__caption">Last recorded</p>
        <p className="stamp__date">{sync.date ? stampDate(sync.date) : '— — —'}</p>
        <p className="stamp__age">
          {sync.age === null
            ? 'nothing pushed yet'
            : sync.age === 0
              ? 'today'
              : `${sync.age} days ago`}
        </p>
      </div>
      {/* Outside the die, and level: a stamp carries a date, a filing note
          carries the evidence for it. */}
      {sync.filename && <p className="masthead__file">{sync.filename}</p>}
    </div>
  )
}

const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']

/** `2026-08-25` → `25 AUG 26`, the way a rubber date stamp is set.
 *
 * Falls back to the string it was given rather than to a guess: a date the
 * server sent in a shape this did not expect should look wrong on the page, not
 * be quietly rendered as some other day. */
function stampDate(iso: string): string {
  const [year, month, day] = iso.split('-')
  if (!year || !month || !day) return iso
  return `${day} ${MONTHS[Number(month) - 1] ?? month} ${year.slice(2)}`
}

/**
 * The first screen, before there is anything to show.
 *
 * Deliberately not the Ledger page with every figure reading "unavailable".
 * Nothing is wrong on a fresh install; there is simply one thing to do, and
 * saying so is the whole job of this component.
 */
function FirstRun() {
  return (
    <div className="page">
      <h1>Welcome</h1>
      <p className="lede">
        Nothing here yet. Upload one bank statement and passbook does the rest.
      </p>

      <Card title="How this works">
        <ol className="steps">
          <li>
            <strong>Download a statement</strong> from your bank's net banking —
            a spreadsheet (<code>.xls</code>, <code>.xlsx</code>, <code>.csv</code>)
            or a PDF. Three months is a good first one.
          </li>
          <li>
            <strong>Drop it on Upload.</strong> passbook reads it, checks the
            arithmetic against the running balance, and shows you what it found
            <em> before</em> anything is saved.
          </li>
          <li>
            <strong>Name your payees</strong> on Payees when you are ready. Banks
            shorten them to about ten characters, so only you know who they are —
            name one once and it sticks, on rows you already have as well as new
            ones.
          </li>
        </ol>
        <div className="actions">
          <Link className="button button--primary" to="/upload">
            Upload a statement
          </Link>
        </div>
      </Card>

      <Why label="Never upload a statement to an online converter">
        <p>
          It carries your account number, your address, and the name, bank and
          account of everyone you have paid. passbook reads it on this machine
          and sends it nowhere — that is most of the point of it existing.
        </p>
      </Why>

      <Why label="Which banks work">
        <p>
          Canara, SBI and Union Bank are read without any setup. If yours is not
          one of them, <Link to="/banks/add">Add a bank</Link> shows you your own
          file and asks which column is which — about ten minutes, on your
          machine, and the balance check tells you whether you got it right.
        </p>
      </Why>
    </div>
  )
}

/**
 * Status, folded in — and folded away when there is nothing to say. SPEC §18.
 *
 * It used to print five chips on every load: ledger verified, store version,
 * token days left, backup age, backup codes left. All five were true and four
 * of them were noise, and a row of permanently-green chips is training to
 * ignore the row — which is exactly the row the incident needed someone to
 * read.
 *
 * So it renders **only problems**. All clear shows nothing, and the Status page
 * keeps the full detail one click away. The tri-state survives intact:
 * `ledger.ok === null` is *unverified*, which is a problem and appears, because
 * a green tick for something never checked is the thing this project exists to
 * not do (non-negotiable 11).
 *
 * Its own query, not part of `/overview`: this one shells out to rclone for
 * the off-site listing, and the balance must not wait behind it.
 */
/** Longer than this and an item takes its own line. Roughly the width at which
 *  two items no longer fit side by side on a laptop. */
const WIDE_AT = 44

function StatusStrip() {
  const { param } = useAccounts()
  const { data, isPending, error } = useQuery({
    queryKey: ['status', param],
    queryFn: () => api.get<Status>(`/status${param}`),
  })
  // Separate, and allowed to fail: it asks GitHub, and the strip must render
  // whether or not the machine has internet.
  const { data: update } = useQuery({
    queryKey: ['update'],
    queryFn: () => api.get<UpdateState>('/update'),
    retry: false,
    staleTime: 60 * 60 * 1000,
  })

  // Silent while loading. A skeleton for a strip that is usually empty is a
  // flash of furniture that then disappears.
  if (isPending) return null
  if (error)
    return (
      <p className="muted strip__error">
        Status unavailable — {describe(error).detail} <Link to="/status">Details</Link>
      </p>
    )

  const problems: { key: string; bad?: boolean; text: string; title?: string }[] = []

  if (data.ledger.ok !== true) {
    problems.push({
      key: 'ledger',
      bad: data.ledger.ok === false,
      text: `Ledger ${data.ledger.ok === false ? data.ledger.headline : 'unverified'}`,
      title: data.ledger.checks.map((c) => `${c.name}: ${c.detail}`).join('\n'),
    })
  }
  if (data.store.error !== null) {
    problems.push({ key: 'store', bad: true, text: 'The ledger is unreachable' })
  }
  if (update?.behind) {
    // Not `bad`: a newer version existing is not a fault, and painting it red
    // would train the same "ignore the strip" reflex the strip exists to avoid.
    problems.push({ key: 'update', text: 'A new version is available' })
  }
  if (data.backups.ageDays === null) {
    problems.push({ key: 'backup', bad: true, text: 'No backup' })
  } else if (data.backups.ageDays > data.backups.staleDays) {
    problems.push({ key: 'backup', text: `Backup ${data.backups.ageDays}d old` })
  }
  if (data.auth.backupCodesLow) {
    problems.push({
      key: 'codes',
      text: `${count(data.auth.backupCodesLeft, 'backup code')} left`,
    })
  }

  if (problems.length === 0) return null

  return (
    <div className="strip">
      {problems.map((problem) => (
        <span
          key={problem.key}
          // `--wide` exists precisely for this and the Ledger's strip was not
          // using it: `verify-ledger`'s headline names the account, the fault
          // and the remedy in one sentence, and at `nowrap` it ran off the right
          // edge of the card — a detail you cannot read pretending to be one you
          // can. Measured in a two-account screenshot. Length decides, so a
          // short failure like "No backup" still sits inline where it belongs.
          className={
            `strip__item ${problem.bad ? 'bad' : 'warn'}` +
            (problem.text.length > WIDE_AT ? ' strip__item--wide' : '')
          }
          title={problem.title}
        >
          {problem.bad ? <Cross title="" /> : null} {problem.text}
        </span>
      ))}
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
  const { param } = useAccounts()
  const range = useRange()
  const { data, isPending, error } = useQuery({
    queryKey: ['analysis', param, range.key],
    queryFn: () => api.get<Analysis>(`/analysis${joinQuery(param, range.query)}`),
  })

  // The picker renders in every state, loading included: changing the window is
  // the reason you are waiting, and a control that disappears while its own
  // request is in flight cannot be corrected.
  if (isPending)
    return (
      <>
        <RangePicker noun="transaction" />
        <h2 className="section">Where it went</h2>
        <Skeleton cards={2} rows={8} />
      </>
    )
  if (error)
    return (
      <>
        <RangePicker noun="transaction" />
        <h2 className="section">Where it went</h2>
        <Notice kind="warn">
          <p>
            No charts: {describe(error).detail} They are drawn from the ledger itself, so
            the store has to answer.
          </p>
        </Notice>
      </>
    )

  // §112. Every deposit excluded, and there were deposits to exclude — which
  // means the earnings allow-list has not been told about this account's
  // payees, not that nothing was earned. Exact rather than a heuristic: it is
  // the same count on both sides.
  const unclassifiedIncome =
    data.deposits > 0 && data.excludedIncome.count === data.deposits

  // One scale for both flow bars, so "earned" and "spent" are comparable
  // without a second axis to read.
  const scale =
    Number(data.grossIncome) > Number(data.grossSpend) ? data.grossIncome : data.grossSpend

  return (
    <>
      <RangePicker window={data.window} outside={data.outsideWindow} noun="transaction" />

      {/* §58. What the page is about, before any chart. It used to go straight
          from the balance into analysis with nothing summarising the window.
          Every figure comes from the same `ledger_analysis` response the charts
          below use, so the row cannot disagree with them (non-negotiable 9) —
          and none of it is coloured by direction, because a KPI row is the most
          tempting place in the app to paint spending red (§16.4). */}
      <div className="kpis">
        <Kpi
          label="Spent"
          value={formatINR(data.spend)}
          note={`${count(data.withdrawals, 'withdrawal')}, ${formatINR(
            data.excludedSpendTotal,
          )} excluded as movement`}
        />
        {/* §112. A zero here is usually an unanswered question, not a figure.
            `not_earnings` is an ALLOW-LIST — `earnings_only` names what counts
            — so on an account whose payees nobody has classified yet, every
            deposit is tagged `not-earnings` and this reads ₹0.00 under the word
            "Earned". Measured on a freshly pushed account: six deposits
            arrived, and all six were excluded.

            That is the tri-state lesson again (non-negotiable 11): a figure
            nobody has classified is not a figure of zero. When every deposit is
            excluded, the card says so and points at the page that fixes it. */}
        <Kpi
          label="Earned"
          value={unclassifiedIncome ? '—' : formatINR(data.income)}
          note={
            unclassifiedIncome
              ? `${formatINR(data.grossIncome)} arrived — say what counts on Payees`
              : `${count(data.deposits, 'deposit')}, ${formatINR(
                  data.excludedIncome.amount,
                )} not earnings`
          }
        />
        {/* **Not `earned - spent`.** That was the first version and it was
            wrong in the way that matters: both of those already have movement
            taken out, so their difference counts nothing that moved. It read
            more than three times the balance's actual movement over a window,
            under a caption saying "earned less spent" — true of the arithmetic
            and read by a person as "what I kept". The server sends the real
            change now, as a Decimal. */}
        <Kpi
          label="Net change"
          value={formatINR(data.net)}
          note="Everything in, less everything out — the change in the balance"
        />
        {/* **Adaptive, because the fixed version went dead.** This tile named
            the movement the other two exclude — which mattered when most of
            The ledger's outflow was hidden behind a toggle. Since §62 and §67
            took Credit
            Card, Investments and Transfers out of `not_spend`, it reads ₹0.00
            and "nothing excluded" on most windows: a quarter of the summary
            row spent saying nothing happened. It shows the exclusion when
            there IS one, and the ledger's size when there is not. */}
        {Number(data.excludedSpendTotal) > 0 ? (
          <Kpi
            label="Movement"
            value={formatINR(data.excludedSpendTotal)}
            note={`${data.excludedSpend.map((s) => s.name).join(', ')} — not counted as spending`}
          />
        ) : (
          <Kpi
            label="Transactions"
            value={String(data.withdrawals + data.deposits)}
            note={`${count(data.categories.length, 'category', 'categories')}, nothing excluded as movement`}
          />
        )}
      </div>

      <h2 className="section">In and out</h2>
      <Card>
        <div className="flows">
          <FlowBar
            flow="earn"
            label="Earned"
            counted={data.income}
            gross={data.grossIncome}
            excluded={data.excludedIncome.amount}
            scale={scale}
            excludedLabel={`money coming back, not earned (${data.excludedIncome.count} deposits)`}
          />
          <FlowBar
            flow="spend"
            label="Spent"
            counted={data.spend}
            gross={data.grossSpend}
            excluded={data.excludedSpendTotal}
            scale={scale}
            excludedLabel={`${data.notSpend.join(', ')} — movement, not spending`}
          />
        </div>
        <Why label="Why these are not the totals on the statement">
          <p>
            The ledger store counts every deposit as income and every withdrawal as spend, by type.
            Read that way this ledger says {formatINR(data.grossSpend)} spent and{' '}
            {formatINR(data.grossIncome)} earned. Both are wrong, and not by a little.
          </p>
          <p>
            Money moving is not money leaving: {data.notSpend.join(', ')} are excluded from
            spend. Money coming back is not money earned: repayments, refunds and
            verifications carry the <code>not-earnings</code> tag. The hatched part of each
            bar is what those two rules removed.
          </p>
          {Number(data.refunds.amount) > 0 && (
            <p>
              {data.refunds.count === 1 ? 'One refund' : `${data.refunds.count} refunds`} of{' '}
              {formatINR(data.refunds.amount)} sits in the excluded deposits — left in place
              rather than netted, because netting it against a month it did not happen in is
              the more misleading of the two.
            </p>
          )}
        </Why>
      </Card>

      {data.balances.length > 0 && (
        <>
          <h2 className="section">The balance</h2>
          <Card>
            <BalanceLine series={data.balances} />
            {/* The page refuses a trend line four sections down, so it has to
                say why this one is different or it reads as contradicting
                itself. */}
            <Why label="Why this one is a line when the month chart is not">
              <p>
                {data.balances.length === 1
                  ? `${count(data.balances[0]?.points.length ?? 0, 'recorded balance')}.`
                  : `${data.balances.length} accounts, one line each — never a sum.`}
                {data.balances.some((b) => b.opening) &&
                  ' The line opens from the last balance recorded before the window.'}
              </p>
              <p>
                The month chart would have to interpolate a direction through a
                handful of aggregates, two of them partial months — that is an
                inference, and a slope is the most persuasive way to be wrong.
                This is not that. Every vertex here is a balance the bank itself
                printed on a statement row, held to the continuity check that
                gates every import, plotted at the resolution it was recorded
                at. Nothing between two points is being claimed.
              </p>
              <p>
                One point per day, being that day&rsquo;s closing figure. The
                axis starts at zero rather than at the lowest balance: on a
                savings account the distance to zero is the thing being looked
                at, and a truncated axis would turn a small wobble into a cliff.
              </p>
            </Why>
          </Card>
        </>
      )}

      <h2 className="section">Where it went</h2>
      <Card>
        {/* The ring and the bars are the same numbers twice, deliberately. A
            ring answers "what dominates" in one glance and is poor at
            comparing its middle; the bars are exact and ordered. Neither
            replaces the other, and the ring is only drawn when there is a
            distribution to show — one category is not a distribution. */}
        {data.categories.length > 1 && (
          <Donut
            slices={topWithOther(data.categories, 6)}
            total={data.spend}
            label="Real spend by category"
          />
        )}
        <CategoryBars slices={data.categories} of={data.spend} />
        {/* Kept, and it is not a caption: it is the one thing on this card
            that asks for an action, and the link is the action. §69 removed
            the explanatory notes, not the prompts. */}
        {Number(data.uncategorised.amount) > 0 && (
          <p className="muted chart__note">
            {data.uncategorised.count} row
            {data.uncategorised.count === 1 ? '' : 's'} worth{' '}
            {formatINR(data.uncategorised.amount)} still{' '}
            {data.uncategorised.count === 1 ? 'has' : 'have'} no category —{' '}
            <Link to="/payees">decide on Payees</Link>.
          </p>
        )}
        {/* **Shown, not disclosed.** This was a collapsed `<Why>`, and a
            collapsed toggle is indistinguishable from absence: a quarter of
            the window's real spending sat behind it as credit-card payments
            and read as missing from the ledger. It is drawn in the categorical hues like everything else,
            but hatched, so it is legible as *measured and deliberately not
            counted* rather than as a second set of categories. */}
        {data.excludedSpend.length > 0 && (
          <div className="excluded">
            <h3 className="excluded__head">
              Movement — {formatINR(data.excludedSpendTotal)}, not counted as spending
            </h3>
            {/* The same component as every other bar list, so movement gets
                hover, keyboard focus and a tooltip like everything else — it
                was a hand-rolled copy with none of them, which is also how it
                came to be the one chart you could not interrogate. */}
            <CategoryBars slices={data.excludedSpend} of={data.excludedSpendTotal} hatched />
            <Why label="Why this is not counted as spending">
              <p>
                Money that changed accounts rather than leaving. Which categories sit
                here is <code>not_spend</code> in <code>rules.yaml</code>, and it is
                your list — Credit Card, Investments and Transfers were all in it and
                are now counted as spending, because this install never imports a card
                statement and the payment is the only record the money left.
              </p>
            </Why>
          </div>
        )}
      </Card>

      {/* §109.3. Who, Tagged, By month, Category by month, The week and The
          day were all here and are all on Reports — as the payee view, the
          tag view, the trend view and the rhythm view. Ten chart sections on
          one page is not ten answers, it is one page nobody reads to the
          bottom of: *"ledger looks much clustered and graphs are redundant"*.

          What stays is what the Ledger is FOR — what the balance did, what
          went in and out, and where it went. Everything else is a question
          you go and ask. */}
      <p className="muted">
        <Link to="/reports">Reports</Link> has the rest — by payee, by tag, by
        month, by weekday and by hour.
      </p>
    </>
  )
}

/** One figure in the KPI row. */
function Kpi({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="kpi">
      <span className="kpi__label">{label}</span>
      <span className="kpi__value num">{value}</span>
      <span className="kpi__note">{note}</span>
    </div>
  )
}


