/* Small shared pieces. SPEC §16.4. */

import type { ReactNode } from 'react'
import { formatAmount, formatINR } from '../lib/money'

/* Status marks are SVG, not `✓`/`✗` characters: those glyphs are absent from
 * the subset Anek and Mukta faces and would silently fall back to a system
 * font mid-line. Drawing them also lets them inherit the semantic colour. */

export function Tick({ title }: { title?: string }) {
  return (
    <svg className="mark" width="13" height="13" viewBox="0 0 16 16" role="img" aria-label={title ?? 'passed'}>
      <path d="M3 8.5 6.5 12 13 4.5" fill="none" stroke="currentColor" strokeWidth="2.2"
        strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export function Cross({ title }: { title?: string }) {
  return (
    <svg className="mark" width="13" height="13" viewBox="0 0 16 16" role="img" aria-label={title ?? 'failed'}>
      <path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
    </svg>
  )
}

export function Arrow() {
  return (
    <svg className="mark" width="14" height="10" viewBox="0 0 16 10" aria-hidden="true">
      <path d="M1 5h13M10 1l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.6"
        strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export function Money({ value, plain }: { value: string | null; plain?: boolean }) {
  return <>{plain ? formatAmount(value) : formatINR(value)}</>
}

/**
 * A bank token, with the cut made visible.
 *
 * Canara truncates the counterparty to about ten characters and D10 measured a
 * 40% error rate on reading meaning out of that fragment. The mark is there so
 * the fragment never reads as a whole name.
 */
export function Token({ token, strong }: { token: string | null; strong?: boolean }) {
  if (!token) return <span className="tok">(unparsed)</span>
  const truncated = token.length >= 9
  return (
    <span className={strong ? 'tok tok--strong' : 'tok'}>
      {token}
      {truncated && <span className="tok__cut" role="img" aria-label="name truncated by the bank" />}
    </span>
  )
}

/**
 * The app's mark: the Day Rail, stamped.
 *
 * Not a wallet and not a rupee glyph — those belong to every finance app ever
 * made. This is the one shape that is only ours: a 24-hour track with the
 * midnight-to-six band shaded and a single transaction tick. The same artwork
 * is the favicon and the installed-app icon.
 */
export function StampMark({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 32 32" role="img" aria-label="passbook">
      <rect x="1.5" y="1.5" width="29" height="29" rx="5"
        fill="none" stroke="currentColor" strokeWidth="2.5" />
      <rect x="6.5" y="12" width="19" height="8" rx="1.5"
        fill="currentColor" opacity="0.22" />
      <rect x="6.5" y="12" width="4.75" height="8" rx="1.5" fill="currentColor" opacity="0.45" />
      <rect x="9" y="10.5" width="2.6" height="11" rx="1" fill="currentColor" />
    </svg>
  )
}

export function Notice({
  kind = 'info',
  children,
}: {
  kind?: 'info' | 'warn' | 'bad' | 'ok'
  children: ReactNode
}) {
  const cls = kind === 'info' ? '' : ` notice--${kind}`
  return <div className={`notice${cls}`}>{children}</div>
}

export function Card({
  title,
  state,
  children,
}: {
  title?: string
  state?: 'ok' | 'warn' | 'bad'
  children: ReactNode
}) {
  return (
    <section className={`card${state ? ` card--${state}` : ''}`}>
      {title && <h2>{title}</h2>}
      {children}
    </section>
  )
}

export function Spinner({ what }: { what: string }) {
  return (
    <p className="spinner" role="status">
      Loading {what}…
    </p>
  )
}

export function ErrorNote({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error)
  return (
    <Notice kind="bad">
      <p>
        <Cross /> {message}
      </p>
    </Notice>
  )
}

/** A unified diff, coloured. Line prefixes decide, exactly as `diff` emits them. */
export function Diff({ text }: { text: string }) {
  return (
    <pre className="diff">
      {text.split('\n').map((line, index) => {
        let cls = ''
        if (line.startsWith('+') && !line.startsWith('+++')) cls = 'add'
        else if (line.startsWith('-') && !line.startsWith('---')) cls = 'del'
        else if (line.startsWith('@@')) cls = 'hunk'
        return (
          <span key={index} className={cls}>
            {line || ' '}
          </span>
        )
      })}
    </pre>
  )
}
