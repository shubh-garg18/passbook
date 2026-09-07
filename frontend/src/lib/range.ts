/* The date window the Ledger and Payees are read through. SPEC §25.
 *
 * Firefly has one and the two are read side by side, but the reason it is here
 * is Payees: deciding on the eleven tokens that appeared last month is a task
 * with an end, and scrolling the same fifty-nine every week hunting for the new
 * ones is not.
 *
 * **Held in the URL, unlike the account selection.** The two look similar and
 * are not: an account is a view preference that must survive a reload, so it
 * lives in `localStorage`; a window is part of what you are looking at. In the
 * URL it survives reload AND back/forward, and a link to "last month's payees"
 * is a link to last month's payees. `lib/account.ts` has the same note from the
 * other side.
 *
 * The server resolves every named range (`_date_scope`), so the page never
 * decides where a month starts and the two cannot drift.
 */

import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

export type RangeName = 'month' | 'last-month' | '3m' | '6m' | 'year' | 'all' | 'custom'

export const PRESETS: { value: Exclude<RangeName, 'custom'>; label: string }[] = [
  { value: 'month', label: 'This month' },
  { value: 'last-month', label: 'Last month' },
  { value: '3m', label: 'Last 3 months' },
  { value: '6m', label: 'Last 6 months' },
  { value: 'year', label: 'This year' },
  { value: 'all', label: 'Everything' },
]

/** Everything, because a ledger's first question is "what is in here". */
export const DEFAULT_RANGE: RangeName = 'all'

export type Window = { range: RangeName; from: string | null; to: string | null }

export function useRange() {
  const [params, setParams] = useSearchParams()

  const from = params.get('from') ?? ''
  const to = params.get('to') ?? ''
  const named = (params.get('range') ?? '') as RangeName
  const custom = Boolean(from || to)
  const range: RangeName = custom
    ? 'custom'
    : PRESETS.some((p) => p.value === named)
      ? named
      : DEFAULT_RANGE

  const choose = useCallback(
    (value: RangeName) => {
      const next = new URLSearchParams(params)
      // A preset and an explicit window are alternatives, not layers — leaving
      // a stale `from` behind would silently outrank the preset just chosen,
      // because the server reads an explicit date first.
      next.delete('from')
      next.delete('to')
      if (value === DEFAULT_RANGE) next.delete('range')
      else next.set('range', value)
      setParams(next, { replace: true })
    },
    [params, setParams],
  )

  const setBounds = useCallback(
    (nextFrom: string, nextTo: string) => {
      const next = new URLSearchParams(params)
      next.delete('range')
      if (nextFrom) next.set('from', nextFrom)
      else next.delete('from')
      if (nextTo) next.set('to', nextTo)
      else next.delete('to')
      setParams(next, { replace: true })
    },
    [params, setParams],
  )

  /** What a query appends. Empty for the default, so those requests are
   *  byte-identical to what they were before this feature existed. */
  const parts: string[] = []
  if (custom) {
    if (from) parts.push(`from=${encodeURIComponent(from)}`)
    if (to) parts.push(`to=${encodeURIComponent(to)}`)
  } else if (range !== DEFAULT_RANGE) {
    parts.push(`range=${range}`)
  }

  return {
    range,
    from,
    to,
    choose,
    setBounds,
    /** Query string fragment WITHOUT a leading `?` or `&` — the caller joins. */
    query: parts.join('&'),
    /** For a react-query key: changes exactly when the window does. */
    key: parts.join('&') || 'all',
  }
}

/** `?a=1&b=2` from parts that may each be empty. */
export function joinQuery(...parts: string[]): string {
  const kept = parts.filter(Boolean).map((p) => p.replace(/^[?&]/, ''))
  return kept.length ? `?${kept.join('&')}` : ''
}
