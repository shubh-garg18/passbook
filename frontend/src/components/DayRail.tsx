/* The Day Rail — this app's signature element. SPEC §16.4.
 *
 * A 24-hour track with 00:00-06:00 shaded. One tick per transaction at row
 * scale; twenty-four bars at aggregate scale. One primitive, two scales.
 *
 * No other finance app has this axis because no other statement carries it.
 * Canara's does: 85 of 93 rows embed `DD/MM/YYYY HH:MM:SS` in the narration,
 * and §6.5 strips that before tokenising without discarding it. Time of day is
 * real signal — a canteen at 01:51 is a different thing from one at 16:30, and
 * that distinction is why a morning vendor and a small-hours one are separate
 * categories at all.
 *
 * SVG, not Unicode block characters. Block glyphs do not align across fonts,
 * cannot be styled, and read as noise to a screen reader — which would make
 * the signature element the least accessible thing on the page. Every rail
 * carries an aria-label with the actual time.
 */

const W = 240
const H = 16
const NIGHT_END = 6

type RailProps = {
  /** `HH:MM:SS`, or null for the rows whose narration carries no clock. */
  time: string | null
  /** Included in the label so a screen reader gets "ZEPKV JYX at 01:51". */
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
        fill="var(--stamp)"
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
}

/**
 * The same primitive at aggregate scale — the analysis that split Morning Stall
 * from Late Counter by hand in Phase 4, made permanent.
 *
 * The two denominators are different and the label must say which is which.
 * The bars sum to the *clocked* transactions; the row's count is *all* of
 * them, and 8 of 93 rows carry no clock (NEFT, CHG, SCHEME, INT). Labelling
 * the chart "N transactions" with N = clocked told a screen-reader user that
 * `Bank Charges` had 0 transactions when the row beside it said 2.
 */
export function HourHistogram({ hours, label, count, height = 40 }: HistogramProps) {
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

  return (
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
        return (
          <rect
            key={hour}
            className="bar"
            style={{ ['--bar-delay' as string]: `${hour * 12}ms` }}
            x={hour * (barWidth + gap)}
            y={height - h}
            width={barWidth}
            height={h}
            // One colour for every bar. Night is already encoded by
            // POSITION — the shaded band behind hours 0-6 — so hue on top of
            // it was a second signal for a fact, and it made ochre mean four
            // different things across the app.
            fill="var(--stamp)"
          />
        )
      })}
    </svg>
  )
}
