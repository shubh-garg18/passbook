/* Upload. SPEC §16.1, §6.7.
 *
 * Validation happens before the file is saved: magic bytes (never the
 * extension), size, the balance-continuity invariant, and the account
 * assertion. A file that fails any of them is deleted rather than left in
 * inbox/ where a later `make sync` would find it. Same step count as Phase 9 —
 * choose, submit, review, push.
 */

import { useMutation } from '@tanstack/react-query'
import { useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api } from '../lib/api'
import type { Parsed } from '../lib/types'
import { Notice } from '../components/ui'
import { Progress, Why, describe, useToast } from '../components/feedback'

export function Upload() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()
  const input = useRef<HTMLInputElement>(null)

  const upload = useMutation({
    mutationFn: (form: FormData) => api.upload<Parsed>('/statement', form),
    onSuccess: (parsed) => {
      queryClient.setQueryData(['pending'], parsed)
      toast({
        kind: 'ok',
        title: 'Checked',
        detail: `${parsed.count} rows, continuity clean, account matches. Nothing pushed yet.`,
      })
      navigate('/preview')
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  // Placement is still the force, but the trigger moved: PDFs are parsed now
  // (SPEC §6.8), so the advice follows a PDF that WORKED rather than one that
  // was refused. Same moment either way — a PDF in hand is when reaching for
  // an online converter becomes tempting.
  const [sawPdf, setSawPdf] = useState(false)

  return (
    <div className="page page--narrow">
      <h1>Upload a statement</h1>

      <form
        className="card"
        onSubmit={(event) => {
          event.preventDefault()
          const file = input.current?.files?.[0]
          if (!file) return
          setSawPdf(file.name.toLowerCase().endsWith('.pdf'))
          const form = new FormData()
          form.append('statement', file)
          upload.mutate(form)
        }}
      >
        <label htmlFor="statement">
          Canara XLS export
          <input id="statement" ref={input} type="file" name="statement" required />
        </label>
        <button type="submit" className="primary" disabled={upload.isPending}>
          {upload.isPending ? 'Checking…' : 'Upload and check'}
        </button>
        {upload.isPending && <Progress label="Parsing and validating" />}
      </form>

      {/* Plain. A standing prohibition is never "waiting on me", so ochre
          costs the semantic and buys nothing — a permanent banner is wallpaper
          within a week. When it needs force it gets force from PLACEMENT: the
          block below fires only when a rejected upload turns out to be a PDF,
          which is the exact moment reaching for a converter becomes tempting. */}
      <Notice>
        <p>
          <strong>Never upload a statement to an online converter.</strong> It carries your
          account number, customer ID and counterparty phone numbers.
        </p>
      </Notice>

      {sawPdf && (
        <Notice kind="warn">
          <p>
            <strong>That was a PDF, and it parsed — but never send one to a converter.</strong>{' '}
            Its protection is nominal: RC4-40 over a four-digit password. Anyone who
            receives the file reads your account number, customer ID, address and every
            counterparty's phone number. XLS stays the primary format (D4); PDF is the
            fallback for weeks when net banking gives you nothing else.
          </p>
        </Notice>
      )}

      <Why label="What gets checked">
        <p>
          Magic bytes (never the extension), size, the balance-continuity invariant, and that
          the account number matches the one configured. A file failing any of these is
          deleted rather than left in <code>inbox/</code> where a later <code>make sync</code>
          would find it. Nothing is pushed until you confirm.
        </p>
      </Why>
    </div>
  )
}
