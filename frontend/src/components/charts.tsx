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
 * part. A chart that silently counts a third of what left the account invites
 * exactly one question,
 * question, and answering it in the mark is better than answering it in a
 * footnote nobody reads.
 */

import { useCallback, useRef, useState } from 'react'

import { compactAmount, formatAmount, formatDayMonth, formatINR } from '../lib/money'

/* `rampStep` lived here and is gone (§75).
 *
 * It mapped a rank to one of five ink densities, and it was the ONLY colour
 * function this file had until §58 added `catColor`. Every mark that used it
 * has since moved to a hue, and a helper with no callers is a helper someone
 * will reach for by accident — the ramp still exists as tokens for the two
 * places that genuinely want density rather than identity (the Day Rail's
 * single series, and the hatched "not counted" fill), and those name the
 * variable directly. */

/** Identity -> hue. §58.
 *
 * The counterpart to `rampStep`, and the choice between them is not a style
 * preference: **the ramp encodes rank, this encodes identity.** A month column
 * and an hour bucket stay on the ramp because they are one series measured
 * over time — colouring twelve months in twelve hues would claim they are
 * twelve different things. A category, a payee, an account is a named thing,
 * and a name is what a hue is for.
 *
 * Eight hues, wrapping. Wrapping is safe because every chart that uses this
 * either caps its series (the donut folds to six plus Other) or prints the
 * name beside the mark, so two rows sharing a hue are never ambiguous.
 */
export function catColor(index: number): string {
  const hue = `var(--cat-${(index % 8) + 1})`
  if (index < 8) return hue
  // Past the eighth the wheel repeats, and a plain repeat reads as a mistake:
  // on the reference ledger the eleventh category wore the same red as the
  // first and looked like a duplicate rather than a smaller thing. Each further
  // lap is mixed toward the ground, so a repeat is visibly the tail of a long
  // ranked list. Two laps is the practical limit and that is fine — every chart
  // using this either caps its series or prints the name beside the mark.
  const lap = Math.floor(index / 8)
  return `color-mix(in oklab, ${hue} ${Math.max(30, 100 - lap * 45)}%, var(--sheet))`
}

/**
 * One tooltip, shared by every chart.
 *
 * Positioned against the chart's own box rather than the viewport, so it
 * travels correctly inside a card that scrolls. Returns the handlers a mark
 * needs for BOTH hover and keyboard focus — hover alone would leave a keyboard
 * or touch user with no way to read an exact figure, on marks whose entire
 * purpose is exact figures.
 */
export function useTip() {
  const box = useRef<HTMLDivElement>(null)
  const [tip, setTip] = useState<{
    x: number
    y: number
    node: React.ReactNode
    below: boolean
  } | null>(null)

  const show = useCallback((event: React.MouseEvent | React.FocusEvent, node: React.ReactNode) => {
    const host = box.current
    if (!host) return
    const bounds = host.getBoundingClientRect()
    const target = (event.currentTarget as Element).getBoundingClientRect()
    // A focus event has no pointer, so anchor to the mark's own centre. A
    // mouse event uses the pointer when it has one, which keeps the tooltip
    // under the finger on a long bar rather than at its midpoint.
    const px = 'clientX' in event && event.clientX ? event.clientX : target.left + target.width / 2
    const py = 'clientY' in event && event.clientY ? event.clientY : target.top

    // §88. Flip below when there is no room above.
    //
    // The tip sits at `translate(-50%, -115%)`, so near the top of its
    // container it renders outside — and on Reports the cards sit inside
    // `.swap`, which is `overflow-x: clip` and therefore establishes a clip
    // context in BOTH axes. On the Ledger the same tooltip had a whole page to
    // overflow into and looked fine, which is why this only showed up on one
    // page. Flipping is better than widening the container: the tip stays
    // attached to the mark it describes.
    const y = py - bounds.top
    const ROOM = 78
    const below = y < ROOM

    // x is clamped by the tip's OWN half-width, because it is centred on the
    // point with `translate(-50%)`. The first attempt clamped to 48px — two
    // fifths of the half-width — and measured a 21px cut on the left edge of
    // the ring: enough margin for a narrow tip and not for a real one.
    //
    // `TIP_HALF` tracks `.tip`'s `max-width` in theme.css. Two constants that
    // must agree is not ideal, but the alternative is measuring the tip after
    // it renders and moving it, which is a visible jump on every hover.
    const TIP_HALF = 110
    const room = Math.max(0, bounds.width / 2 - TIP_HALF)
    const centre = bounds.width / 2
    setTip({
      // When the container is narrower than the tip, `room` is 0 and this
      // pins to the centre — off-centre by a pixel beats clipped by twenty.
      x: Math.max(centre - room, Math.min(centre + room, px - bounds.left)),
      y: below ? y + 22 : y,
      node,
      below,
    })
  }, [])

  const hide = useCallback(() => setTip(null), [])

  const element = tip ? (
    <div
      className={`tip${tip.below ? ' tip--below' : ''}`}
      style={{ left: tip.x, top: tip.y }}
      role="presentation"
    >
      {tip.node}
    </div>
  ) : null

  return { box, show, hide, element }
}

/** The body of a tooltip: name, figure, and one line of context. */
export function TipBody({
  name,
  figure,
  sub,
}: {
  name: string
  figure: string
  sub?: string
}) {
  return (
    <>
      <span className="tip__name">{name}</span>
      <span className="tip__figure">{figure}</span>
      {sub && <span className="tip__sub">{sub}</span>}
    </>
  )
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
export function CategoryBars({
  slices,
  of,
  noun = 'transaction',
  hatched = false,
}: {
  slices: Slice[]
  of: string
  noun?: string
  /** Rows that are measured and deliberately **not** in the headline figure —
   *  the movement categories (§8.1). SPEC §102.
   *
   *  They used to be drawn as an ochre barber's pole, on the reasoning that a
   *  hue would invite comparison with the counted categories. The operator
   *  rejected that three times, most recently *"Investment and other graph
   *  color should be normal not yellow strips its not visually appealing"* —
   *  and they are right about the premise as well as the look: these ARE
   *  categories, the chart is a list of them, and comparing Investments with
   *  Transfers inside it is a reasonable thing to want.
   *
   *  So the exclusion is said in words, in the heading and the note, which is
   *  where it belongs. The flag survives to mark the chart, not to colour it. */
  hatched?: boolean
}) {
  const top = slices[0]?.amount ?? '0'
  const tip = useTip()
  return (
    <div className="chartwrap" ref={tip.box}>
      <ul className="bars">
        {slices.map((slice, index) => (
          <li className="bars__row" key={slice.name}>
            <span className="bars__label" title={slice.name}>
              {slice.name}
            </span>
            <span
              className="bars__track mark"
              tabIndex={0}
              role="button"
              aria-label={`${slice.name}, ${formatINR(slice.amount)}, ${pct(
                slice.amount,
                of,
              ).toFixed(0)} percent, ${slice.count} ${noun}${slice.count === 1 ? '' : 's'}`}
              onMouseMove={(e) =>
                tip.show(
                  e,
                  <TipBody
                    name={slice.name}
                    figure={formatINR(slice.amount)}
                    sub={`${pct(slice.amount, of).toFixed(1)}% of the total · ${slice.count} ${noun}${slice.count === 1 ? '' : 's'}`}
                  />,
                )
              }
              onMouseLeave={tip.hide}
              onFocus={(e) =>
                tip.show(
                  e,
                  <TipBody
                    name={slice.name}
                    figure={formatINR(slice.amount)}
                    sub={`${pct(slice.amount, of).toFixed(1)}% of the total · ${slice.count} ${noun}${slice.count === 1 ? '' : 's'}`}
                  />,
                )
              }
              onBlur={tip.hide}
            >
              <span
                className={`bars__fill${hatched ? ' bars__fill--out' : ''}`}
                style={{
                  width: `${pct(slice.amount, top)}%`,
                  // The wheel either way. §102 — an excluded category is still
                  // a category, and the heading is what says it is excluded.
                  background: catColor(index),
                }}
              />
            </span>
            <span className="bars__value num">{formatAmount(slice.amount)}</span>
            <span className="bars__share">{pct(slice.amount, of).toFixed(0)}%</span>
          </li>
        ))}
      </ul>
      {tip.element}
    </div>
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
  flow,
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
  /** Which series this is. A discriminator, not the label — inferring the hue
   *  from `label.startsWith('earn')` meant renaming "Earned" to "Money in"
   *  silently flipped it to the spend colour, with no type error and no test
   *  failure. `MonthColumns` already takes the same union. */
  flow: 'spend' | 'earn'
}) {
  const countedPct = pct(counted, scale)
  const excludedPct = Math.max(0, pct(gross, scale) - countedPct)
  const tip = useTip()
  // §72. Two hues, and as with the month columns this is series identity, not
  // §16.4's forbidden colouring by SIGN — indigo and cyan carry no verdict.
  // The two labels and the two positions still say which is which.
  const hue = flow === 'earn' ? 'var(--cat-5)' : 'var(--cat-1)'
  return (
    <div className="flow chartwrap" ref={tip.box}>
      <div className="flow__head">
        <h3>{label}</h3>
        <p className="figure figure--small">{formatINR(counted)}</p>
      </div>
      <div className="flow__track">
        <span
          className="flow__counted mark"
          style={{ width: `${countedPct}%`, background: hue }}
          tabIndex={0}
          role="button"
          aria-label={`${label}, ${formatINR(counted)} counted`}
          onMouseMove={(e) =>
            tip.show(e, <TipBody name={label} figure={formatINR(counted)} sub="counted" />)
          }
          onMouseLeave={tip.hide}
          onFocus={(e) =>
            tip.show(e, <TipBody name={label} figure={formatINR(counted)} sub="counted" />)
          }
          onBlur={tip.hide}
        />
        <span
          className="flow__excluded mark"
          style={{ width: `${excludedPct}%` }}
          tabIndex={Number(excluded) > 0 ? 0 : undefined}
          role={Number(excluded) > 0 ? 'button' : undefined}
          aria-label={`${formatINR(excluded)} ${excludedLabel}`}
          onMouseMove={(e) =>
            tip.show(e, <TipBody name={excludedLabel} figure={formatINR(excluded)} sub={`of ${formatINR(gross)} gross`} />)
          }
          onMouseLeave={tip.hide}
          onFocus={(e) =>
            tip.show(e, <TipBody name={excludedLabel} figure={formatINR(excluded)} sub={`of ${formatINR(gross)} gross`} />)
          }
          onBlur={tip.hide}
        />
      </div>
      <p className="muted flow__note">
        of {formatINR(gross)} gross — {formatINR(excluded)} is {excludedLabel}
      </p>
      {tip.element}
    </div>
  )
}

/**
 * A tag's total with its member categories stacked inside one bar.
 *
 * The total comes from the TAG as the ledger stored it; the segments come from the
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
  const tip = useTip()
  return (
    <div className="stack chartwrap" ref={tip.box}>
      <div
        className="stack__bar"
        role="img"
        aria-label={`${label}: ${parts
          .map((p) => `${p.name} ${formatINR(p.amount)}`)
          .join(', ')}, total ${formatINR(total)}`}
      >
        {parts.map((part, index) => {
          const body = (
            <TipBody
              name={part.name}
              figure={formatINR(part.amount)}
              sub={`${pct(part.amount, total).toFixed(1)}% of ${label} · ${part.count} transaction${part.count === 1 ? '' : 's'}`}
            />
          )
          return (
            <span
              key={part.name}
              className="stack__seg mark"
              style={{ width: `${pct(part.amount, total)}%`, background: catColor(index) }}
              tabIndex={0}
              role="button"
              aria-label={`${part.name}, ${formatINR(part.amount)}`}
              onMouseMove={(e) => tip.show(e, body)}
              onMouseLeave={tip.hide}
              onFocus={(e) => tip.show(e, body)}
              onBlur={tip.hide}
            />
          )
        })}
      </div>
      <ul className="stack__key">
        {parts.map((part, index) => (
          <li key={part.name}>
            <span
              className="stack__chip"
              style={{ background: catColor(index) }}
              aria-hidden="true"
            />
            {part.name}
            <span className="num stack__amount">{formatAmount(part.amount)}</span>
          </li>
        ))}
      </ul>
      {tip.element}
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
/**
 * A y-axis with real numbers on it.
 *
 * HTML, not SVG text. Both column charts render with
 * `preserveAspectRatio="none"` so the bars stretch to whatever width the card
 * gives them — which is right for bars and fatal for type: a `<text>` inside
 * that viewBox is scaled horizontally with everything else, so the labels come
 * out condensed or stretched depending on the viewport. Keeping the scale in
 * HTML beside the plot means the numbers are real text at a real size, they
 * are selectable, and a screen reader reads them without an aria-label.
 *
 * Three ticks, not five: the point of the axis is to make a column's height
 * mean an amount, and 0 / half / peak does that without turning the card into
 * graph paper.
 */
function Scale({ peak, format }: { peak: number; format: (n: number) => string }) {
  return (
    <ul className="scale" aria-hidden="true">
      {[peak, peak / 2, 0].map((value, index) => (
        <li key={index}>{format(value)}</li>
      ))}
    </ul>
  )
}

/** A "nice" ceiling, so the top tick is a number a person would say. */
export function niceCeiling(value: number): number {
  if (value <= 0) return 1
  const magnitude = 10 ** Math.floor(Math.log10(value))
  for (const step of [1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10]) {
    if (value <= step * magnitude) return step * magnitude
  }
  return 10 * magnitude
}

export function MonthColumns({ months, title }: { months: Month[]; title: 'spend' | 'income' }) {
  const key = title === 'spend' ? 'spend' : 'income'
  // ONE scale across both charts, from the larger of the two measures. Giving
  // Out and In their own peaks would let a small income month draw a taller
  // column than a large spend month — a dual scale by the back door, which is
  // the one thing a pair of charts side by side must never do.
  const peak = niceCeiling(
    Math.max(...months.map((m) => Math.max(Number(m.spend), Number(m.income))), 1),
  )
  const H = 96
  const gap = 10
  const width = 240
  const barWidth = (width - gap * (months.length - 1)) / Math.max(months.length, 1)

  const tip = useTip()
  return (
    <div className="months chartwrap" ref={tip.box}>
      <h3>{title === 'spend' ? 'Out, by month' : 'In, by month'}</h3>
      <div className="plot">
        <Scale peak={peak} format={compactAmount} />
        <div className="plot__area">
          {/* Gridlines at the tick positions, drawn behind the columns so a
              column's top can be read against a number rather than guessed. */}
          <span className="plot__rule" style={{ top: '0%' }} aria-hidden="true" />
          <span className="plot__rule" style={{ top: '50%' }} aria-hidden="true" />
          <svg
            className="cols"
            viewBox={`0 0 ${width} ${H + 2}`}
            preserveAspectRatio="none"
            role="img"
            aria-label={
              `${title === 'spend' ? 'Out' : 'In'} by month, scale 0 to ` +
              `${formatINR(String(peak))}. ` +
              months
                .map(
                  (m) =>
                    `${monthLabel(m.month)} ${
                      Number(m[key]) === 0 ? 'nothing' : formatINR(m[key])
                    }${m.partial ? ', partial month' : ''}`,
                )
                .join('; ')
            }
          >
            <line
              x1="0"
              y1={H + 0.5}
              x2={width}
              y2={H + 0.5}
              stroke="var(--grid)"
              strokeWidth="1"
            />
            {months.map((month, index) => {
              const value = Number(month[key])
              const height = value === 0 ? 0 : Math.max(2, (value / peak) * (H - 4))
              const x = index * (barWidth + gap)
              const body = (
                <TipBody
                  name={monthLabel(month.month)}
                  figure={value === 0 ? 'nothing' : formatINR(month[key])}
                  sub={`${title === 'spend' ? 'out' : 'in'}${month.partial ? ' · partial month' : ''}`}
                />
              )
              return (
                <g key={month.month}>
                  <rect
                    className={value > 0 ? 'col mark' : 'col'}
                    /* Left to right, 60ms apart — the axis's own direction. */
                    style={{ ['--col-delay' as string]: `${index * 60}ms` }}
                    x={x}
                    y={H - height}
                    width={barWidth}
                    height={height}
                    tabIndex={value > 0 ? 0 : undefined}
                    role={value > 0 ? 'button' : undefined}
                    aria-label={`${monthLabel(month.month)}, ${formatINR(month[key])}`}
                    onMouseMove={value > 0 ? (e) => tip.show(e, body) : undefined}
                    onMouseLeave={value > 0 ? tip.hide : undefined}
                    onFocus={value > 0 ? (e) => tip.show(e, body) : undefined}
                    onBlur={value > 0 ? tip.hide : undefined}
                    /* §63. Two hues for two series, and this is NOT §16.4
                       being broken. That rule forbids colouring money by
                       SIGN — red for out, green for in, the value judgement a
                       passbook never makes. Indigo and cyan carry no such
                       reading: they are series identity, the same job the hue
                       does on every other chart here, and direction is still
                       carried by the two titles and the two positions. The
                       hues are deliberately NOT the red/green pair. */
                    fill={title === 'spend' ? 'var(--cat-1)' : 'var(--cat-5)'}
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
        </div>
      </div>
      {tip.element}
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

/* ---------------------------------------------------------------------------
 * §57. Three marks added so the ledger's report screens stop being a reason to
 * open the ledger. All three obey non-negotiable 16: `--ramp-*` and nothing else,
 * one ink at five densities, ordered by magnitude.
 * ------------------------------------------------------------------------- */

/** Top N by amount, with the tail folded into one slice — re-sorted after.
 *
 * The re-sort is not cosmetic. The ramp encodes RANK, so if the folded "Other"
 * outweighs the smallest kept slice, leaving it last would paint a large slice
 * in the lightest ink and the density would be lying about magnitude.
 */
export function topWithOther(slices: Slice[], keep: number): Slice[] {
  if (slices.length <= keep) return slices
  const head = slices.slice(0, keep)
  const tail = slices.slice(keep)
  const other: Slice = {
    name: `Other (${tail.length} categories)`,
    amount: String(tail.reduce((sum, s) => sum + Number(s.amount), 0)),
    count: tail.reduce((sum, s) => sum + s.count, 0),
  }
  return [...head, other].sort((a, b) => Number(b.amount) - Number(a.amount))
}

/**
 * The category distribution as a ring.
 *
 * Asked for by name — the ledger's pie is the chart the operator missed. It is
 * drawn in the ramp rather than in ten hues, which works here for a reason
 * specific to this data: the slices are already rank-ordered, so density is
 * carrying the same fact the arc length carries, and the identity is in the
 * legend beside it. Ten hues would spend the palette's only discipline on
 * decoration (see the note at the top of this file and in theme.css).
 *
 * **The bars below it are still the better read** and are not being replaced.
 * A ring is good at "one thing dominates" and bad at comparing its middle;
 * they answer different questions and the card carries both.
 *
 * Geometry: one `<circle>` per slice, `stroke-dasharray` walking the
 * circumference. `preserveAspectRatio` is left at its default — unlike the
 * column charts this must NOT stretch, or the ring becomes an ellipse and the
 * arc lengths stop being comparable.
 */
export function Donut({
  slices,
  total,
  label,
}: {
  slices: Slice[]
  total: string
  label: string
}) {
  const R = 38
  const STROKE = 15
  const circumference = 2 * Math.PI * R
  const sum = slices.reduce((n, s) => n + Number(s.amount), 0)
  // The gap is a fraction of the ring, not a fixed length, so it stays
  // proportional at any card width. Suppressed entirely for a single slice —
  // a ring with one gap in it reads as a missing piece.
  const gap = slices.length > 1 ? circumference * 0.004 : 0

  const tip = useTip()

  let walked = 0
  return (
    <div className="donut chartwrap" ref={tip.box}>
      <svg
        className="donut__ring"
        viewBox="0 0 100 100"
        role="img"
        aria-label={
          `${label}, ${formatINR(total)} across ${slices.length} ` +
          `${slices.length === 1 ? 'category' : 'categories'}: ` +
          slices
            .map(
              (s) =>
                `${s.name} ${formatINR(s.amount)}, ${((Number(s.amount) / (sum || 1)) * 100).toFixed(0)} percent`,
            )
            .join('; ')
        }
      >
        {/* The track. Without it a ring of one small slice floats with nothing
            to be a fraction OF. */}
        <circle
          cx="50"
          cy="50"
          r={R}
          fill="none"
          stroke="var(--grid-soft)"
          strokeWidth={STROKE}
        />
        {/* Start at twelve o'clock and run clockwise, which is how a reader
            expects to walk one; the default start is three.
            **The rotation lives on this group, never on the arcs.** A
            `transform` attribute on an element whose CSS also sets
            `transform-origin` is applied about that origin as well as its own
            centre — measured: the ring rotated about (100,100), left the
            viewBox entirely and was clipped away, while every computed style
            (opacity 1, correct dasharray, correct stroke) still read perfectly
            healthy. The group carries no CSS transform, so its three-argument
            rotate means what it says. */}
        <g transform="rotate(-90 50 50)">
          {slices.map((slice, index) => {
            const share = sum ? Number(slice.amount) / sum : 0
            const length = Math.max(0, share * circumference - gap)
            const dash = `${length} ${circumference - length}`
            const offset = -walked
            walked += share * circumference
            const body = (
              <TipBody
                name={slice.name}
                figure={formatINR(slice.amount)}
                sub={`${(share * 100).toFixed(1)}% of ${formatINR(total)} · ${slice.count} transaction${slice.count === 1 ? '' : 's'}`}
              />
            )
            return (
              <circle
                key={slice.name}
                className="donut__arc mark"
                style={{ ['--arc-delay' as string]: `${index * 70}ms` }}
                cx="50"
                cy="50"
                r={R}
                fill="none"
                stroke={catColor(index)}
                strokeWidth={STROKE}
                strokeDasharray={dash}
                strokeDashoffset={offset}
                tabIndex={0}
                role="button"
                aria-label={`${slice.name}, ${formatINR(slice.amount)}, ${(share * 100).toFixed(0)} percent`}
                onMouseMove={(e) => tip.show(e, body)}
                onMouseLeave={tip.hide}
                onFocus={(e) => tip.show(e, body)}
                onBlur={tip.hide}
              />
            )
          })}
        </g>
      </svg>
      <ul className="donut__key">
        {slices.map((slice, index) => (
          <li key={slice.name}>
            <span
              className="donut__swatch"
              style={{ background: catColor(index) }}
              aria-hidden="true"
            />
            <span className="donut__name" title={slice.name}>
              {slice.name}
            </span>
            <span className="donut__share num">
              {((Number(slice.amount) / (sum || 1)) * 100).toFixed(0)}%
            </span>
          </li>
        ))}
      </ul>
      {tip.element}
    </div>
  )
}

type BalanceSeries = {
  slug: string
  label: string
  points: { day: string; balance: string }[]
  opening: { day: string; balance: string } | null
}

/**
 * The balance, over time. the ledger's headline chart, and passbook had none.
 *
 * **This is not the trend line `MonthColumns` refuses to draw.** That one would
 * interpolate a direction through four monthly aggregates, two of them stubs —
 * an inference. This is the bank's own running balance, recorded on every
 * statement row and held to §6.6's continuity check, plotted at the resolution
 * it was recorded at. Every vertex is a figure the bank printed. Nothing here
 * is inferred, which is why a line is allowed.
 *
 * One line per account and never a sum: see the note in `/analysis`.
 *
 * `preserveAspectRatio="none"` to fill the card, with `vector-effect` keeping
 * the stroke a constant width — without it the line is squashed thin
 * horizontally and hairline-thick vertically at wide viewports.
 */
export function BalanceLine({ series }: { series: BalanceSeries[] }) {
  const lines = series
    .flatMap((s) => {
      // The opening point is a real recorded balance from before the window.
      // Drawing from it is what stops the first in-window transaction from
      // reading as the month's starting balance.
      const all = [...(s.opening ? [s.opening] : []), ...s.points]
      const first = all[0]
      const last = all[all.length - 1]
      return first && last ? [{ ...s, all, first, last }] : []
    })
    // Rank by closing balance so the ramp's densities are ordered by magnitude.
    .sort((a, b) => Number(b.last.balance) - Number(a.last.balance))

  if (lines.length === 0) return null

  const days = lines.flatMap((s) => s.all.map((p) => Date.parse(p.day)))
  const values = lines.flatMap((s) => s.all.map((p) => Number(p.balance)))
  const t0 = Math.min(...days)
  const t1 = Math.max(...days)
  const span = t1 - t0 || 1
  // The floor is zero unless the account went overdrawn. Starting the axis at
  // the minimum balance would magnify a 2% wobble into a cliff — the classic
  // truncated-axis lie, and on a savings balance the distance to zero is the
  // thing being looked at.
  const low = Math.min(0, ...values)
  const peak = niceCeiling(Math.max(...values, 1))
  const H = 150
  const W = 320
  const x = (day: string) => ((Date.parse(day) - t0) / span) * W
  const y = (value: number) => H - ((value - low) / (peak - low || 1)) * H

  const iso = (ms: number) => new Date(ms).toISOString().slice(0, 10)
  const spanFirst = iso(t0)
  const spanMid = iso(t0 + span / 2)
  const spanLast = iso(t1)

  return (
    <>
      <div className="plot">
        <Scale peak={peak} format={compactAmount} />
        <div className="plot__area">
          <span className="plot__rule" style={{ top: '0%' }} aria-hidden="true" />
          <span className="plot__rule" style={{ top: '50%' }} aria-hidden="true" />
          <svg
            className="line"
            viewBox={`0 0 ${W} ${H}`}
            preserveAspectRatio="none"
            role="img"
            aria-label={lines
              .map(
                (s) =>
                  `${s.label}: ${s.all.length} recorded balances from ` +
                  `${s.first.day} at ${formatINR(s.first.balance)} to ` +
                  `${s.last.day} at ${formatINR(s.last.balance)}`,
              )
              .join('; ')}
          >
            <line x1="0" y1={y(low)} x2={W} y2={y(low)} stroke="var(--grid)" strokeWidth="1"
              vectorEffect="non-scaling-stroke" />
            {lines.map((s, index) => (
              <polyline
                key={s.slug}
                className="line__path"
                fill="none"
                stroke={catColor(index)}
                strokeWidth="2"
                strokeLinejoin="round"
                strokeLinecap="round"
                vectorEffect="non-scaling-stroke"
                points={s.all.map((p) => `${x(p.day)},${y(Number(p.balance))}`).join(' ')}
              />
            ))}
          </svg>
        </div>
      </div>
      {/* A time series with no time on it. The first shot of this chart had no
          axis at all — four months of balance reading as an anonymous squiggle.
          HTML for the same reason `Scale` is: the SVG stretches and would
          condense any text inside it.
          **Outside `.plot`, not inside `.plot__area`.** `.plot` stretches its
          children, so a date row nested in the plot area makes that column
          taller and the scale spreads its `0` down to meet it — measured, the
          axis read `0 09 May` with the two touching. Indented past the scale
          gutter instead, the way `.railscale--inset` already does it. */}
      <ul className="line__dates" aria-hidden="true">
        <li>{formatDayMonth(spanFirst)}</li>
        <li>{formatDayMonth(spanMid)}</li>
        <li>{formatDayMonth(spanLast)}</li>
      </ul>
    </>
  )
}

/**
 * Category against month, as density.
 *
 * The ledger's Category report, compressed. Twenty-one categories over four months
 * is 21 sparklines — a wall of near-identical stubs — or one grid where each
 * cell's density is its share of that category's biggest month. Density for
 * magnitude is exactly what the ramp is for, so this needs no new ink.
 *
 * Cells carry the **exact** figure, not `compactAmount`. There is room, and a
 * grid of `1.2k` beside a Total column reading `1,245.00` is two precisions in
 * one table — the compact form exists for axis ticks, where there is no room.
 *
 * **The magnitude is a bar under the figure, not a fill behind it.** Filled
 * cells were tried and measured: worst contrast 2.97:1 in dark and 2.23:1 in
 * light, against a 4.5 floor. No ink-flip threshold fixes that, because the
 * failure is in the MIDDLE of the ramp — a mid-luminance fill is too dark for
 * dark text and too light for light text, and an alpha ramp has to pass
 * through it. Moving the colour out from behind the type keeps the hue, keeps
 * the magnitude, and puts every figure back on the plain card.
 *
 * **Each row is scaled to its OWN peak**, not to the grid's. The question this
 * answers is "when did I spend on X", and normalising every row to the largest
 * category would leave every row but the first blank.
 *
 * Which is exactly why the **Total column is not optional.** Under per-row
 * scaling a category with one active month gets a maximally dark cell carrying
 * no information at all — measured on the reference ledger, a category holding
 * a single ₹20 row and one holding hundreds of times that rendered at identical
 * weight, and the caption explaining
 * it is not read at a glance. The total puts the absolute size back on the row,
 * so density answers *when* and the column answers *how much*.
 */
export function CategoryHeat({
  rows,
  months,
}: {
  rows: { name: string; amounts: string[]; total: string }[]
  months: { month: string }[]
}) {
  return (
    <div className="heat__scroll">
      <table className="heat">
        <caption className="visually-hidden">
          Spend per category per month. The bar under each figure is that
          month&rsquo;s share of the category&rsquo;s own largest month, so a
          full bar is that category&rsquo;s peak and not the grid&rsquo;s.
        </caption>
        <thead>
          <tr>
            <th scope="col">Category</th>
            <th scope="col" className="num heat__total">
              Total
            </th>
            {months.map((m) => (
              <th scope="col" key={m.month} className="num">
                {monthLabel(m.month)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => {
            const peak = Math.max(...row.amounts.map(Number), 0)
            return (
              <tr key={row.name}>
                <th scope="row" title={row.name}>
                  {row.name}
                </th>
                <td className="num heat__total">{formatAmount(row.total)}</td>
                {row.amounts.map((amount, index) => {
                  const value = Number(amount)
                  // **Hue is the category, alpha is the magnitude.** The row
                  // keeps one colour so the eye can follow it across, and the
                  // cell's weight within that colour says which month was the
                  // big one — two facts in one mark without two inks.
                  // Zero gets no fill at all rather than the faintest one:
                  // "no spend" and "a little spend" must not look alike.
                  const strength = value <= 0 ? 0 : 0.06 + 0.94 * (value / (peak || 1))
                  const label = `${row.name}, ${monthLabel(months[index]?.month ?? '')}: ${
                    value === 0 ? 'nothing' : formatINR(amount)
                  }`
                  return (
                    <td key={index} className="heat__cell num" title={label}>
                      {value > 0 && (
                        <span
                          className="heat__bar"
                          style={{
                            width: `${(strength * 100).toFixed(1)}%`,
                            background: catColor(rowIndex),
                          }}
                          aria-hidden="true"
                        />
                      )}
                      {value === 0 ? '' : formatAmount(amount)}
                    </td>
                  )
                })}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/* `BoxPlot` and its `SpreadRow` type lived here and are gone (§91).
 *
 * The operator asked for a box plot, saw it, and asked for it to be removed —
 * which is the system working: it answered a question ("is this one big
 * payment or forty small ones") that turned out not to be one they ask. The
 * five-number summary it drew still comes back from `/analysis` as `spread`,
 * because it is four lines of `_quantile` inside a pass that already runs and
 * it is exactly right if the question ever does come up. A component with no
 * callers is one somebody reaches for by accident; a field with no readers
 * costs nothing.
 */

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

/**
 * Spending by day of the week. §76.
 *
 * The Day Rail's companion, and it covers more of the ledger than the Day Rail
 * does: an hour needs a clock parsed out of the narration and only most rows
 * carry one, where a weekday needs only the date, which every row has.
 *
 * Bars are the AMOUNT, and the count rides along in the tooltip. Sizing them by
 * count would answer "when do I transact" — a different and much less
 * interesting question than "when does the money go".
 */
export function WeekdayBars({
  amounts,
  counts,
}: {
  amounts: string[]
  counts: number[]
}) {
  const tip = useTip()
  const peak = Math.max(...amounts.map(Number), 1)
  return (
    <div className="chartwrap" ref={tip.box}>
      <ul className="week">
        {DAYS.map((day, index) => {
          const value = Number(amounts[index] ?? '0')
          const n = counts[index] ?? 0
          const body = (
            <TipBody
              name={day}
              figure={value === 0 ? 'nothing' : formatINR(amounts[index] ?? '0')}
              sub={`${n} transaction${n === 1 ? '' : 's'}`}
            />
          )
          return (
            <li className="week__col" key={day}>
              <span
                className={value > 0 ? 'week__bar mark' : 'week__bar'}
                style={{
                  height: `${value === 0 ? 0 : Math.max(3, (value / peak) * 100)}%`,
                  // Saturday and Sunday keep the same ink as the weekdays. The
                  // weekend is a FACT carried by position and by the labels —
                  // giving it a second hue would be the mistake §18.4 records
                  // about the Day Rail's night band.
                  background: 'var(--cat-1)',
                  ['--col-delay' as string]: `${index * 45}ms`,
                }}
                tabIndex={value > 0 ? 0 : undefined}
                role={value > 0 ? 'button' : undefined}
                aria-label={`${day}, ${formatINR(amounts[index] ?? '0')} across ${n} transactions`}
                onMouseMove={value > 0 ? (e) => tip.show(e, body) : undefined}
                onMouseLeave={value > 0 ? tip.hide : undefined}
                onFocus={value > 0 ? (e) => tip.show(e, body) : undefined}
                onBlur={value > 0 ? tip.hide : undefined}
              />
              <span className="week__label">{day}</span>
            </li>
          )
        })}
      </ul>
      {tip.element}
    </div>
  )
}
