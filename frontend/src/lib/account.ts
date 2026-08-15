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

import { useCallback, useEffect, useState } from 'react'
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

function store(slug: string | null): void {
  try {
    if (slug) window.localStorage.setItem(KEY, slug)
    else window.localStorage.removeItem(KEY)
  } catch {
    /* see above */
  }
}

/**
 * The registry, plus the current selection.
 *
 * `param` is what queries append. It is an empty string for a single-account
 * install, so those requests are byte-identical to what they were before this
 * phase — one account behaves exactly as it did.
 */
export function useAccounts() {
  const { data } = useQuery({
    queryKey: ['accounts'],
    queryFn: () => api.get<Accounts>('/accounts'),
    staleTime: 30_000,
  })

  const [selected, setSelected] = useState<string | null>(() => storedAccount())

  // Validate the stored slug against the registry once it arrives: an account
  // that has been removed, or a slug from someone else's install, must not pin
  // the page to something that no longer exists.
  useEffect(() => {
    if (!data) return
    const known = new Set([...data.accounts.map((a) => a.slug), ALL_ACCOUNTS])
    if (selected && !known.has(selected)) {
      setSelected(null)
      store(null)
    }
  }, [data, selected])

  const choose = useCallback((slug: string) => {
    setSelected(slug)
    store(slug)
  }, [])

  const multiple = data?.multiple ?? false
  // With one account there is nothing to scope and nothing to remember.
  const effective = multiple ? (selected ?? data?.accounts[0]?.slug ?? null) : null

  return {
    accounts: data?.accounts ?? [],
    multiple,
    selected: effective,
    choose,
    /** `?account=…`, or '' when there is nothing to say. */
    param: effective ? `?account=${encodeURIComponent(effective)}` : '',
    isAll: effective === ALL_ACCOUNTS,
    label:
      effective === ALL_ACCOUNTS
        ? 'All accounts'
        : (data?.accounts.find((a) => a.slug === effective)?.label ?? ''),
  }
}
