/* Toasts, skeletons, and progressive disclosure. SPEC §17.
 *
 * Three rules this file exists to enforce:
 *
 * 1. **Every action ends in a toast that names what happened in the same word
 *    the button used.** "Push" produces "Pushed", "Discard" produces
 *    "Discarded". A button that says one thing and a confirmation that says
 *    another makes the operator check whether the right thing ran.
 *
 * 2. **Errors say what went wrong and what to do.** Never a bare "failed".
 *    The API already returns actionable messages; `describe()` adds the next
 *    step where the status code implies one.
 *
 * 3. **Explanation that helps once is clutter forever.** `<Why>` keeps it one
 *    click away instead of on the page permanently.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import { ApiError } from '../lib/api'
import { Cross, Tick } from './ui'

type Kind = 'ok' | 'bad' | 'warn' | 'info'
type Toast = { id: number; kind: Kind; title: string; detail?: string }

const ToastContext = createContext<(t: Omit<Toast, 'id'>) => void>(() => {})

export function useToast() {
  return useContext(ToastContext)
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const push = useCallback((t: Omit<Toast, 'id'>) => {
    setToasts((current) => [...current, { ...t, id: Date.now() + Math.random() }])
  }, [])

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((t) => t.id !== id))
  }, [])

  const value = useMemo(() => push, [push])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <ToastItem key={t.id} toast={t} onDismiss={() => dismiss(t.id)} />
        ))}
      </div>
    </ToastContext.Provider>
  )
}

function ToastItem({ toast, onDismiss }: { toast: Toast; onDismiss: () => void }) {
  // Failures stay until dismissed — an error that vanishes before it is read
  // is an error that gets hit twice.
  useEffect(() => {
    if (toast.kind === 'bad') return
    const timer = setTimeout(onDismiss, 5000)
    return () => clearTimeout(timer)
  }, [toast.kind, onDismiss])

  return (
    <div className={`toast toast--${toast.kind}`}>
      {toast.kind === 'ok' && <Tick title="" />}
      {toast.kind === 'bad' && <Cross title="" />}
      <div className="toast__body">
        <div className="toast__title">{toast.title}</div>
        {toast.detail && <div className="toast__detail">{toast.detail}</div>}
      </div>
      <button className="toast__close" onClick={onDismiss} aria-label="Dismiss">
        ×
      </button>
    </div>
  )
}

/** Turn an error into "what went wrong" plus "what to do about it". */
export function describe(error: unknown): { title: string; detail: string } {
  if (error instanceof ApiError) {
    const next: Record<string, string> = {
      rejected: 'Check you exported the .xls from Canara net banking, not a PDF or a print-to-file.',
      invalid: 'The file parsed but did not add up. Re-download the statement rather than editing it.',
      account_mismatch: 'This statement belongs to another account. Nothing was saved.',
      unconfigured: 'Set the missing value in .env on the host, then reload.',
      firefly: 'Firefly did not answer. Check `make ps`, then try again.',
      csrf: 'Reload the page and repeat the action.',
      rate_limited: 'Wait for the lockout to pass, then try again.',
      no_pending: 'Upload a statement first.',
      unknown_category: 'Pick a category that already has a rule, or add the rule first.',
      stale_backup: 'Run `make backup` on the host, then reload this page.',
      empty_archive: 'Nothing in archive/ to re-push — sync a statement first.',
      too_large: 'A Canara three-month export is about 30 KB. This is not that file.',
    }
    return { title: 'That did not work', detail: `${error.message} ${next[error.code] ?? ''}`.trim() }
  }
  return {
    title: 'That did not work',
    detail: error instanceof Error ? error.message : String(error),
  }
}

/**
 * Progressive disclosure for the reasoning behind a screen.
 *
 * The explanations were written when each decision was fresh and were correct
 * to write down — but a paragraph you have read fifty times is furniture. This
 * keeps them one click away and out of the daily path.
 */
export function Why({ label, children }: { label: string; children: ReactNode }) {
  return (
    <details className="why">
      <summary>{label}</summary>
      <div className="why__body">{children}</div>
    </details>
  )
}

/** A shape that says what is coming, rather than that something is happening. */
export function Skeleton({ rows = 3, cards = 0 }: { rows?: number; cards?: number }) {
  return (
    <div aria-hidden="true">
      {cards > 0 && (
        <div className="cards">
          {Array.from({ length: cards }, (_, i) => (
            <div className="skel-card" key={i}>
              <span className="skel skel-row" style={{ width: '35%' }} />
              <span className="skel skel-figure" />
              <span className="skel skel-row" style={{ width: '55%' }} />
            </div>
          ))}
        </div>
      )}
      <div className="sheet" style={{ padding: '1rem 1.15rem' }}>
        {Array.from({ length: rows }, (_, i) => (
          <span
            className="skel skel-row"
            key={i}
            style={{ width: `${92 - (i % 4) * 13}%` }}
          />
        ))}
      </div>
    </div>
  )
}

/** An indeterminate bar for work whose duration we genuinely cannot predict. */
export function Progress({ label }: { label: string }) {
  return (
    <div>
      <span className="muted">{label}</span>
      <div className="progress" role="progressbar" aria-label={label}>
        <div className="progress__bar" />
      </div>
    </div>
  )
}
