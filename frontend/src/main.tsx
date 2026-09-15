import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'

import App from './App'
import { ToastProvider } from './components/feedback'
import { applyTheme, readTheme, watchSystemMode } from './lib/theme'
import { applyPalette, readPalette } from './lib/palette'
import './theme.css'

// Before the first paint, not in a component effect. Applied in an effect the
// app renders once in the default accent and then repaints in the chosen one —
// a visible flash on every load, and the kind of thing that only shows up on a
// real machine rather than in a test.
applyTheme(readTheme())
// §111. Before the first paint, like the theme: a chart drawn in the default
// eight and then repainted in the chosen eight is a flash of the wrong answer.
applyPalette(readPalette())
watchSystemMode()

// Server state only. There is no client store and no Zustand: everything this
// app knows comes from the API, and a store here would end up managing a
// modal. SPEC §16.1.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Come back to the tab and the numbers are current. This was off, on the
      // grounds that re-parsing the archive per focus is real work — but the
      // work is bounded by `staleTime` below, so a focus inside 15s costs
      // nothing, and the thing being avoided was never the expensive case.
      //
      // What it was actually costing: a laptop that had been shut for a week
      // showed last week's ledger until something happened to invalidate it,
      // and nothing on a read-only page ever does.
      refetchOnWindowFocus: true,
      // Same reasoning for coming back online after a suspend.
      refetchOnReconnect: true,
      retry: (count, error) => {
        const status = (error as { status?: number }).status
        // Never retry a rejection the server meant: 401, 403, 422 are answers.
        if (status && status < 500) return false
        return count < 2
      },
      // Long enough that clicking between pages is instant, short enough that
      // returning to the window shows the current state.
      staleTime: 15_000,
      // Whatever page is open re-reads itself every minute, so a push made in
      // another window — or by `make sync` in a terminal — shows up here
      // without a reload. 60s rather than 10s deliberately: `/analysis`
      // re-parses the whole archive, and six of those a minute per open tab is
      // real work on a container serving one person.
      refetchInterval: 60_000,
      // Only while the tab is actually being looked at. A minimised window
      // polling all day is the same work for nobody's benefit.
      refetchIntervalInBackground: false,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ToastProvider>
          <App />
        </ToastProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
