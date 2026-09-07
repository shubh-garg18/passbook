/* The Day Rail — this app's signature element. SPEC §16.4.
 *
 * A 24-hour track with 00:00-06:00 shaded. One tick per transaction at row
 * scale; twenty-four bars at aggregate scale. One primitive, two scales.
 *
 * No other finance app has this axis because no other statement carries it.
 * Canara's does: 85 of 93 rows embed `DD/MM/YYYY HH:MM:SS` in the narration,
 * and §6.5 strips that before tokenising without discarding it. Time of day is
 * real signal — a canteen at 01:51 is a different thing from one at 16:30, and
 * that distinction is why Day Canteen and Night Canteen are separate
 * categories at all.
 *
 * SVG, not Unicode block characters. Block glyphs do not align across fonts,
 * cannot be styled, and read as noise to a screen reader — which would make
 * the signature element the least accessible thing on the page. Every rail
 * carries an aria-label with the actual time.
 */

import { TipBody, useTip } from './charts'

const W = 240
const H = 16
const NIGHT_END = 6

type RailProps = {
  /** `HH:MM:SS`, or null for the rows whose narration carries no clock. */
  time: string | null
  /** Included in the label so a screen reader gets "VIKAS KAU at 01:51". */
  label?: string
  /** Row position, for the staggered entrance. Capped in CSS terms below. */
  index?: number
}

import { hoursPastMidnight, formatClock } from '../lib/money'

export function DayRail({ time, label, index = 0 }: RailProps) {
  const hours = hoursPastMidnight(time)

  if (hours === null) {
    // Honest absence. Never rendered as midnight, which would put a tick in
    // the night band and invent a nocturnal transaction that did not happen.
    return (
      <svg
        className="rail rail--none"
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={label ? `${label}: no time recorded` : 'No time recorded'}
      >
        <rect
          x="0.5"
          y="0.5"
          width={W - 1}
          height={H - 1}
          rx="1"
          fill="none"
          stroke="var(--grid)"
          strokeDasharray="3 3"
        />
      </svg>
    )
  }

  const x = (hours / 24) * W
  const clock = formatClock(time)

  return (
    <svg
      className="rail"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={label ? `${label} at ${clock}` : `At ${clock}`}
    >
      <rect x="0" y="0" width={W} height={H} rx="1" fill="var(--rail-track)" />
      <rect x="0" y="0" width={(NIGHT_END / 24) * W} height={H} fill="var(--rail-night)" />
      {[6, 12, 18].map((hour) => (
        <line
          key={hour}
          x1={(hour / 24) * W}
          y1="0"
          x2={(hour / 24) * W}
          y2={H}
          stroke="var(--grid)"
          strokeWidth="1"
        />
      ))}
      <rect
        className="rail__tick"
        // Capped at 24 rows: a 93-row sheet must not make the last tick wait
        // three seconds, and the sweep reads from the first screenful anyway.
        style={{ ['--tick-delay' as string]: `${Math.min(index, 24) * 18}ms` }}
        x={Math.min(Math.max(x - 2, 0), W - 4)}
        y="0"
        width="4"
        height={H}
        rx="1"
        // Ramp, not stamp — same rule as the histogram bars (non-negotiable
        // 15). This tick locates one transaction on a track; it is a chart
        // mark, and chart marks are ink.
        fill="var(--ramp-1)"
      />
    </svg>
  )
}

/** The column header: a legend the rails below are read against. */
export function DayRailScale() {
  return (
    <span className="railscale" aria-hidden="true">
      <span>00</span>
      <span>06</span>
      <span>12</span>
      <span>18</span>
      <span>24</span>
    </span>
  )
}

type HistogramProps = {
  /** 24 counts, one per hour. Sums to `clocked`, NOT to `count`. */
  hours: number[]
  label: string
  /** Every transaction on this row, including the ones with no clock. */
  count: number
  height?: number
  /** Show the count axis. Off in a 26px table cell, on for the big one. */
  axis?: boolean
}

/**
 * The same primitive at aggregate scale — the analysis that split Day Canteen
 * from Night Canteen by hand in Phase 4, made permanent.
 *
 * The two denominators are different and the label must say which is which.
 * The bars sum to the *clocked* transactions; the row's count is *all* of
 * them, and 8 of 93 rows carry no clock (NEFT, CHG, SCHEME, INT). Labelling
 * the chart "N transactions" with N = clocked told a screen-reader user that
 * `Bank Charges` had 0 transactions when the row beside it said 2.
 */
/** One hour's reading, in the shared tooltip's shape. */
function HourTip({ at, n }: { at: string; n: number }) {
  return (
    <TipBody
      name={at}
      figure={`${n} transaction${n === 1 ? '' : 's'}`}
      sub="spend rows carrying a clock"
    />
  )
}

export function HourHistogram({
  hours,
  label,
  count,
  height = 40,
  axis = false,
}: HistogramProps) {
  // Only the axis form is a standalone chart on a card; the inline per-payee
  // rails are 40px tall inside a table cell and a tooltip there would fight
  // the row. So the hook is created either way (hooks cannot be conditional)
  // and only wired up when this is the big one.
  const tipState = useTip()
  const tip = axis ? tipState : null
  const peak = Math.max(1, ...hours)
  const clocked = hours.reduce((a, b) => a + b, 0)
  const night = hours.slice(0, NIGHT_END).reduce((a, b) => a + b, 0)
  const gap = 1.5
  const barWidth = (W - gap * 23) / 24

  const busiest = String(hours.indexOf(peak)).padStart(2, '0')
  const txns = (n: number) => `${n} transaction${n === 1 ? '' : 's'}`

  // Spelled out per case rather than assembled from fragments: "all 1
  // transactions" and "none of its 1 transactions carry" are what template
  // concatenation produces, and a label read aloud has no punctuation to hide
  // behind.
  let description: string
  if (clocked === 0) {
    description =
      count === 1
        ? `${label}: its one transaction has no recorded time, so there is nothing to plot.`
        : `${label}: none of its ${txns(count)} have a recorded time, so there is nothing to plot.`
  } else {
    const spread = `${night} between midnight and 6am, busiest at ${busiest}:00.`
    if (clocked < count) {
      description = `${label}: ${clocked} of ${txns(count)} have a recorded time. ${spread}`
    } else if (count === 1) {
      description = `${label}: one transaction, at ${busiest}:00.`
    } else {
      description = `${label}: all ${txns(count)} have a recorded time. ${spread}`
    }
  }

  const plot = (
    <svg
      className="hist"
      viewBox={`0 0 ${W} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={description}
      style={{ height }}
    >
      <rect x="0" y="0" width={(NIGHT_END / 24) * W} height={height} fill="var(--rail-night)" />
      <line x1="0" y1={height - 0.5} x2={W} y2={height - 0.5} stroke="var(--grid)" strokeWidth="1" />
      {hours.map((count, hour) => {
        const h = count === 0 ? 0 : Math.max(2, (count / peak) * (height - 2))
        const at = `${String(hour).padStart(2, '0')}:00`
        return (
          <rect
            key={hour}
            className={count > 0 ? 'bar mark' : 'bar'}
            style={{ ['--bar-delay' as string]: `${hour * 12}ms` }}
            x={hour * (barWidth + gap)}
            y={height - h}
            width={barWidth}
            height={h}
            /* **No `rx`.** Rounded caps were tried and looked wrong for a
               reason no amount of tuning fixes: this SVG is
               `preserveAspectRatio="none"`, so a corner radius in user units
               is stretched horizontally with the bar and every column
               rendered as a lozenge — rounded on the BASELINE too, which a
               column standing on an axis must never be. `vector-effect` does
               not apply to `rx`. Flat columns are what a histogram is, and the
               hover target below is what actually modernises this chart. */
            tabIndex={count > 0 ? 0 : undefined}
            role={count > 0 ? 'button' : undefined}
            aria-label={count > 0 ? `${at}, ${txns(count)}` : undefined}
            onMouseMove={count > 0 ? (e) => tip?.show(e, <HourTip at={at} n={count} />) : undefined}
            onMouseLeave={count > 0 ? tip?.hide : undefined}
            onFocus={count > 0 ? (e) => tip?.show(e, <HourTip at={at} n={count} />) : undefined}
            onBlur={count > 0 ? tip?.hide : undefined}
            // One colour for every bar. Night is already encoded by
            // POSITION — the shaded band behind hours 0-6 — so hue on top of
            // it was a second signal for a fact, and it made ochre mean four
            // different things across the app.
            //
            // §63. `--cat-1`, not `--ramp-2`. The ramp encodes RANK and these
            // bars are one series with no ranking — they were grey because
            // that was the only ink a chart could use, and since §58 it is
            // not. Still not `--stamp`: that ink means "this acts" and a bar
            // is not a control.
            fill="var(--cat-1)"
          />
        )
      })}
    </svg>
  )

  if (!axis) return plot

  // The bars encode a COUNT, and without a scale the tallest one could be two
  // transactions or twenty. Peak and half, in HTML beside the plot — the same
  // reason as the month columns: this SVG stretches to the card width, so text
  // inside it would be stretched with it.
  return (
    <div className="plot chartwrap" ref={tipState.box}>
      <ul className="scale" aria-hidden="true" style={{ height }}>
        <li>{peak}</li>
        <li>{peak > 1 ? Math.round(peak / 2) : ''}</li>
        <li>0</li>
      </ul>
      <div className="plot__area" style={{ height }}>
        <span className="plot__rule" style={{ top: '0%' }} aria-hidden="true" />
        {peak > 1 && <span className="plot__rule" style={{ top: '50%' }} aria-hidden="true" />}
        {plot}
      </div>
      {tipState.element}
    </div>
  )
}
