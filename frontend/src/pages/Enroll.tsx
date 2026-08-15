/* Mandatory TOTP enrolment. SPEC §16.2.
 *
 * Reached automatically after the password step when no secret exists, so it
 * cannot be skipped by simply not visiting the page.
 *
 * The backup codes are shown exactly once and stored only as salted digests.
 * They are mandatory rather than optional because the failure mode TOTP
 * *creates* is a lost phone — and a ledger you cannot open is a worse outcome
 * than the one the second factor prevents.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { api } from '../lib/api'
import type { EnrollStart } from '../lib/types'
import { Notice, Tick } from '../components/ui'
import { Why, describe, useToast } from '../components/feedback'

export function Enroll() {
  const queryClient = useQueryClient()
  const [enrolment, setEnrolment] = useState<EnrollStart | null>(null)
  const [code, setCode] = useState('')
  const [codes, setCodes] = useState<string[] | null>(null)
  const [saved, setSaved] = useState(false)

  const toast = useToast()
  const start = useMutation({
    mutationFn: () => api.post<EnrollStart>('/totp/enroll/start'),
    onSuccess: setEnrolment,
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  const confirm = useMutation({
    mutationFn: () => api.post<{ backupCodes: string[] }>('/totp/enroll/confirm', { code }),
    onSuccess: (data) => setCodes(data.backupCodes),
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  // One request on arrival. StrictMode double-invokes effects in dev; the
  // mutation guard keeps that from minting two candidate secrets.
  useEffect(() => {
    if (!enrolment && start.isIdle) start.mutate()
  }, [enrolment, start])

  if (codes) {
    return (
      <div className="page page--narrow">
        <h1>Save these</h1>
        <Notice kind="warn">
          <p>
            <strong>Shown once.</strong> Each signs you in a single time if your authenticator
            is unavailable.
          </p>
        </Notice>

        <ul className="codes">
          {codes.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>

        <label htmlFor="saved" style={{ display: 'flex', gap: '.5rem', alignItems: 'center' }}>
          <input
            id="saved"
            type="checkbox"
            style={{ width: 'auto', margin: 0 }}
            checked={saved}
            onChange={(e) => setSaved(e.target.checked)}
          />
          I have written these down somewhere that is not this laptop
        </label>

        <div className="actions">
          <button
            type="button"
            className="primary"
            disabled={!saved}
            onClick={() => queryClient.invalidateQueries({ queryKey: ['session'] })}
          >
            <Tick title="" /> Open the ledger
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="page page--narrow">
      <h1>Set up your authenticator</h1>
      <p className="lede">Scan this, then type the code it shows.</p>

      {enrolment && (
        <>
          <div className="card">
            {/* Server-rendered SVG from segno: no QR library in the bundle, and
                the secret never has to be redrawn client-side. */}
            <div className="qr" dangerouslySetInnerHTML={{ __html: enrolment.qr }} />
            <Why label="Cannot scan?">
              <p>
                Enter this key by hand: <code>{enrolment.secretPretty}</code>
              </p>
            </Why>
          </div>

          <form
            className="card"
            onSubmit={(event) => {
              event.preventDefault()
              confirm.mutate()
            }}
          >
            <label htmlFor="code">
              Code from the app
              <input
                id="code"
                inputMode="numeric"
                pattern="[0-9]*"
                maxLength={6}
                autoComplete="one-time-code"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
              />
            </label>
            <button type="submit" className="primary" disabled={confirm.isPending || code.length < 6}>
              {confirm.isPending ? 'Checking…' : 'Confirm'}
            </button>
          </form>
        </>
      )}
    </div>
  )
}
