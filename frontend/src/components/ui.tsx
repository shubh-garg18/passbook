/* Small shared pieces. SPEC §16.4. */

import { useState } from 'react'
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

export function Eye({ title }: { title?: string }) {
  return (
    <svg className="mark" width="16" height="16" viewBox="0 0 16 16" role="img"
         aria-label={title ?? 'activity'}>
      <path d="M1 8s2.6-4.2 7-4.2S15 8 15 8s-2.6 4.2-7 4.2S1 8 1 8Z" fill="none"
        stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <circle cx="8" cy="8" r="1.9" fill="none" stroke="currentColor" strokeWidth="1.5" />
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
 * The app's mark: the die, the day's track, and one entry struck through it.
 *
 * Not a wallet and not a rupee glyph — those belong to every finance app ever
 * made. This is the stamp die the whole design is built on, and inside it the
 * Day Rail: a 24-hour track with one transaction struck through it.
 *
 * **Redrawn because the first one could not be seen.** It carried the rail as
 * two translucent fills at 22% and 45%, which is legible at 200px and gone at
 * the 26px it actually ships at — on the header's dark ground both washes
 * collapsed into the square and the mark read as a plain blue box. Everything
 * here is now either full ink or knocked out of it, which is what a stamp can
 * actually print.
 *
 * Three shapes, and the tick overhangs the rail top and bottom — that overhang
 * is the entire reading. Without it the mark is two blocks in a box, which is
 * what the first redraw produced and what a pause button looks like. The same
 * artwork is the favicon and the installed-app icon, where it is 16px.
 */
export function StampMark({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 32 32" role="img" aria-label="passbook">
      {/* §82. A ledger column with a rising line through it.
       *
       * Three bare bars said "chart" and nothing else — every analytics tool
       * on earth has that mark, and it did not say *ledger*. This says both:
       * the two grouped uprights are a column of entries, the line rising
       * across them is the reckoning drawn from them, and the dot is where it
       * lands. The mark is a passbook, not a graph.
       *
       * Solid fills and a 2.6 stroke, no tints. The mark it replaces carried a
       * track at `opacity: 0.5` and its own comment recorded why that value
       * had to keep going up: what reads at 200px is gone at the 26px a
       * browser tab renders.
       *
       * Same geometry as `mark()` in `scripts/icons.py`, which generates the
       * favicon and the PWA icons. **Change one and change the other** — the
       * last time they drifted the app shipped four phases with the wrong
       * coloured tab. */}
      <rect x="3.2" y="14.6" width="5.4" height="14.2" rx="1.9" fill="var(--cat-8, currentColor)" opacity="0.55" />
      <rect x="11.1" y="9.4" width="5.4" height="19.4" rx="1.9" fill="var(--cat-2, currentColor)" />
      <path d="M4 12.2 L13.8 6.4 L28 3.2" fill="none" stroke="var(--cat-1, currentColor)"
        strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="27.6" cy="3.4" r="2.9" fill="var(--cat-1, currentColor)" />
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


/**
 * A teller's stamp, landing. The one thing this app should be remembered by.
 *
 * A passbook gets **stamped** — that is the whole ritual of the object, and it
 * is the moment the bank says "recorded". The palette has carried a token
 * called `--stamp` since Phase 13 and nothing has ever actually printed one.
 *
 * It thumps: overshoots, settles, slightly off-square, because a hand-held
 * stamp never lands straight. Everything around it stays quiet — this is the
 * one place the design raises its voice, so it earns being loud by being rare.
 * Used only where something is genuinely confirmed, never as decoration.
 */
export function StampImpression({ label }: { label: string }) {
  return (
    <span className="impress" role="img" aria-label={label}>
      <span className="impress__ring">
        <span className="impress__text">{label}</span>
      </span>
    </span>
  )
}

/**
 * A password box you can read back. SPEC §41.
 *
 * Three pages had their own `<input type="password">` and none could be
 * revealed, which is the wrong default for *this* password: it is not a secret
 * the person chose and remembers, it is a string the bank emailed them and they
 * are copying by hand. "That password did not open the PDF" with no way to see
 * what you typed is a guessing game — and the thing most often wrong with it is
 * a capital letter or a trailing space, both invisible behind dots.
 *
 * It defaults to hidden, because this is still a credential and a shoulder is
 * still a shoulder.
 */
export function PasswordField({
  value,
  onChange,
  invalid,
  onEnter,
  id,
  label = 'PDF password',
  autoFocus,
}: {
  value: string
  onChange: (next: string) => void
  invalid?: boolean
  onEnter?: () => void
  id?: string
  label?: string
  autoFocus?: boolean
}) {
  const [shown, setShown] = useState(false)
  return (
    <span className="secret">
      <input
        id={id}
        type={shown ? 'text' : 'password'}
        autoFocus={autoFocus}
        autoComplete="off"
        // Off for all three: a bank's statement password is not a login and
        // should not be offered up by a password manager, corrected, or
        // capitalised by a phone keyboard.
        autoCorrect="off"
        autoCapitalize="off"
        spellCheck={false}
        placeholder={label}
        aria-label={label}
        aria-invalid={invalid ? true : undefined}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && onEnter) {
            event.preventDefault()
            onEnter()
          }
        }}
      />
      <button
        type="button"
        className="secret__peek"
        aria-pressed={shown}
        aria-label={shown ? 'Hide the password' : 'Show the password'}
        onClick={() => setShown(!shown)}
      >
        {shown ? 'Hide' : 'Show'}
      </button>
    </span>
  )
}
