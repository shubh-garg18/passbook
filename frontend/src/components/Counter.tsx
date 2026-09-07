/* A figure that arrives by counting. SPEC §36.
 *
 * A passbook's whole point is the balance, and it was rendering as static text
 * — the one number the operator opens the app for, delivered with no more
 * ceremony than a table cell.
 *
 * It counts **from the figure that was there**, not from zero — switching
 * accounts or changing the window, the travel between two balances is the
 * comparison the operator came for, and starting from zero throws it away.
 *
 * **The digits are the only thing animated; the value is never computed.**
 * Money is a decimal string all the way to the DOM (§16.1, non-negotiable 1),
 * so this interpolates a *display* number for ~700ms and then hands over to the
 * exact formatted string. What settles on screen is the same characters that
 * would have been there without this file.
 */

import { useEffect, useRef, useState } from 'react'

const DURATION = 700

/** Ease-out cubic: fast, then decelerating, like a mechanical counter stopping. */
const ease = (t: number) => 1 - (1 - t) ** 3

export function Counter({
  value,
  format,
}: {
  /** The decimal string. `format(Number(value))` is what settles on screen. */
  value: string
  /** Formats a number for display — used mid-flight AND for the final frame,
   *  so the settled text is byte-identical to rendering without this. */
  format: (n: number) => string
}) {
  const target = Number(value)
  // The settled text. `format` is given the exact decimal so no rounding is
  // introduced: `formatINR` takes the STRING and never sees a float, which is
  // non-negotiable 1 holding all the way to the DOM.
  const settled = Number.isFinite(target) ? format(target) : value
  const [shown, setShown] = useState<number | null>(null)
  const frame = useRef(0)
  // What was on screen last. It counts FROM here rather than from zero, which
  // is the difference between a flourish and a fact: switching accounts, or
  // changing the date window, the travel between the two figures is the
  // comparison the operator came for. Zero on first mount, because there was
  // nothing there before.
  const previous = useRef(0)

  useEffect(() => {
    if (!Number.isFinite(target)) return
    // The viewer asked for less motion: show the truth and stop. `matchMedia`
    // rather than CSS, because there is no CSS that can make a JS
    // interpolation not run.
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      previous.current = target
      return
    }

    const from = previous.current
    previous.current = target
    if (from === target) return

    const started = performance.now()
    const tick = (now: number) => {
      const progress = Math.min(1, (now - started) / DURATION)
      if (progress >= 1) {
        // Hand back to the exact string. Never leave an interpolated value
        // on screen: it is a rounded float standing where money should be.
        setShown(null)
        return
      }
      setShown(from + (target - from) * ease(progress))
      frame.current = requestAnimationFrame(tick)
    }
    frame.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame.current)
  }, [target])

  // `tabular-nums` so the width does not jitter while the digits change —
  // a figure that shifts sideways as it counts reads as broken, not alive.
  return (
    <span className="counter" style={{ fontVariantNumeric: 'tabular-nums' }}>
      {shown === null ? settled : format(shown)}
    </span>
  )
}
