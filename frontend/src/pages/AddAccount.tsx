/* Register another account, from the browser. SPEC §26.
 *
 * It was CLI-only (`passbook accounts add <statement>`), which is fine until
 * you are the person who installed this from GitHub and has never opened the
 * repo.
 *
 * **The account number is read from the statement, never typed.** It is in the
 * file (§6.3), and a number wrong by one digit would give the account its own
 * `external_id` namespace and its own ledger — silently, forever, because
 * nothing downstream would ever disagree with itself. So step one is an upload,
 * not a form field.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '../lib/api'
import { count } from '../lib/money'
import type { Accounts, Parsed } from '../lib/types'
import { Cross, Notice, PasswordField } from '../components/ui'
import { Progress, describe, useToast } from '../components/feedback'

type Candidates = {
  assetAccounts: { name: string; taken: boolean }[]
  banks: string[]
}

export function AddAccount() {
  const toast = useToast()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const file = useRef<HTMLInputElement>(null)

  const [parsed, setParsed] = useState<Parsed | null>(null)
  const [asset, setAsset] = useState('')
  const [bank, setBank] = useState('canara')

  const { data: registry } = useQuery({
    queryKey: ['accounts'],
    queryFn: () => api.get<Accounts>('/accounts'),
  })
  const { data: candidates, error: candidatesError } = useQuery({
    queryKey: ['accounts', 'candidates'],
    queryFn: () => api.get<Candidates>('/accounts/candidates'),
    retry: false,
  })

  const [needsPassword, setNeedsPassword] = useState(false)
  const [password, setPassword] = useState('')
  /** The message from a REJECTED password. Empty means none has been tried. */
  const [wrong, setWrong] = useState('')
  const chosen = useRef<File | null>(null)

  const send = (file: File, secret: string) => {
    chosen.current = file
    const form = new FormData()
    form.append('statement', file)
    if (secret) form.append('password', secret)
    upload.mutate(form)
  }

  const upload = useMutation({
    // Not `/statement`: that refuses an unregistered account and deletes the
    // staged file (§21.7), which is exactly right on the normal path and
    // exactly wrong on the page for registering one. `/accounts/inspect`
    // stages and validates but cannot push.
    mutationFn: (form: FormData) => api.upload<Parsed>('/accounts/inspect', form),
    onSuccess: (result) => {
      setParsed(result)
      setNeedsPassword(false)
      setPassword('')
      setWrong('')
    },
    onError: (error) => {
      // Most Indian banks hand out password-protected PDFs. Decryption is
      // pikepdf, here, on this machine — never an online unlocker (§30).
      const code = (error as { code?: string }).code
      if (code === 'pdf_password' || code === 'pdf_password_wrong') {
        setNeedsPassword(true)
        // `pdf_password` means "show the box". Under one code a REJECTED
        // password also said that — and the box was already showing, so
        // nothing on the page changed and the Unlock button looked dead. §41.
        setWrong(code === 'pdf_password_wrong' ? describe(error).detail : '')
        setPassword('')
        return
      }
      toast({ kind: 'bad', ...describe(error) })
    },
  })

  const register = useMutation({
    mutationFn: () =>
      api.post<{
        created: boolean
        assetCreated: boolean
        account: { label: string; account: string }
      }>('/accounts', { assetAccount: asset.trim(), bank }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      toast({
        kind: 'ok',
        title: result.created ? 'Account added' : 'Already registered',
        detail:
          `${result.account.label} · ${result.account.account}` +
          (result.assetCreated ? ' — and its ledger account was created for you.' : ''),
      })
      // Straight to the preview: the statement is still staged, and pushing it
      // is obviously the next thing.
      navigate('/preview')
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  const free = candidates?.assetAccounts.filter((a) => !a.taken) ?? []

  /** What it will be called if the operator says nothing. The server computes
   *  the same thing, so a blank field and this text agree. §56.1. */
  const suggested = parsed
    ? `${bank.charAt(0).toUpperCase()}${bank.slice(1)} ${parsed.meta.account}`
    : ''
  /** Typing a name the ledger already has attaches to it instead of making one. */
  const matchesExisting = (candidates?.assetAccounts ?? []).some(
    (a) => a.name === asset.trim(),
  )

  return (
    <div className="page">
      <h1>Add an account</h1>
      <p className="lede">
        Upload one of its statements. The account number is read from the file rather than
        typed — a digit wrong would give it its own ledger, and nothing would ever say so.
      </p>

      {registry && registry.accounts.length > 0 && (
        <p className="muted">
          Already registered:{' '}
          {registry.accounts.map((a) => `${a.label} (${a.account})`).join(', ')}
        </p>
      )}

      <ol className="steps">
        <li>
          <h2 className="section">1 — the statement</h2>
          {parsed ? (
            <Notice kind="ok">
              <p>
                <strong>{parsed.meta.account}</strong> · {parsed.transactions.length}{' '}
                transactions, {parsed.meta.periodFrom} to {parsed.meta.periodTo}.{' '}
                <button type="button" className="linklike" onClick={() => setParsed(null)}>
                  Use a different file
                </button>
              </p>
            </Notice>
          ) : (
            <>
              <input
                ref={file}
                id="statement"
                type="file"
                accept=".xls,.xlsx,.pdf,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/pdf"
                onChange={(event) => {
                  const picked = event.target.files?.[0]
                  if (picked) send(picked, password)
                }}
              />
              {needsPassword && (
                <>
                  <Notice kind="warn">
                    <p>
                      <strong>This PDF is encrypted.</strong> Every bank sets that password
                      differently, so passbook asks rather than guesses. It never leaves
                      this machine — the file is opened here and the password is not
                      written to disk. <strong>Never use an online PDF unlocker.</strong>
                    </p>
                  </Notice>
                  {/* Beside the field, not in a toast: the person is looking at
                      the box they just typed into. */}
                  {wrong && (
                    <p className="field__error" role="alert">
                      <Cross title="" /> {wrong}
                    </p>
                  )}
                  <div className="field-row">
                    <PasswordField
                      autoFocus
                      value={password}
                      invalid={!!wrong}
                      onChange={setPassword}
                      onEnter={() => chosen.current && send(chosen.current, password)}
                    />
                    <button
                      type="button"
                      className="primary"
                      disabled={!password || upload.isPending}
                      data-working={upload.isPending}
                      onClick={() => chosen.current && send(chosen.current, password)}
                    >
                      {upload.isPending ? 'Reading…' : 'Unlock'}
                    </button>
                  </div>
                </>
              )}
              {upload.isPending && <Progress label="Reading the statement" />}
            </>
          )}
        </li>

        <li>
          <h2 className="section">2 — where it posts</h2>
          {candidatesError ? (
            <Notice kind="warn">
              <p>
                The ledger could not be asked which accounts exist —{' '}
                {describe(candidatesError).detail}
              </p>
            </Notice>
          ) : (
            <>
              {/* §93. "why the ledger name there" — because this field names the
                  account rows are POSTED into, and that store is the ledger.
                  The operator should not have to know that: it is an
                  implementation detail of where the ledger lives, and every
                  other page stopped naming it long ago. The field is "Ledger
                  account name" now. The value it holds is unchanged — it is
                  still `asset_account` in `accounts.yaml` and still the name
                  The ledger matches on — and the code comments still say so,
                  because that is what a reader of the code needs to know.

                  SPEC §56.1. This was two steps: create the ledger account,
                  then choose it. They are one intention, and splitting them
                  sent the operator off to do half of it by hand — "I dont want
                  to create manually in the ledger again as I upload the statement
                  in UI". Registering now creates it if it does not exist, so
                  this is a name with a sensible default, not a decision. */}
              <div className="fields">
                <label className="field">
                  <span>Ledger account name</span>
                  <input
                    list="asset-accounts"
                    value={asset}
                    placeholder={suggested}
                    onChange={(event) => setAsset(event.target.value)}
                  />
                  <datalist id="asset-accounts">
                    {free.map((a) => (
                      <option key={a.name} value={a.name} />
                    ))}
                  </datalist>
                  <span className="muted">
                    {matchesExisting
                      ? 'Posts into the existing ledger account of that name.'
                      : 'Created in INR when you register — you do not have to make it first.'}
                  </span>
                </label>
                <label className="field">
                  <span>Bank</span>
                  <select value={bank} onChange={(event) => setBank(event.target.value)}>
                    {(candidates?.banks ?? ['canara']).map((b) => (
                      <option key={b} value={b}>
                        {b}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              {free.length > 0 && !matchesExisting && (
                <p className="muted">
                  {count(free.length, 'unclaimed ledger account')} already{' '}
                  {free.length === 1 ? 'exists' : 'exist'} — start typing to attach to one
                  instead.
                </p>
              )}
            </>
          )}
        </li>
      </ol>

      <div className="actions">
        <button
          type="button"
          className="primary"
          // No longer gated on an asset account: blank means "use the
          // suggested name", and registering creates it. §56.1.
          disabled={!parsed || register.isPending}
          data-working={register.isPending}
          onClick={() => register.mutate()}
        >
          {register.isPending ? 'Registering…' : 'Register this account'}
        </button>
        <Link className="button" to="/accounts">
          Cancel
        </Link>
      </div>

      <Notice>
        <p>
          passbook reads <strong>{(candidates?.banks ?? ['canara']).join(', ')}</strong>. If
          yours is not one of them,{' '}
          <a href="https://github.com/shubh-garg18/passbook/issues">say which bank it is</a>{' '}
          and it can be added — your statement stays on this machine either way.
        </p>
      </Notice>
    </div>
  )
}
