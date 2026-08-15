import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'

import { api } from './lib/api'
import { ALL_ACCOUNTS, useAccounts } from './lib/account'
import type { Session } from './lib/types'
import { Spinner, StampMark } from './components/ui'
import { SignIn } from './pages/SignIn'
import { Enroll } from './pages/Enroll'
import { NotConfigured } from './pages/NotConfigured'
import { Home } from './pages/Home'
import { Upload } from './pages/Upload'
import { Preview } from './pages/Preview'
import { Result } from './pages/Result'
import { Payees } from './pages/Payees'
import { PayeesDiff } from './pages/PayeesDiff'
import { Reapply } from './pages/Reapply'
import { ReapplyDone } from './pages/ReapplyDone'
import { StatusPage } from './pages/Status'
import { Password } from './pages/Password'

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
  { to: '/payees', label: 'Payees' },
]

function Cover({ session }: { session: Session }) {
  return (
    <header className="cover">
      <div className="cover__inner">
        <NavLink to="/" className="cover__brand">
          <StampMark className="cover__stamp" />
          <span className="cover__mark">Passbook</span>
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
              <AccountSwitcher />
              <AccountMenu session={session} />
            </nav>
          </>
        )}
      </div>
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
  const bank = only.bank
  return (
    <p className="cover__id">
      {bank.charAt(0).toUpperCase() + bank.slice(1)} &middot; INR
    </p>
  )
}

/**
 * Which account the pages are showing. SPEC §21.9.
 *
 * **Renders nothing at all unless more than one account is registered**, and the
 * server decides that (`/api/accounts.multiple`) so the client cannot get it
 * wrong. Someone with one account never sees a control, a label, or a hint that
 * accounts are a concept here.
 *
 * A native `<select>`: it is one tap on a phone, it is keyboard-navigable without
 * a single handler, and it announces itself. The Day Rail earned an SVG because
 * no element does that job; a dropdown is not that.
 */
function AccountSwitcher() {
  const { accounts, multiple, selected, choose } = useAccounts()
  if (!multiple) return null
  return (
    <label className="switcher">
      <span className="visually-hidden">Account</span>
      <select
        value={selected ?? ''}
        onChange={(event) => choose(event.target.value)}
        aria-label="Account"
      >
        {accounts.map((account) => (
          <option key={account.slug} value={account.slug}>
            {account.label} · {account.account}
          </option>
        ))}
        <option value={ALL_ACCOUNTS}>All accounts</option>
      </select>
    </label>
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
      <summary aria-label="Account menu">Account</summary>
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
            <Route path="/upload" element={<Upload />} />
            <Route path="/preview" element={<Preview />} />
            <Route path="/result" element={<Result />} />
            <Route path="/payees" element={<Payees />} />
            <Route path="/payees/diff" element={<PayeesDiff />} />
            <Route path="/reapply" element={<Reapply />} />
            <Route path="/reapply/done" element={<ReapplyDone />} />
            <Route path="/status" element={<StatusPage />} />
            <Route path="/password" element={<Password />} />
            <Route path="*" element={<Navigate to="/" replace state={{ from: location.pathname }} />} />
          </Routes>
        )}
      </main>
    </>
  )
}
