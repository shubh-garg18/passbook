/* Which account the pages are showing. SPEC §21.9.
 *
 * **A single-account install must never learn this feature exists.** The server
 * says whether more than one account is registered (`/api/accounts.multiple`),
 * and the switcher renders only then — the client never has to decide, and there
 * is no "1 of 1" dropdown to explain.
 *
 * The selection is held in `localStorage`, not in the URL. It has to survive a
 * reload (a query param would not, unless every link carried it) and it is a view
 * preference rather than an address: two accounts are the same ledger seen two
 * ways, and a shared link that silently reframed someone else's page would be
 * worse than one that does not carry the state at all.
 *
 * Every read endpoint takes `?account=<slug|all>` and falls back to the first
 * account for a slug it does not know, so a stale selection left in a browser
 * after an account is removed shows data rather than an error.
 */

import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from './api'
import type { Accounts } from './types'

const KEY = 'passbook.account'

/** `all` is a real scope, not a sentinel for "none". */
export const ALL_ACCOUNTS = 'all'

export function storedAccount(): string | null {
  try {
    return window.localStorage.getItem(KEY)
  } catch {
    // Private-mode Safari throws on localStorage. Losing the selection across a
    // reload is a smaller failure than a page that will not render.
    return null
  }
}

/* The selection is ONE value, and every component reads the same one. §100.
 *
 * It used to be `useState(() => storedAccount())` inside `useAccounts`, which
 * gives each caller its own copy of it — and `useAccounts` is called by the tab
 * strip, by the Combine checklist and by every page that scopes a query. So
 * clicking a tab moved that tab strip's own highlight, wrote localStorage, and
 * told nobody: the page underneath kept querying the account it was already
 * showing. The operator's words were *"All accounts Canara Main address and
 * combine are stale buttons as of now"*, and they were describing exactly this.
 *
 * A one-account install never rendered a switcher, so nothing could reveal it
 * until a second account was registered (§95).
 *
 * `useSyncExternalStore` is the right shape for this: the value lives outside
 * React, in `localStorage`, and several components have to see it change.
 * Subscribing to `storage` as well means two browser tabs agree — that event
 * fires only in the OTHER tab, which is why the local write emits by hand.
 */
const listeners = new Set<() => void>()

function subscribe(notify: () => void): () => void {
  listeners.add(notify)
  window.addEventListener('storage', notify)
  return () => {
    listeners.delete(notify)
    window.removeEventListener('storage', notify)
  }
}

function store(slug: string | null): void {
  try {
    if (slug) window.localStorage.setItem(KEY, slug)
    else window.localStorage.removeItem(KEY)
  } catch {
    /* see above */
  }
  for (const notify of listeners) notify()
}

/**
 * The registry, plus the current selection.
 *
 * `param` is what queries append. It is an empty string for a single-account
 * install, so those requests are byte-identical to what they were before this
 * phase — one account behaves exactly as it did.
 */
export function useAccounts() {
  const { data, isPending } = useQuery({
    queryKey: ['accounts'],
    queryFn: () => api.get<Accounts>('/accounts'),
    staleTime: 30_000,
  })

  // `getSnapshot` must return a stable value for an unchanged store, and this
  // one returns a string or null — compared by value, so no cache is needed.
  // The third argument is the server snapshot; there is no SSR here, and `null`
  // is what a first render sees anyway.
  const selected = useSyncExternalStore(subscribe, storedAccount, () => null)

  // Validate the stored slug against the registry once it arrives: an account
  // that has been removed, or a slug from someone else's install, must not pin
  // the page to something that no longer exists.
  useEffect(() => {
    if (!data) return
    const known = new Set([...data.accounts.map((a) => a.slug), ALL_ACCOUNTS])
    // A subset is valid if every slug in it still exists. One removed account
    // must not pin the page to a scope that can no longer be built.
    const valid =
      selected === null ||
      known.has(selected) ||
      selected.split(',').every((slug) => known.has(slug))
    if (!valid) store(null)
  }, [data, selected])

  const choose = useCallback((slug: string) => store(slug), [])

  const multiple = data?.multiple ?? false
  // With one account there is nothing to scope and nothing to remember.
  const effective = multiple ? (selected ?? data?.accounts[0]?.slug ?? null) : null

  const known = data?.accounts ?? []
  // A selection is a slug, `all`, or a comma-separated subset. Split here once
  // so no caller has to know that.
  const slugs =
    effective && effective !== ALL_ACCOUNTS
      ? effective.split(',').filter(Boolean)
      : known.map((a) => a.slug)

  /** Add or remove one account from the current scope. Never empties: a scope
   *  of nothing is not a view, and the server would silently fall back to the
   *  first account anyway — better to refuse the last removal here. */
  const toggle = (slug: string) => {
    const next = new Set(slugs)
    if (next.has(slug)) {
      if (next.size === 1) return
      next.delete(slug)
    } else {
      next.add(slug)
    }
    const ordered = known.filter((a) => next.has(a.slug)).map((a) => a.slug)
    choose(ordered.length === known.length ? ALL_ACCOUNTS : ordered.join(','))
  }

  const label =
    effective === ALL_ACCOUNTS
      ? 'All accounts'
      : slugs.length > 1
        ? `${slugs.length} accounts`
        : (known.find((a) => a.slug === effective)?.label ?? '')

  /**
   * Which account these figures are, always — even with one registered. §40.
   *
   * `label` is the *switcher's* label and is empty for a single account,
   * because §21.3 says a one-account install must never learn the feature
   * exists. This is a different question: naming the account a page is showing
   * is identification, not a control, and a passbook has the account number
   * printed on it. So the masthead and the Payees lede use this one.
   */
  const scopeLabel = multiple ? label : (known[0]?.label ?? '')

  return {
    accounts: known,
    multiple,
    selected: effective,
    /** The slugs actually in scope, expanded from `all`. */
    slugs,
    choose,
    toggle,
    /** `?account=…`, or '' when there is nothing to say. */
    param: effective ? `?account=${encodeURIComponent(effective)}` : '',
    isAll: effective === ALL_ACCOUNTS,
    /** More than one account's rows are being combined. */
    isCombined: effective === ALL_ACCOUNTS || slugs.length > 1,
    label,
    scopeLabel,
    /** Still asking. `accounts` is empty both before the answer arrives and
     *  when there genuinely are none, and a page that opens on "you have
     *  nothing" for a moment before showing a ledger is worse than one that
     *  waits. */
    isPending,
  }
}
