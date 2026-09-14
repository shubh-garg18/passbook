import { useEffect, useRef, useState } from 'react'
import { Popover } from './components/Popover'
import { useQuery } from '@tanstack/react-query'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'

import { api } from './lib/api'
import { ALL_ACCOUNTS, useAccounts } from './lib/account'
import { applyTheme, readTheme, type ThemeId } from './lib/theme'
import { PALETTES, applyPalette, readPalette, type PaletteId } from './lib/palette'
import type { Session } from './lib/types'
import { Spinner, StampMark } from './components/ui'
import { SignIn } from './pages/SignIn'
import { Enroll } from './pages/Enroll'
import { NotConfigured } from './pages/NotConfigured'
import { Home } from './pages/Home'
import { Upload } from './pages/Upload'
import { TransactionsPage } from './pages/Transactions'
import { Reminder } from './pages/Reminder'
import { Reports } from './pages/Reports'
import { Preview } from './pages/Preview'
import { Result } from './pages/Result'
import { Payees } from './pages/Payees'
import { PayeesDiff } from './pages/PayeesDiff'
import { Reapply } from './pages/Reapply'
import { ReapplyDone } from './pages/ReapplyDone'
import { StatusPage } from './pages/Status'
import { Password } from './pages/Password'
import { AccountsPage } from './pages/Accounts'
import { AddAccount } from './pages/AddAccount'
import { AddBank } from './pages/AddBank'

export function useSession() {
  return useQuery({
    queryKey: ['session'],
    queryFn: () => api.get<Session>('/session'),
    staleTime: 0,
  })
}

/* Three destinations, down from six. SPEC §18.
 *
 * Nothing was removed; three things stopped being destinations:
 *
 *   Re-apply  is not a place, it is the second half of editing a payee. It now
 *             surfaces on Payees, at the moment config is written, where the
 *             operator is already standing. Six nav items meant the step could
 *             be missed entirely — and it was: payees were edited, 8080 kept
 *             showing the old names, and nothing said a second step existed.
 *   Status    is monitoring, and the Ledger already shows the last sync. It is a
 *             strip there, with the artefact tables one click away.
 *   Account   is a setting, not a task. It moved into the header menu.
 *
 * Every route still exists and every action is still reachable.
 */
const NAV = [
  { to: '/', label: 'Ledger', end: true },
  { to: '/upload', label: 'Upload' },
  // §61. Before Payees: browsing is the more common act, and Payees is
  // where you go having found something here.
  { to: '/transactions', label: 'Transactions' },
  { to: '/payees', label: 'Payees' },
  { to: '/reports', label: 'Reports' },
  // A destination, not a setting. It was in the Account menu, which is where
  // things go to be found once and never again — and the whole point of a
  // reminder is that it is easy to change when the habit changes.
  // Was "Add account", which named the task and hid the subject: there was a
  // front door for registering one and no room at all for what is registered,
  // where it posts, or getting rid of one. Adding is now something you do on
  // this page rather than the only thing you can do.
  { to: '/accounts', label: 'Accounts', end: true },
]

// The menu beside it used to be labelled "Account", which put ADD ACCOUNT and
// ACCOUNT next to each other in the bar — two different things one word apart.
// It holds the password, the second factor, the backups and sign out: none of
// that is an account, all of it is the person. So it is the username.

function Cover({ session }: { session: Session }) {
  return (
    <header className="cover">
      <div className="cover__inner">
        <NavLink to="/" className="cover__brand">
          <StampMark className="cover__stamp" />
          <span className="cover__mark">passbook</span>
        </NavLink>
        {session.authenticated && (
          <>
            <CoverId />
            <nav className="cover__nav" aria-label="Main">
              {NAV.map((item) => (
                <NavLink key={item.to} to={item.to} end={item.end}>
                  {item.label}
                </NavLink>
              ))}
              {/* Beside the menu, not inside it: a theme switch is reached
                  mid-task and a menu you must open first is the wrong depth. */}
              <PalettePicker />
              <ThemeToggle />
              <AccountMenu session={session} />
            </nav>
          </>
        )}
      </div>
      {session.authenticated && <AccountTabs />}
    </header>
  )
}

/**
 * The bank under the wordmark.
 *
 * Read from the registry rather than written down. It said `Canara · SB · INR`
 * from Phase 7 until Phase 14, which was true of the only account that could
 * exist then; with a registry it is a claim about whichever account happens to
 * be first, printed next to a switcher that may be showing a different one.
 *
 * With more than one account it says nothing at all — the switcher is right
 * there naming the account, and two labels for one fact is how they drift apart.
 * `SB` is gone because the registry does not record an account type, and a
 * guessed one would be wrong for the first non-savings account added.
 */
function CoverId() {
  const { accounts, multiple } = useAccounts()
  const only = accounts[0]
  if (multiple || !only) return null
  // The bank's name used to sit here. It is chrome, not information: there is
  // one bank, the operator knows which, and a wordmark that names it reads as
  // that bank's own app rather than as a ledger over it. The currency is the
  // part that actually qualifies every figure on the page.
  return <p className="cover__id">INR</p>
}

/**
 * Which account the pages are showing. SPEC §21.9, redrawn in §40.
 *
 * **It was a disclosure, and that was the wrong element.** Which account you are
 * looking at is not a preference tucked into a menu — it is the frame around
 * every figure on the page, and a `<details>` summary reading "Canara ****1111"
 * in a nav bar is a thing the eye slides past. Switch accounts and the whole
 * Ledger changes underneath you with nothing prominent saying it did.
 *
 * So it is a tab strip across the top: every account visible at once, the
 * current one marked, one click to move. That is the same affordance browser
 * tabs would give without any of their cost — no second sign-in, no split
 * cache, and crucially no loss of the thing tabs cannot do at all, which is
 * show two accounts *together*.
 *
 * Combining is what a per-account tab or a per-window setup would have to give
 * up, and §21.9 established it is sound rather than convenient: spend, income,
 * the categories, the roll-ups, the month buckets and the Day Rail are each a
 * sum over transactions, and the exclusions are decided per transaction. So
 * `All` is a tab like any other, and `Combine` opens the checklist for the
 * subsets in between — "these two of my four" is a real question that neither
 * "one" nor "all" answers.
 *
 * Still renders nothing at all with one account registered. A single-account
 * install must never learn this feature exists.
 */
function AccountTabs() {
  const { accounts, multiple, slugs, isAll, isCombined, choose } = useAccounts()
  if (!multiple) return null

  return (
    <div className="tabs" role="tablist" aria-label="Accounts">
      <div className="tabs__inner">
        <div className="tabs__scroll">
        <button
          type="button"
          role="tab"
          aria-selected={isAll}
          className={`tab${isAll ? ' tab--on' : ''}`}
          onClick={() => choose(ALL_ACCOUNTS)}
        >
          All accounts
        </button>
        {accounts.map((account) => {
          // Marked only when it is the WHOLE scope. Under a combination the
          // page is showing several, and lighting two tabs would suggest two
          // pages rather than one over both — the combination gets its own
          // mark on the Combine control instead.
          const on = !isAll && !isCombined && slugs[0] === account.slug
          return (
            <button
              key={account.slug}
              type="button"
              role="tab"
              aria-selected={on}
              className={`tab${on ? ' tab--on' : ''}`}
              onClick={() => choose(account.slug)}
            >
              {account.label}
            </button>
          )
        })}
        </div>
        {/* §115. Outside `.tabs__scroll`, which is the element that scrolls.
            `margin-left: auto` inside it had nothing to push against — a scroll
            container is sized to its own content, so the button sat wherever
            the last tab ended. It is also the container that was clipping the
            panel (§113): the same mistake, twice, in one element. */}
        <Combine />
      </div>
    </div>
  )
}

/** The subsets between "one" and "all". A checklist, because that is what
 *  choosing several of a known set is.
 *
 *  **Nothing below three accounts.** SPEC §109. With two, every subset it can
 *  reach is already a tab: tick both and the scope is `all`, untick one and the
 *  scope is that account. So the control could only ever reproduce a tab, and
 *  the operator's report was the accurate one — *"combine button doesnt
 *  working"*. It did exactly what it was asked and the answer was somewhere
 *  they could already get.
 *
 *  §21.9's justification — *"these two of my four is a real question that
 *  neither one nor all answers"* — is true from three accounts up, and vacuous
 *  below it. The same reasoning as §21.3 hiding the switcher entirely at one:
 *  a control with no reachable state of its own is furniture. */
function Combine() {
  const { accounts, slugs, isAll, isCombined, toggle } = useAccounts()
  if (accounts.length < 3) return null

  const active = isCombined && !isAll
  return (
    <Popover
      className={`combine__button${active ? ' combine__button--on' : ''}`}
      title="Show several accounts together"
      label={active ? `${slugs.length} combined` : 'Combine'}
      panelClassName="combine__panel"
    >
      <p className="muted">Every figure adds the accounts you tick.</p>
      {accounts.map((account) => {
        const on = slugs.includes(account.slug)
        return (
          <label key={account.slug} className="combine__row">
            <input
              type="checkbox"
              checked={on}
              // The last one cannot be unticked: a scope of nothing is not a
              // view, and the server would fall back to the first account
              // anyway — which would look like the click did something else.
              disabled={on && slugs.length === 1}
              onChange={() => toggle(account.slug)}
            />
            <span>
              {account.label} <span className="muted">· {account.assetAccount}</span>
            </span>
          </label>
        )
      })}
    </Popover>
  )
}

/**
 * The settings that are not tasks: status detail, password and second factor,
 * sign out.
 *
 * A `<details>` element, not a scripted dropdown: it opens on click and on
 * Enter, closes on Escape, is reachable by keyboard and announced as a
 * disclosure, all without a line of JavaScript. The one behaviour worth adding
 * is closing after a navigation, so the menu is not still hanging open over the
 * page it just took you to.
 */
function AccountMenu({ session }: { session: Session }) {
  const location = useLocation()
  const ref = useRef<HTMLDetailsElement>(null)

  useEffect(() => {
    if (ref.current) ref.current.open = false
  }, [location.pathname])

  return (
    <details className="menu" ref={ref}>
      <summary aria-label="Your settings and sign out">{session.username ?? 'You'}</summary>
      <div className="menu__panel">
        <p className="menu__who">
          Signed in as <strong>{session.username}</strong>
        </p>
        <NavLink to="/password">Password &amp; second factor</NavLink>
        <NavLink to="/status">Status &amp; backups</NavLink>
        <button
          type="button"
          onClick={async () => {
            await api.del('/session')
            window.location.assign('/')
          }}
        >
          Sign out
        </button>
      </div>
    </details>
  )
}

/**
 * Which eight colours the charts use. SPEC §111.
 *
 * > "there should be a color picker choice for user see better practices for
 * >  that use skill"
 *
 * **A list of measured palettes, not a hex picker.** The better practice here
 * is the restrictive one: eight colours have to stay legible on the card, stay
 * distinct from one another and mean the same thing on every page, and a person
 * choosing eight hexes can check none of that. The first illegible chart would
 * be one they built themselves and could not diagnose.
 *
 * So each option carries the measurement that justifies it — worst contrast
 * against the card — and picking is a click rather than an afternoon. The
 * colour-blind-safe set is the one that earns the control on its own: Okabe-Ito
 * is the standard for red-green colour blindness, which no hue-wheel arithmetic
 * substitutes for.
 *
 * Beside the theme toggle, because both answer "how should this look" and a
 * reader who wants one will look where the other is.
 */
function PalettePicker() {
  const [current, setCurrent] = useState<PaletteId>(() => readPalette())
  const shown = PALETTES.find((p) => p.id === current) ?? PALETTES[0]

  return (
    <Popover
      className="palette__button"
      title="Chart colours"
      panelClassName="palette__panel"
      label={
        <span className="palette__chips" aria-hidden="true">
          {shown!.cats.slice(0, 4).map((hex) => (
            <span key={hex} style={{ background: hex }} />
          ))}
        </span>
      }
    >
      <p className="muted">Chart colours. Every one is measured against the page.</p>
      {PALETTES.map((palette) => (
        <button
          key={palette.id}
          type="button"
          className={`palette__row${palette.id === current ? ' palette__row--on' : ''}`}
          aria-pressed={palette.id === current}
          onClick={() => {
            applyPalette(palette.id)
            setCurrent(palette.id)
          }}
        >
          <span className="palette__chips" aria-hidden="true">
            {palette.cats.map((hex) => (
              <span key={hex} style={{ background: hex }} />
            ))}
          </span>
          <span className="palette__name">{palette.label}</span>
          <span className="muted palette__note">{palette.note}</span>
        </button>
      ))}
    </Popover>
  )
}


/**
 * The theme toggle. SPEC §81.
 *
 * One button in the nav rather than a two-row picker buried in a menu. Two
 * options do not need a grid, and a theme switch is something you reach for
 * mid-task — a menu you have to open first is the wrong depth for it.
 *
 * The icon shows what you will GET, not what you have: a sun when clicking
 * gives you light. That is the convention every OS uses and the opposite
 * choice is a coin-flip for the reader either way, so following the crowd is
 * the whole argument.
 */
function ThemeToggle() {
  const [theme, setTheme] = useState<ThemeId>(() => readTheme())
  const next: ThemeId = theme === 'dark' ? 'light' : 'dark'
  return (
    <button
      type="button"
      className="themetoggle"
      onClick={() => {
        applyTheme(next)
        setTheme(next)
      }}
      aria-label={`Switch to the ${next} theme`}
      title={next === 'light' ? 'Light' : 'Dark'}
    >
      {next === 'light' ? (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9"
          strokeLinecap="round" aria-hidden="true">
          <circle cx="12" cy="12" r="4.2" />
          <path d="M12 2.4v2.2M12 19.4v2.2M2.4 12h2.2M19.4 12h2.2M5.2 5.2l1.6 1.6M17.2 17.2l1.6 1.6M18.8 5.2l-1.6 1.6M6.8 17.2l-1.6 1.6" />
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M20.6 14.2A8.6 8.6 0 0 1 9.8 3.4a8.6 8.6 0 1 0 10.8 10.8Z" />
        </svg>
      )}
    </button>
  )
}

export default function App() {
  const { data: session, isPending } = useSession()
  const location = useLocation()

  if (isPending || !session) {
    return (
      <div className="page">
        <Spinner what="passbook" />
      </div>
    )
  }

  return (
    <>
      <a className="skip" href="#main">
        Skip to content
      </a>
      <Cover session={session} />
      <main id="main">
        {!session.authenticated ? (
          // A restored install has no credential file at all — offering a
          // sign-in form that can only ever fail is what the DR drill hit.
          !session.configured ? (
            <NotConfigured />
          ) : session.stage === 'enroll' ? (
            <Enroll />
          ) : (
            <SignIn stage={session.stage} />
          )
        ) : (
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/transactions" element={<TransactionsPage />} />
            <Route path="/upload" element={<Upload />} />
            <Route path="/preview" element={<Preview />} />
            <Route path="/result" element={<Result />} />
            <Route path="/payees" element={<Payees />} />
            <Route path="/reports" element={<Reports />} />
            <Route path="/reminder" element={<Reminder />} />
            <Route path="/payees/diff" element={<PayeesDiff />} />
            <Route path="/reapply" element={<Reapply />} />
            <Route path="/reapply/done" element={<ReapplyDone />} />
            <Route path="/status" element={<StatusPage />} />
            <Route path="/password" element={<Password />} />
            <Route path="/accounts" element={<AccountsPage />} />
            <Route path="/accounts/add" element={<AddAccount />} />
            <Route path="/banks/add" element={<AddBank />} />
            <Route path="*" element={<Navigate to="/" replace state={{ from: location.pathname }} />} />
          </Routes>
        )}
      </main>
    </>
  )
}
