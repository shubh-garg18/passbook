/* A panel that escapes whatever it is nested inside. SPEC §113.
 *
 * **An absolutely-positioned child of a scroll container is clipped to it.**
 * That is CSS working as specified and it made two controls invisible:
 *
 *   .tabs__inner   overflow-x: auto  ->  the Combine checklist
 *   .sheet__scroll overflow-x: auto  ->  the credit-card Split panel
 *
 * Both opened correctly, below their button, outside the container's box, and
 * were painted nowhere. The buttons looked dead — *"combine button doesnt
 * working ... basically dropdown not shows up"* — and the panels were fine.
 *
 * Worse: both were "verified" by a Playwright driver that clicked the trigger,
 * read `inner_text()` off the panel and typed into its inputs. Every one of
 * those works on a clipped element, because the DOM is there. **A driver that
 * reads the DOM has not looked at the page** — the same lesson `shoot.py`
 * exists for, learned again one level down.
 *
 * So the panel renders into a portal at `document.body`, where no ancestor can
 * clip it, and is positioned from the trigger's own rect. Closes on Escape, on
 * a click outside, and on a route change.
 */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useLocation } from 'react-router-dom'

export function Popover({
  label,
  title,
  className = '',
  panelClassName = '',
  children,
}: {
  /** What the trigger shows. A node, so a row of colour chips works. */
  label: React.ReactNode
  title?: string
  className?: string
  panelClassName?: string
  children: React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  const [at, setAt] = useState<{ top: number; right: number } | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const location = useLocation()

  useEffect(() => setOpen(false), [location.pathname])

  // Measured after layout and before paint, so the panel never appears at the
  // wrong place first. `right` rather than `left`: these hang off the end of a
  // strip or a row, and a panel that runs off the right edge is the bug one
  // over from the one this component exists for.
  useLayoutEffect(() => {
    if (!open || !trigger.current) return
    const rect = trigger.current.getBoundingClientRect()
    setAt({ top: rect.bottom + 6, right: Math.max(8, window.innerWidth - rect.right) })
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    const onDown = (event: MouseEvent) => {
      const target = event.target as Node
      if (panel.current?.contains(target) || trigger.current?.contains(target)) return
      setOpen(false)
    }
    // Capture, so a click inside another popover's trigger closes this one
    // before that one opens — two panels at once is never what was meant.
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onDown, true)
    window.addEventListener('resize', () => setOpen(false), { once: true })
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onDown, true)
    }
  }, [open])

  return (
    <>
      <button
        ref={trigger}
        type="button"
        className={className}
        title={title}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((was) => !was)}
      >
        {label}
      </button>
      {open &&
        at &&
        createPortal(
          <div
            ref={panel}
            className={`pop ${panelClassName}`}
            style={{ top: at.top, right: at.right }}
            role="dialog"
          >
            {children}
          </div>,
          document.body,
        )}
    </>
  )
}
