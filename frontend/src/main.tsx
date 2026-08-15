import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'

import App from './App'
import { ToastProvider } from './components/feedback'
import './theme.css'

// Server state only. There is no client store and no Zustand: everything this
// app knows comes from the API, and a store here would end up managing a
// modal. SPEC §16.1.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // One operator, one laptop. Refetching on every window focus is noise,
      // and re-parsing 93 rows per focus is real work on the server.
      refetchOnWindowFocus: false,
      retry: (count, error) => {
        const status = (error as { status?: number }).status
        // Never retry a rejection the server meant: 401, 403, 422 are answers.
        if (status && status < 500) return false
        return count < 2
      },
      staleTime: 15_000,
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
