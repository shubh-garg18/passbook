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
  const [code, setCode] = useState('')
  const [backupCode, setBackupCode] = useState('')
  const [useBackup, setUseBackup] = useState(false)
  const [remember, setRemember] = useState(false)

  const toast = useToast()
  const submit = useMutation({
    mutationFn: () =>
      api.post<{ stage: Stage; backupCodesLeft: number }>('/session/totp', {
        code: useBackup ? '' : code,
        backupCode: useBackup ? backupCode : '',
        remember,
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['session'] }),
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  return (
    <div className="page page--narrow">
      <h1>Second factor</h1>
      <p className="lede">
        {useBackup
          ? 'One of the eight codes issued at enrolment. Each works once.'
          : 'The six-digit code from your authenticator.'}
      </p>

      <form
        className="card"
        onSubmit={(event) => {
          event.preventDefault()
          submit.mutate()
        }}
      >
        {useBackup ? (
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
          <button type="button" onClick={() => setUseBackup(!useBackup)}>
            {useBackup ? 'Use authenticator' : 'Use a backup code'}
          </button>
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
