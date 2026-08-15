/* The Ledger overview's charts. SPEC §18.
 *
 * **No chart library.** Every mark here is a bar or a column, sharing the Day
 * Rail's construction: an inline SVG or a plain element, one fill, an accessible
 * label carrying the real number. Recharts or Chart.js would add ~90 KB gzipped
 * to a bundle whose entire font budget is 46 KB, bring their own type scale and
 * their own default palette, and then have to be argued out of drawing a legend
 * and a tooltip for four categories. The primitive was already here.
 *
 * **Colour carries rank, never identity.** Fills come from `--ramp-1..5`, one
 * ink at five densities, assigned by position in a list that is already sorted
 * by amount. See the long note in theme.css: ten categorical hues would spend
 * the only discipline this palette has, and the category's identity is already
 * written next to the bar in words.
 *
 * **The excluded remainder is drawn, not dropped.** Where a figure excludes
 * something (§8/§8.1), the excluded part appears hatched beyond the counted
 * part. A chart that silently shows a third of the gross figure invites exactly
 * one question, and answering it in the mark is better than answering it in a
 * footnote nobody reads.
 */

import { formatAmount, formatINR } from '../lib/money'

/** A decimal string that is zero, without going anywhere near Number(). */
function isZero(amount: string): boolean {
  return /^-?0*\.?0*$/.test(amount.trim())
}

/** Rank -> ramp step. Five steps over N rows, largest first. */
export function rampStep(index: number, total: number): number {
  if (total <= 1) return 1
  return Math.min(5, 1 + Math.floor((index / total) * 5))
}

function pct(value: string, of: string): number {
  // Amounts are decimal STRINGS and stay that way for display (§16.1). A bar's
  // WIDTH is a geometry question, not a money question, so converting here is
  // safe — nothing rounded this way is ever shown as a figure.
  const top = Number(of)
  if (!top) return 0
  return Math.max(0, Math.min(100, (Number(value) / top) * 100))
}

type Slice = { name: string; amount: string; count: number }

/**
 * Horizontal bars, one per category, largest first.
 *
 * Plain elements rather than SVG on purpose: the label and the figure are real
 * text, so they are selectable, they wrap, and they need no aria-label to be
 * read out. The bar is then decoration over numbers that are already there —
 * which is also why a screen reader gets no chart description here.
 */
export function CategoryBars({ slices, of }: { slices: Slice[]; of: string }) {
  const top = slices[0]?.amount ?? '0'
  return (
    <ul className="bars">
      {slices.map((slice, index) => (
        <li className="bars__row" key={slice.name}>
          <span className="bars__label" title={slice.name}>
            {slice.name}
          </span>
          <span className="bars__track">
            <span
              className="bars__fill"
              style={{
                width: `${pct(slice.amount, top)}%`,
                background: `var(--ramp-${rampStep(index, slices.length)})`,
              }}
            />
          </span>
          <span className="bars__value num">{formatAmount(slice.amount)}</span>
          <span className="bars__share">{pct(slice.amount, of).toFixed(0)}%</span>
        </li>
      ))}
    </ul>
  )
}

/**
 * One measured figure, with what was excluded from it drawn beyond it.
 *
 * The counted part is solid ink; the excluded part is hatched and labelled. Both
 * bars share one scale, so "spend" and "earned" are directly comparable —
 * and neither is coloured by direction. A passbook prints withdrawals and
 * deposits in the same ink and lets the column carry the meaning (§16.4); a bar
 * IS the number, so colouring it by sign would be colouring money by sign.
 */
export function FlowBar({
  label,
  counted,
  gross,
  excluded,
  scale,
  excludedLabel,
  emptyLabel,
}: {
  label: string
  counted: string
  gross: string
  /** From the API, as a string. Subtracting two amounts in JS would put money
   *  through a float on its way to being displayed, which §16.1 forbids —
   *  `pct` may convert for geometry, nothing may convert for display. */
  excluded: string
  scale: string
  excludedLabel: string
  emptyLabel: string
}) {
  const countedPct = pct(counted, scale)
  const excludedPct = Math.max(0, pct(gross, scale) - countedPct)
  return (
    <div className="flow">
      <div className="flow__head">
        <h3>{label}</h3>
        <p className="figure figure--small">{formatINR(counted)}</p>
      </div>
      <div className="flow__track">
        <span className="flow__counted" style={{ width: `${countedPct}%` }} />
        <span className="flow__excluded" style={{ width: `${excludedPct}%` }} />
      </div>
      <p className="muted flow__note">
        {/* Nothing excluded is the DEFAULT state, not an edge case: `not_spend`
            ships empty because categories are the operator's to derive (D10).
            The two-clause sentence read "₹0.00 is <label>" there, which is a
            fact about nothing — so the empty case gets its own sentence. */}
        {isZero(excluded)
          ? <>all of {formatINR(gross)} counts — {emptyLabel}</>
          : <>of {formatINR(gross)} gross — {formatINR(excluded)} is {excludedLabel}</>}
      </p>
    </div>
  )
}

/**
 * A tag's total with its member categories stacked inside one bar.
 *
 * The total comes from the TAG as Firefly stored it; the segments come from the
 * categories that carry that tag in rules.yaml. They are two different sources
 * for the same number on purpose — if they ever disagree, the segments will not
 * fill the bar, and that is a visible bug rather than a silent one.
 */
export function StackedBar({
  parts,
  total,
  label,
}: {
  parts: Slice[]
  total: string
  label: string
}) {
  return (
    <div className="stack">
      <div
        className="stack__bar"
        role="img"
        aria-label={`${label}: ${parts
          .map((p) => `${p.name} ${formatINR(p.amount)}`)
          .join(', ')}, total ${formatINR(total)}`}
      >
        {parts.map((part, index) => (
          <span
            key={part.name}
            className="stack__seg"
            style={{
              width: `${pct(part.amount, total)}%`,
              background: `var(--ramp-${rampStep(index, parts.length)})`,
            }}
          />
        ))}
      </div>
      <ul className="stack__key">
        {parts.map((part, index) => (
          <li key={part.name}>
            <span
              className="stack__chip"
              style={{ background: `var(--ramp-${rampStep(index, parts.length)})` }}
              aria-hidden="true"
            />
            {part.name}
            <span className="num stack__amount">{formatAmount(part.amount)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

type Month = { month: string; spend: string; income: string; partial: boolean }

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function monthLabel(month: string): string {
  const [year, number] = month.split('-')
  return `${MONTH_NAMES[Number(number) - 1] ?? month} ${year?.slice(2) ?? ''}`
}

/**
 * Two column charts, one scale, no line through them.
 *
 * **There is deliberately no trend line.** Four buckets, two of them partial
 * months, is not a series — a line would assert a direction the data cannot
 * support, and a slope is the single most persuasive thing you can draw. So:
 * discrete columns, the partial months marked on the column itself with a
 * dashed cap rather than in a caption, and the count of complete months stated.
 *
 * Out and In are separate charts sharing a scale rather than paired bars with a
 * legend. That is the passbook's own answer: two columns on the page, position
 * carrying the direction, one ink. The shared scale is what makes the pair
 * readable — the peak is taken across BOTH series, so the flat Out chart is
 * telling you something true about its size next to In.
 *
 * A partial month gets a dashed cap on the column, and a month with no money at
 * all gets nothing: a dashed line hovering over an empty axis reads as a bar
 * that is being hidden rather than as a month that was quiet.
 */
export function MonthColumns({ months, title }: { months: Month[]; title: 'spend' | 'income' }) {
  const key = title === 'spend' ? 'spend' : 'income'
  const peak = Math.max(
    ...months.map((m) => Math.max(Number(m.spend), Number(m.income))),
    1,
  )
  const H = 96
  const gap = 10
  const width = 240
  const barWidth = (width - gap * (months.length - 1)) / Math.max(months.length, 1)

  return (
    <div className="months">
      <h3>{title === 'spend' ? 'Out, by month' : 'In, by month'}</h3>
      <svg
        className="cols"
        viewBox={`0 0 ${width} ${H + 2}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={months
          .map(
            (m) =>
              `${monthLabel(m.month)} ${Number(m[key]) === 0 ? 'nothing' : formatINR(m[key])}` +
              `${m.partial ? ', partial month' : ''}`,
          )
          .join('; ')}
      >
        <line x1="0" y1={H + 0.5} x2={width} y2={H + 0.5} stroke="var(--grid)" strokeWidth="1" />
        {months.map((month, index) => {
          const value = Number(month[key])
          const height = value === 0 ? 0 : Math.max(2, (value / peak) * (H - 4))
          const x = index * (barWidth + gap)
          return (
            <g key={month.month}>
              <rect
                className="col"
                style={{ ['--col-delay' as string]: `${index * 60}ms` }}
                x={x}
                y={H - height}
                width={barWidth}
                height={height}
                /* One density for both charts. Giving Out and In different
                   steps of the ramp made the shade look like it encoded
                   direction, which is the thing §16.4 forbids — the two titles
                   and the two positions already carry it. */
                fill="var(--ramp-2)"
              />
              {month.partial && height > 0 && (
                <line
                  x1={x}
                  y1={H - height - 2.5}
                  x2={x + barWidth}
                  y2={H - height - 2.5}
                  stroke="var(--ink-soft)"
                  strokeWidth="1.5"
                  strokeDasharray="3 3"
                />
              )}
            </g>
          )
        })}
      </svg>
      <ul className="cols__labels" aria-hidden="true">
        {months.map((month) => (
          <li key={month.month}>
            {monthLabel(month.month)}
            {/* A zero month draws no column, and a blank space under a month
                name reads as a bug rather than as a fact. So the zero is
                labelled — and it displaces "part", because "no earnings in the
                covered days" is the more useful of the two and two notes under
                one 60px column is neither. The accessible label still carries
                both, and the caption still names every partial month. */}
            {Number(month[key]) === 0 ? (
              <span className="cols__note">none</span>
            ) : (
              month.partial && <span className="cols__note">part</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
