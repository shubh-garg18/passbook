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
import { invalidateLedger } from '../lib/ledger'
import { Cross, Notice, PasswordField, Tick } from '../components/ui'
import { Progress, Why, describe, useToast } from '../components/feedback'

export function Upload() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()
  const input = useRef<HTMLInputElement>(null)

  // Most Indian banks hand out password-protected PDFs. Decryption is pikepdf,
  // in the container, on this machine — an online "PDF unlocker" would be
  // handed the account number, the customer ID, the address and every
  // counterparty (non-negotiable 7). The password is typed here, used for the
  // request, and never stored. §30.
  const [needsPassword, setNeedsPassword] = useState(false)
  const [password, setPassword] = useState('')
  // The message from a REJECTED password, shown beside the field. Empty means
  // "we have not tried one yet". §41.
  const [wrong, setWrong] = useState('')
  const chosen = useRef<File | null>(null)

  const upload = useMutation({
    mutationFn: (form: FormData) => api.upload<Parsed>('/statement', form),
    onSuccess: (parsed) => {
      queryClient.setQueryData(['pending'], parsed)
      // A staged file is already counted by /payees, /overview and /analysis —
      // `_all_transactions` includes `session['pending']`. Without this the
      // Payees caption keeps describing the set from before the upload.
      invalidateLedger(queryClient)
      toast({
        kind: 'ok',
        title: 'Checked',
        detail: `${parsed.count} rows, continuity clean, account matches. Nothing pushed yet.`,
      })
      setNeedsPassword(false)
      setPassword('')
      setWrong('')
      // Set HERE and not on submit. The block this drives opens with "That was
      // a PDF, and it parsed" — and on submit it fired for a PDF that had just
      // been REJECTED, printing a sentence that was not true underneath the
      // error saying so. Its own comment always said "a PDF that WORKED".
      setSawPdf((chosen.current?.name ?? '').toLowerCase().endsWith('.pdf'))
      // §112.2. Seed Preview with the statement the server just returned, and
      // invalidate behind it. Neither was done: `/preview` reads `['pending']`,
      // and on a second upload in one session that key still held the FIRST
      // statement — so the page rendered the wrong file until it was reloaded
      // by hand. The operator's words were "preview page also needs refresh".
      //
      // Seeding rather than only invalidating, because the response already IS
      // the answer: a refetch here is a round trip to be told what we were just
      // told, and the gap is where the stale rows were showing.
      queryClient.setQueryData(['pending'], parsed)
      queryClient.invalidateQueries({ queryKey: ['pending'] })
      navigate('/preview')
    },
    onError: (error) => {
      const code = (error as { code?: string }).code
      if (code === 'pdf_password' || code === 'pdf_password_wrong') {
        setNeedsPassword(true)
        // Two codes, and the difference is the whole reason this reads them
        // separately. `pdf_password` says "show the box"; the box was already
        // showing when a password was rejected, so under one code a wrong
        // password re-rendered an identical page and looked like the button
        // did nothing at all. §41.
        setWrong(code === 'pdf_password_wrong' ? describe(error).detail : '')
        setPassword('')
        // Not a toast: the remedy is a field on this page, and a message that
        // fades after five seconds is the wrong shape for "type something".
        return
      }
      setNeedsPassword(false)
      setWrong('')
      toast({ kind: 'bad', ...describe(error) })
    },
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
          // Kept in a ref so the retry after a password prompt does not ask
          // the operator to pick the same file again.
          const file = input.current?.files?.[0] ?? chosen.current
          if (!file) return
          chosen.current = file
          const form = new FormData()
          form.append('statement', file)
          if (password) form.append('password', password)
          upload.mutate(form)
        }}
      >
        {needsPassword && (
          <>
            <Notice kind="warn">
              <p>
                <strong>This PDF is encrypted.</strong> Type its password and submit again —
                it is used here, in this container, by <code>pikepdf</code>, and never
                stored. <strong>Do not use an online PDF unlocker:</strong> the file carries
                your account number, customer ID, address and every counterparty.
              </p>
            </Notice>
            {/* Beside the field, not in a toast. The person is looking at the
                box they just typed into; a message that appears elsewhere and
                fades is how "nothing happens" happened. */}
            {wrong && (
              <p className="field__error" role="alert">
                <Cross title="" /> {wrong}
              </p>
            )}
            <label htmlFor="pdf-password">
              PDF password
              <PasswordField
                id="pdf-password"
                autoFocus
                value={password}
                invalid={!!wrong}
                onChange={setPassword}
              />
            </label>
            {/* Every bank sets this differently — last four digits, date of
                birth, customer ID, some combination — so passbook does not
                guess, it asks. What it can promise is where the password
                goes, and that is the sentence that matters here. */}
            <p className="muted">
              <Tick title="" /> The password never leaves this machine. It is used to open
              the file here and is not written to disk. <strong>Never use an online PDF
              unlocker</strong> — that file carries your account number, address and every
              counterparty you have paid.
            </p>
          </>
        )}

        <label htmlFor="statement">
          Statement export (.xls, .xlsx or .pdf)
          {/* A hint to the file dialog, not a guard: the server sniffs magic
              bytes, because a .pdf renamed to .xls passes every extension
              check there is. Both exist on purpose. */}
          <input
            id="statement"
            ref={input}
            type="file"
            name="statement"
            required
            accept=".xls,.xlsx,.pdf,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/pdf"
          />
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
