/* Account: change the password, re-issue backup codes, revoke devices.
 * SPEC §16.2. */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { api } from '../lib/api'
import type { Status } from '../lib/types'
import { Card, Notice, Tick } from '../components/ui'
import { Skeleton, Why, describe, useToast } from '../components/feedback'
import { count } from '../lib/money'

export function Password() {
  return (
    <div className="page page--narrow">
      <h1>Account</h1>
      <ChangePassword />
      <SecondFactorPanel />
    </div>
  )
}

function ChangePassword() {
  const queryClient = useQueryClient()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')

  const toast = useToast()
  const submit = useMutation({
    mutationFn: () => api.post('/password', { current, new: next, confirm }),
    onSuccess: () => {
      toast({ kind: 'ok', title: 'Changed', detail: 'Sign in again with the new password.' })
      queryClient.invalidateQueries({ queryKey: ['session'] })
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  return (
    <>
      <form
        className="card"
        onSubmit={(event) => {
          event.preventDefault()
          submit.mutate()
        }}
      >
        <h2>Change password</h2>
        <label htmlFor="current">
          Current password
          <input
            id="current"
            type="password"
            autoComplete="current-password"
            required
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </label>
        <label htmlFor="new">
          New password
          <input
            id="new"
            type="password"
            autoComplete="new-password"
            minLength={12}
            required
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
        </label>
        {/* Confirmation belongs here — on creation — not on the sign-in form. */}
        <label htmlFor="confirm">
          Confirm new password
          <input
            id="confirm"
            type="password"
            autoComplete="new-password"
            minLength={12}
            required
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </label>
        <button type="submit" className="primary" disabled={submit.isPending}>
          {submit.isPending ? 'Saving…' : 'Change password'}
        </button>
      </form>

      <p className="muted">At least 12 characters. You will be signed out.</p>
      <Why label="Where it is stored">
        <p>
          <code>config/web-auth.json</code>, hashed — the plaintext is stored nowhere. Your
          second factor is kept.
        </p>
      </Why>
    </>
  )
}

function SecondFactorPanel() {
  const queryClient = useQueryClient()
  const [password, setPassword] = useState('')
  const [codes, setCodes] = useState<string[] | null>(null)

  const { data, isPending } = useQuery({
    queryKey: ['status'],
    queryFn: () => api.get<Status>('/status'),
  })

  const regenerate = useMutation({
    mutationFn: () => api.post<{ backupCodes: string[] }>('/totp/backup-codes', { password }),
    onSuccess: (result) => {
      setCodes(result.backupCodes)
      setPassword('')
      queryClient.invalidateQueries({ queryKey: ['status'] })
    },
  })

  const forget = useMutation({
    mutationFn: () => api.post<{ forgotten: number }>('/devices/forget'),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['status'] }),
  })

  if (isPending || !data) return <Skeleton rows={3} />

  return (
    <Card title="Second factor">
      <p className="muted">
        Enrolled {data.auth.enrolledAt?.slice(0, 10) ?? '—'} ·{' '}
        <strong>{data.auth.backupCodesLeft}</strong>{' '}
        {data.auth.backupCodesLeft === 1 ? 'backup code' : 'backup codes'} left ·{' '}
        {count(data.auth.rememberedDevices, 'remembered device')}
      </p>

      {data.auth.backupCodesLeft === 0 && !data.auth.recoveryEmail && (
        <Notice kind="warn">
          <p>
            <strong>No backup codes and no recovery address.</strong> If you lose the phone
            now, <code>passbook web-totp --reset</code> on the host is the only way back in.
          </p>
        </Notice>
      )}

      <RecoveryEmail current={data.auth.recoveryEmail} />

      {codes ? (
        <>
          <Notice kind="warn">
            <p>
              <strong>Shown once.</strong> The previous set no longer works.
            </p>
          </Notice>
          <ul className="codes">
            {codes.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
          <button type="button" onClick={() => setCodes(null)}>
            <Tick title="" /> Done
          </button>
        </>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault()
            regenerate.mutate()
          }}
        >
          <label htmlFor="regen">
            Password, to issue eight new backup codes
            <input
              id="regen"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          <div className="actions">
            <button type="submit" disabled={regenerate.isPending || !password}>
              {regenerate.isPending ? 'Issuing…' : 'Re-issue backup codes'}
            </button>
            <button
              type="button"
              onClick={() => forget.mutate()}
              disabled={forget.isPending || data.auth.rememberedDevices === 0}
            >
              Revoke remembered devices
            </button>
          </div>
        </form>
      )}

      <Why label="Lost the phone?">
        <p>
          A backup code signs you in once. Out of those,{' '}
          <code>make web-totp RESET=yes</code> on the host clears the secret so the next
          sign-in enrols a new one. <code>make web-password</code> resets the password without
          touching the second factor.
        </p>
      </Why>
    </Card>
  )
}

/**
 * The recovery address. SPEC §29.
 *
 * A second way back, standing **beside** the backup codes rather than replacing
 * them — and never in front of the authenticator. An emailed code is only as
 * strong as the mailbox it lands in, and that mailbox can usually reset
 * everything else too; as a *recovery* path that is fine, as a primary factor
 * it would collapse two factors into one.
 *
 * Only ever shown masked. The server never returns the address itself.
 */
function RecoveryEmail({ current }: { current: string | null }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [value, setValue] = useState('')
  const [editing, setEditing] = useState(false)

  const save = useMutation({
    mutationFn: (email: string) =>
      api.put<{ recoveryEmail: string | null }>('/recovery-email', { email }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['status'] })
      queryClient.invalidateQueries({ queryKey: ['session'] })
      setEditing(false)
      setValue('')
      toast({
        kind: 'ok',
        title: result.recoveryEmail ? 'Recovery address set' : 'Recovery address cleared',
        detail: result.recoveryEmail
          ? `A sign-in code can be emailed to ${result.recoveryEmail}.`
          : 'Backup codes are now the only way back.',
      })
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  if (!editing)
    return (
      <p className="muted">
        Recovery email:{' '}
        {current ? <strong>{current}</strong> : <em>not set</em>} ·{' '}
        <button type="button" className="linklike" onClick={() => setEditing(true)}>
          {current ? 'change' : 'add'}
        </button>
        {current && (
          <>
            {' '}·{' '}
            <button type="button" className="linklike" onClick={() => save.mutate('')}>
              remove
            </button>
          </>
        )}
      </p>
    )

  return (
    <div className="field-row">
      <input
        autoFocus
        type="email"
        value={value}
        placeholder="you@example.com"
        aria-label="Recovery email"
        onChange={(event) => setValue(event.target.value)}
      />
      <button
        type="button"
        className="primary"
        disabled={!value.trim() || save.isPending}
        data-working={save.isPending}
        onClick={() => save.mutate(value.trim())}
      >
        {save.isPending ? 'Saving…' : 'Save'}
      </button>
      <button type="button" onClick={() => setEditing(false)}>
        Cancel
      </button>
    </div>
  )
}
