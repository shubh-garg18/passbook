/* Sign-in: password, then a second factor. SPEC §16.2.
 *
 * Two steps because that is what a second factor is. A remembered device skips
 * step two — never step one; a remembered device is not a remembered session.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { api } from '../lib/api'
import type { Stage } from '../lib/types'
import { Why, describe, useToast } from '../components/feedback'

export function SignIn({ stage }: { stage: Stage }) {
  return stage === 'totp' ? <SecondFactor /> : <PasswordStep />
}

function PasswordStep() {
  const queryClient = useQueryClient()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [reveal, setReveal] = useState(false)

  const toast = useToast()
  const submit = useMutation({
    mutationFn: () => api.post<{ stage: Stage }>('/session', { username, password }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['session'] }),
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  return (
    <div className="page page--narrow">
      <h1>Sign in</h1>
      <form
        className="card"
        onSubmit={(event) => {
          event.preventDefault()
          submit.mutate()
        }}
      >
        <label htmlFor="username">
          Username
          <input
            id="username"
            name="username"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>

        {/* No confirm field. This is sign-in, not account creation. */}
        <label htmlFor="password">
          Password
          <span className="field-row">
            <input
              id="password"
              name="password"
              type={reveal ? 'text' : 'password'}
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <button type="button" aria-pressed={reveal} onClick={() => setReveal(!reveal)}>
              {reveal ? 'Hide' : 'Show'}
            </button>
          </span>
        </label>

        <button type="submit" className="primary" disabled={submit.isPending}>
          {submit.isPending ? 'Checking…' : 'Continue'}
        </button>
      </form>

      <Why label="Locked out?">
        <p>
          There is no password reset — no email is configured, deliberately. On the host,{' '}
          <code>make web-password</code> sets a new one and preserves your second factor.
        </p>
      </Why>
    </div>
  )
}

function SecondFactor() {
  const queryClient = useQueryClient()
  // Three ways in, one at a time. `mode` rather than a pile of booleans: they
  // are alternatives, and two of them being true at once is a state the server
  // would have to arbitrate.
  const [mode, setMode] = useState<'totp' | 'backup' | 'email'>('totp')
  const [code, setCode] = useState('')
  const [backupCode, setBackupCode] = useState('')
  const [emailCode, setEmailCode] = useState('')
  const [remember, setRemember] = useState(false)
  const [sentTo, setSentTo] = useState<{ to: string; minutes: number } | null>(null)

  const toast = useToast()
  const submit = useMutation({
    mutationFn: () =>
      api.post<{ stage: Stage; backupCodesLeft: number }>('/session/totp', {
        code: mode === 'totp' ? code : '',
        backupCode: mode === 'backup' ? backupCode : '',
        recoveryCode: mode === 'email' ? emailCode : '',
        remember,
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['session'] }),
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  const sendEmail = useMutation({
    mutationFn: () =>
      api.post<{ to: string; expiresInMinutes: number }>('/session/recover'),
    onSuccess: (result) => {
      setMode('email')
      setSentTo({ to: result.to, minutes: result.expiresInMinutes })
      toast({
        kind: 'ok',
        title: 'Code sent',
        detail: `Check ${result.to}. It works once and expires in ${result.expiresInMinutes} minutes.`,
      })
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  return (
    <div className="page page--narrow">
      <h1>Second factor</h1>
      <p className="lede">
        {mode === 'backup'
          ? 'One of the eight codes issued at enrolment. Each works once.'
          : mode === 'email'
            ? sentTo
              ? `Sent to ${sentTo.to}. It works once and expires in ${sentTo.minutes} minutes.`
              : 'A one-time code, emailed to your recovery address.'
            : 'The six-digit code from your authenticator.'}
      </p>

      <form
        className="card"
        onSubmit={(event) => {
          event.preventDefault()
          submit.mutate()
        }}
      >
        {mode === 'email' ? (
          <label htmlFor="emailed">
            Emailed code
            <input
              id="emailed"
              name="recoveryCode"
              autoFocus
              autoComplete="one-time-code"
              spellCheck={false}
              autoCapitalize="characters"
              value={emailCode}
              onChange={(e) => setEmailCode(e.target.value.toUpperCase())}
            />
          </label>
        ) : mode === 'backup' ? (
          <label htmlFor="backup">
            Backup code
            <input
              id="backup"
              name="backupCode"
              autoFocus
              autoComplete="one-time-code"
              spellCheck={false}
              autoCapitalize="characters"
              value={backupCode}
              onChange={(e) => setBackupCode(e.target.value.toUpperCase())}
            />
          </label>
        ) : (
          <label htmlFor="code">
            Authenticator code
            <input
              id="code"
              name="code"
              autoFocus
              inputMode="numeric"
              pattern="[0-9]*"
              maxLength={6}
              autoComplete="one-time-code"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
            />
          </label>
        )}

        <label htmlFor="remember" style={{ display: 'flex', gap: '.5rem', alignItems: 'center' }}>
          <input
            id="remember"
            type="checkbox"
            style={{ width: 'auto', margin: 0 }}
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
          />
          Remember this device for 30 days
        </label>

        <div className="actions">
          <button type="submit" className="primary" disabled={submit.isPending}>
            {submit.isPending ? 'Checking…' : 'Sign in'}
          </button>
          {mode === 'totp' ? (
            <>
              <button type="button" onClick={() => setMode('backup')}>
                Use a backup code
              </button>
              <button
                type="button"
                onClick={() => sendEmail.mutate()}
                disabled={sendEmail.isPending}
                data-working={sendEmail.isPending}
              >
                {sendEmail.isPending ? 'Sending…' : 'Email me a code'}
              </button>
            </>
          ) : (
            <button type="button" onClick={() => setMode('totp')}>
              Use authenticator
            </button>
          )}
        </div>
      </form>

      <Why label="Lost the phone?">
        <p>
          A backup code signs you in once. Out of those,{' '}
          <code>make web-totp RESET=yes</code> on the host clears the second factor so the
          next sign-in enrols a new one.
        </p>
      </Why>
    </div>
  )
}
