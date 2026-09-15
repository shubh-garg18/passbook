/* The accounts this install knows about — and the way back out. SPEC §38.
 *
 * There was an "Add account" nav item and no "accounts" anywhere: adding was a
 * task with a front door and everything after it — what is registered, where it
 * posts, getting rid of one — had none. A registry you can only append to is
 * not a registry, it is a log.
 *
 * So this is the place, and adding is one of the things you do here rather than
 * the only one.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../lib/api'
import type { Accounts as Registry, AccountSummary, Removal } from '../lib/types'
import { Notice, Spinner } from '../components/ui'
import { Why, describe, useToast } from '../components/feedback'
import { count } from '../lib/money'

export function AccountsPage() {
  const { data, isPending, error } = useQuery({
    queryKey: ['accounts'],
    queryFn: () => api.get<Registry>('/accounts'),
  })

  if (isPending) return <Spinner what="your accounts" />
  if (error)
    return (
      <div className="page">
        <Notice kind="bad">
          <p>{describe(error).detail}</p>
        </Notice>
      </div>
    )

  const registry = data?.accounts ?? []

  return (
    <div className="page">
      <h1>Accounts</h1>
      <p className="lede">
        {registry.length === 0
          ? 'Nothing registered yet. The first statement you upload registers itself.'
          : `${count(registry.length, 'account')}, each posting to its own ledger.`}
      </p>

      <ul className="roster">
        {registry.map((account) => (
          <AccountRow key={account.slug} account={account} last={registry.length === 1} />
        ))}
      </ul>

      <div className="actions">
        <Link className="button button--primary" to="/accounts/add">
          Add an account
        </Link>
        <Link className="button" to="/banks/add">
          Add a bank
        </Link>
      </div>

      {/* §115.1. A disclosure, not a notice. It is the difference between the
          two buttons above it, which is worth having and is not news after the
          first read — a `Notice` says "attend to this" every visit and this
          never needs attending to. */}
      <Why label="Account or bank?">
        <p>
          An <strong>account</strong> is one bank account, read from one statement and
          posted to one ledger account. A <strong>bank</strong> is the description of
          where the columns are in that bank's file — add it once and every account at that
          bank can be read. Neither ever sends a statement anywhere.
        </p>
      </Why>
    </div>
  )
}

/** `canara` is a filename, not a bank. Nothing else in the UI shows a slug. */
function bankName(slug: string): string {
  return slug.replace(/(^|[-_])(\w)/g, (_, sep: string, letter: string) =>
    (sep ? ' ' : '') + letter.toUpperCase(),
  )
}

type Panel = 'rename' | 'remove' | null

function AccountRow({ account, last }: { account: AccountSummary; last: boolean }) {
  // One at a time. Two open panels under one row is two questions at once, and
  // one of them is destructive.
  const [panel, setPanel] = useState<Panel>(null)
  const toggle = (which: Panel) => setPanel(panel === which ? null : which)

  return (
    <li className="sheet">
      <div className="sheet__head">
        <div>
          <h2>{account.label}</h2>
          {/* The heading already says the name, and the name is
              `Canara ****1111` until somebody changes it — so repeating the
              bank and the last four underneath makes the row look like it holds
              two facts when it holds one. They come back the moment the heading
              stops saying them. */}
          <p className="muted">
            {account.renamed && `${bankName(account.bank)} · ${account.account} · `}
            posts to <code>{account.assetAccount}</code>
          </p>
        </div>
        <div className="sheet__tools">
          <button type="button" className="quiet" onClick={() => toggle('rename')}>
            {panel === 'rename' ? 'Cancel' : 'Rename'}
          </button>
          <button type="button" className="quiet quiet--danger" onClick={() => toggle('remove')}>
            {panel === 'remove' ? 'Keep it' : 'Remove'}
          </button>
        </div>
      </div>
      {panel === 'rename' && <Rename account={account} onDone={() => setPanel(null)} />}
      {panel === 'remove' && (
        <Remove account={account} last={last} onDone={() => setPanel(null)} />
      )}
    </li>
  )
}

/**
 * Rename an account. SPEC §40.
 *
 * Safe in a way a payee rename is not, and worth saying because the two look
 * identical from here. A payee's display name is what categorisation rules
 * match on, so renaming one moves the row out from under its own rule (§23.4).
 * An account's name is matched by nothing: it is not stored, the ledger never sees
 * it, and no rule mentions it. The slug underneath — the thing `external_id` is
 * built from — does not move.
 *
 * Clearing the field is a reset rather than an error. It puts the account back
 * to `Canara ****1111`, a name that needs no maintenance and can never name two
 * accounts at once.
 */
function Rename({ account, onDone }: { account: AccountSummary; onDone: () => void }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  // Empty when the current name is the default: pre-filling `Canara ****1111`
  // would invite editing a string nobody chose, and the placeholder says what
  // clearing it gets you.
  const [name, setName] = useState(account.renamed ? account.label : '')

  const rename = useMutation({
    mutationFn: () => api.patch<{ account: AccountSummary }>(`/accounts/${account.slug}`, {
      label: name.trim(),
    }),
    onSuccess: (result) => {
      queryClient.invalidateQueries()
      onDone()
      toast({
        kind: 'ok',
        title: 'Renamed',
        detail: `Now shown as ${result.account.label} everywhere.`,
      })
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  return (
    <div className="sheet__body">
      <div className="field-row">
        <input
          autoFocus
          value={name}
          maxLength={40}
          placeholder={`${account.bankName} ${account.account}`}
          aria-label={`A name for ${account.label}`}
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => event.key === 'Enter' && !rename.isPending && rename.mutate()}
        />
        <button
          type="button"
          className="primary"
          disabled={rename.isPending}
          data-working={rename.isPending}
          onClick={() => rename.mutate()}
        >
          {rename.isPending ? 'Saving…' : name.trim() ? 'Rename' : 'Use the default'}
        </button>
      </div>
      <Why label="What a name changes">
        <p>
          A display name only — it appears in the account tabs, on the Ledger and on
          Payees. Nothing is pushed, no rule matches it, and the transactions already
          recorded are untouched.
        </p>
      </Why>
    </div>
  )
}

/**
 * Removing one, with the consequence shown before the button rather than after.
 *
 * This deletes nothing — not a transaction, not an archived statement — and
 * saying so is most of the job, because "Remove" beside a bank account reads
 * like it might. What it actually does is stop passbook managing those rows:
 * every comparison iterates the registry, so they fall out of re-apply, out of
 * verify, and out of every figure on the Ledger.
 *
 * The count is fetched rather than guessed because "this leaves 93 rows
 * unmanaged" is a sentence you can act on, and "are you sure?" is not.
 */
function Remove({
  account,
  last,
  onDone,
}: {
  account: AccountSummary
  last: boolean
  onDone: () => void
}) {
  const toast = useToast()
  const queryClient = useQueryClient()

  const { data, isPending } = useQuery({
    queryKey: ['accounts', account.slug, 'removal'],
    queryFn: () => api.get<Removal>(`/accounts/${account.slug}/removal`),
    retry: false,
  })

  const remove = useMutation({
    mutationFn: () => api.del(`/accounts/${account.slug}`),
    onSuccess: () => {
      // Everything scoped by account is now scoped to a different set. The
      // whole cache, not a list of keys somebody has to remember to extend.
      queryClient.invalidateQueries()
      onDone()
      toast({
        kind: 'ok',
        title: `${account.label} removed`,
        detail: 'Nothing was deleted from the ledger. Re-add it under the same name to reconnect.',
      })
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  if (isPending) return <Spinner what="what this would affect" />

  return (
    <div className="sheet__body">
      <Notice kind="warn">
        <p>
          <strong>This removes the account from passbook. It deletes nothing.</strong>
        </p>
        <ul>
          <li>
            {data?.ledgerRows === null
              ? data?.countReason
              : `${count(data?.ledgerRows ?? 0, 'transaction')} stay in the ledger, and passbook stops
                 managing them — no re-apply, no verify, no figure on the Ledger.`}
          </li>
          <li>
            {count(data?.archiveFiles ?? 0, 'archived statement')} stay in{' '}
            <code>archive/</code>.
          </li>
          <li>
            Adding it back under the same name reconnects it exactly. Under a{' '}
            <strong>different</strong> name it would be a new account, and pushing those
            statements again would duplicate every row rather than skip them.
          </li>
        </ul>
      </Notice>
      {last && (
        <p className="muted">
          This is the only registered account. Removing it puts the install back to where it
          was before you registered anything — the next statement you upload registers itself.
        </p>
      )}
      <div className="actions">
        <button
          type="button"
          className="danger"
          disabled={remove.isPending}
          data-working={remove.isPending}
          onClick={() => remove.mutate()}
        >
          {remove.isPending ? 'Removing…' : `Remove ${account.label}`}
        </button>
        <button type="button" onClick={onDone}>
          Keep it
        </button>
      </div>
    </div>
  )
}
