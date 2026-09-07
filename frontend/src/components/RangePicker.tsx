/* The date window control. SPEC §25.
 *
 * A row of presets and, behind a disclosure, two date inputs. Presets first
 * because they are what gets used — "last month" is a click, and a custom
 * window is the exception that needs two.
 *
 * The resolved dates are shown next to the control rather than left implied.
 * "This month" is unambiguous; "Last 3 months" is not, and a chart whose
 * boundary the reader has to guess at is a chart they cannot check.
 */

import { PRESETS, useRange } from '../lib/range'

export function RangePicker({
  window,
  outside,
  noun,
}: {
  /** What the server resolved. Echoed back so the label cannot drift — the
   *  `range` name is not read here, only the two dates. */
  window?: { range: string; from: string | null; to: string | null }
  /** How many rows the window is hiding — never left unsaid. */
  outside?: number
  noun: string
}) {
  const { range, from, to, choose, setBounds } = useRange()

  return (
    <div className="range">
      <div className="range__row" role="group" aria-label="Date range">
        {PRESETS.map((preset) => (
          <button
            key={preset.value}
            type="button"
            className={`chipbtn${range === preset.value ? ' chipbtn--on' : ''}`}
            aria-pressed={range === preset.value}
            onClick={() => choose(preset.value)}
          >
            {preset.label}
          </button>
        ))}
        <details className="range__custom">
          <summary className={`chipbtn${range === 'custom' ? ' chipbtn--on' : ''}`}>
            Custom
          </summary>
          <div className="range__panel">
            <label className="field">
              <span>From</span>
              <input
                type="date"
                value={from}
                max={to || undefined}
                onChange={(e) => setBounds(e.target.value, to)}
              />
            </label>
            <label className="field">
              <span>To</span>
              <input
                type="date"
                value={to}
                min={from || undefined}
                onChange={(e) => setBounds(from, e.target.value)}
              />
            </label>
          </div>
        </details>
      </div>

      <p className="range__note muted">
        {window?.from || window?.to ? (
          <>
            {window.from ?? 'the beginning'} to {window.to ?? 'today'}
          </>
        ) : (
          <>Every {noun} in the archive</>
        )}
        {outside ? ` · ${outside} outside this window` : ''}
      </p>
    </div>
  )
}
