/* Themes: a bank accent, and a light/dark mode. SPEC §65, §77, §78.
 *
 * Two independent axes, and the second one exists because of the first.
 *
 * **The mode is always stamped, never left to a media query.** `data-mode` is
 * resolved here — the operator's explicit choice, else the theme's own
 * default, else the OS — and written to the root before the first paint, so
 * the DOM never sits in an ambiguous "auto" state and the CSS needs one
 * selector per mode instead of a selector plus a media query.
 *
 * That is what lets a bank theme default to LIGHT on a machine whose OS is
 * dark. Canara's own standard is white and light blue; following the OS would
 * have made the bank's actual palette unreachable for anyone in dark mode,
 * which defeats the point of having it. A media query cannot express "unless
 * this particular theme says otherwise".
 *
 * **A theme is the accent and the surfaces. It is never the signals.**
 * `--ochre` asks, `--verdigris` reconciles, `--alarm` failed, and the eight
 * categorical hues were measured against these grounds — a theme that moved
 * any of them would need all of it re-measured, and the first one to skip that
 * ships an unreadable chart or an alarm that no longer means alarm. Two tests
 * enforce it.
 */

/* §81. Two themes, not four.
 *
 *
 * Union and SBI are gone. They were built because a theme-per-bank sounded
 * right, and the operator banks with one of them — three quarters of the
 * picker was for institutions they have no account at, and a control whose
 * options are mostly irrelevant is a control nobody reads.
 *
 * What survives is the pair that means something here: **Canara's own white
 * and light blue is the light theme, the indigo-on-near-black is the
 * dark one.** Light and dark stop being an axis crossed with a brand and
 * become the two things they always were, which also collapses the two-row
 * picker into one toggle in the nav.
 *
 * The Union and SBI hexes stay recorded in SPEC §77 — they were read off those
 * banks' own stylesheets and would be tedious to recover.
 */
export type ThemeId = 'dark' | 'light'
export type Mode = ThemeId

export const THEMES: { id: ThemeId; label: string; swatch: string }[] = [
  { id: 'dark', label: 'Dark', swatch: '#7c83f5' },
  // Canara's real pair: navy #2f2483 on white, read off canarabank.com.
  { id: 'light', label: 'Light', swatch: '#2f2483' },
]

const THEME_KEY = 'passbook.theme'

/** localStorage that cannot throw. A private window, or site data blocked. */
function read(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // The choice will not survive a reload. That is a worse experience, not a
    // broken one — it still applied.
  }
}

/** The stored choice, else the OS, else dark — this app's own default. */
export function readTheme(): ThemeId {
  const saved = read(THEME_KEY)
  if (saved === 'light' || saved === 'dark') return saved
  return typeof window !== 'undefined' &&
    window.matchMedia?.('(prefers-color-scheme: light)').matches
    ? 'light'
    : 'dark'
}

/** Stamp it on the root. Call before the first paint. */
export function applyTheme(theme: ThemeId): void {
  document.documentElement.setAttribute('data-mode', theme)
  write(THEME_KEY, theme)
}

/** Follow the OS until the operator has chosen for themselves. */
export function watchSystemMode(): () => void {
  if (typeof window === 'undefined' || !window.matchMedia) return () => {}
  const query = window.matchMedia('(prefers-color-scheme: light)')
  const onChange = () => {
    if (read(THEME_KEY) === null) applyTheme(query.matches ? 'light' : 'dark')
  }
  query.addEventListener('change', onChange)
  return () => query.removeEventListener('change', onChange)
}
