/* Teach passbook a new bank, from the browser. SPEC §34.
 *
 * A bank profile is a description of a layout, not a program — which is why
 * adding one needs no Python. Writing it still needed a terminal, and that was
 * the last step keeping a non-developer out of their own second bank.
 *
 * The page is the CLI's `passbook inspect` with a form attached:
 *
 *   1. upload the statement — read ONLY, never staged, never pushed, never
 *      registered. Safe on a file this cannot parse at all, which is exactly
 *      the case it exists for.
 *   2. read the grid. Every cell is shown as `repr()`, so `' '` is visibly a
 *      space and not `''` — the likeliest silent bug in the whole project.
 *   3. name the six columns.
 *   4. save, and the profile is loaded back to prove it parses.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../lib/api'
import { count } from '../lib/money'
import { Cross, Notice, PasswordField, Tick } from '../components/ui'
import { Progress, Why, describe, useToast } from '../components/feedback'

type Inspection = {
  container: string
  rows: number
  grid: string[][]
  headerRow: number | null
  matched: Record<string, number>
  missing: string[]
  required: string[]
  banks: string[]
  /** What the proposed labels resolve to in this file, masked. §45. */
  metaFound: Record<string, string>
}

/** The result of parsing with a profile that has not been saved. §49. */
type Attempt = {
  container: string
  order: string[]
  headerLine?: number
  columns?: Record<string, [number, number]>
  edges?: Record<string, number>
  banded?: string[][]
  /** Grid index of the first banded row shown, so the numbers are the real
   *  ones the error refers to. §49.4. */
  bandedFrom?: number
  /** The row the error named, highlighted below. */
  failedRow?: number
  /** The failing row as shapes — one line, safe to paste anywhere. §52.2. */
  failedRowShapes?: string
  /** Formats that read EVERY date in the file. More than one means it is
   *  genuinely ambiguous — every day in it is 12 or less. §49.1. */
  dateCandidates?: string[]
  dateSample?: string
  /** The page as geometry and token *shapes*, carrying no content. §50. */
  shapes?: string[]
  rows?: number
  account?: string
  period?: [string, string]
  opening?: string
  closing?: string
  ok?: boolean
  error?: string
}

/** The identity fields a profile can point at, and what they are for. */
const META = [
  {
    field: 'account_number',
    label: 'Account number',
    why: 'Required. Every row is filed under it, so it is read from the statement and never typed.',
  },
  { field: 'account_name', label: 'Account holder', why: 'Optional.' },
  { field: 'ifsc', label: 'IFSC', why: 'Optional.' },
] as const

/** The order a statement is read in, which is not alphabetical.
 *
 * `required` arrives sorted, so the questions came out Balance, Credit, Date,
 * Debit, Narration — five fields in an order that matches no bank's paper and
 * makes the operator hunt back and forth across their own file. This is the
 * order the columns actually sit in.
 */
const READING_ORDER = ['date', 'txn_id', 'narration', 'debit', 'credit', 'balance']

const WHAT = {
  date: 'the transaction date',
  txn_id: 'a per-row reference, stable across re-downloads',
  debit: 'money out',
  credit: 'money in',
  balance: 'the running balance after the row',
  narration: 'the description the payee is read from',
} as const

export function AddBank() {
  const toast = useToast()
  const file = useRef<HTMLInputElement>(null)
  const chosen = useRef<File | null>(null)

  const [seen, setSeen] = useState<Inspection | null>(null)
  const [password, setPassword] = useState('')
  const [needsPassword, setNeedsPassword] = useState(false)
  /** The message from a REJECTED password. Empty means none has been tried. */
  const [wrong, setWrong] = useState('')
  const [bank, setBank] = useState('')
  const [columns, setColumns] = useState<Record<string, string>>({})
  /** This bank prints no per-row reference. §44. */
  const [derive, setDerive] = useState(false)
  /**
   * `{ your label: passbook field }` — where the account number is printed.
   * §45. Without this the statement parses perfectly and is then refused at
   * the last step, on another page, for a label nobody could name from here.
   */
  const [metaLabels, setMetaLabels] = useState<Record<string, string>>({})
  /** What parsing with the current mapping produced. §49. */
  const [attempt, setAttempt] = useState<Attempt | null>(null)
  /**
   * Which row holds the column names, when passbook could not tell. §44.
   *
   * For a bank it already knows something about, the header is found by
   * matching known words and this stays null. For a **genuinely new** bank —
   * the case this whole page exists for — nothing matches, `headerRow` comes
   * back null, and the dropdowns had nothing to offer: the operator could see
   * their grid and could not act on it. Now they point at the row.
   */
  const [pickedHeader, setPickedHeader] = useState<number | null>(null)

  const send = (picked: File, secret: string, labels = metaLabels) => {
    chosen.current = picked
    const form = new FormData()
    form.append('statement', picked)
    if (secret) form.append('password', secret)
    // Sent so the answer comes back with the question. §45.
    if (Object.keys(labels).length) form.append('metadata', JSON.stringify(labels))
    inspect.mutate(form)
  }

  const inspect = useMutation({
    mutationFn: (form: FormData) => api.upload<Inspection>('/banks/inspect', form),
    onSuccess: (result) => {
      setSeen(result)
      setNeedsPassword(false)
      // Seed the form with whatever already matched, so only the gaps are work.
      const header = result.headerRow !== null ? result.grid[result.headerRow] ?? [] : []
      const seeded: Record<string, string> = {}
      for (const [field, column] of Object.entries(result.matched)) {
        const cell = header[column]
        if (cell) seeded[field] = cell.replace(/^'|'$/g, '')
      }
      setColumns(seeded)
    },
    onError: (error) => {
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

  /**
   * Parse it here, before saving anything. SPEC §49.
   *
   * Without this the loop is: save, go to another page, upload again, read a
   * one-line rejection, come back, guess. `row 10: transaction has no balance`
   * is true, unactionable, and says nothing about which column the balance
   * actually landed in — which is the only thing worth knowing.
   */
  const attemptParse = useMutation({
    mutationFn: () => {
      const form = new FormData()
      form.append('statement', chosen.current as File)
      if (password) form.append('password', password)
      form.append('columns', JSON.stringify(
        Object.fromEntries(
          Object.entries(columns)
            .filter(([, header]) => header.trim())
            .map(([field, header]) => [header.trim(), field]),
        ),
      ))
      form.append('metadata', JSON.stringify(metaLabels))
      form.append('deriveTxnId', String(derive))
      return api.upload<Attempt>('/banks/try', form)
    },
    onSuccess: setAttempt,
    onError: (error) => {
      setAttempt(null)
      toast({ kind: 'bad', ...describe(error) })
    },
  })

  const save = useMutation({
    // **Inverted on the way out, and this was the bug.** The form is keyed by
    // passbook's field (`date`, `debit`, …) because that is what the operator
    // is answering one question at a time; the profile is keyed by the header
    // text in THEIR file, because that is what a parser has to look up. Posting
    // the form's own shape sent `{date: "Date"}` where the server reads the
    // values as field names — so it answered
    //
    //     unknown field(s): ['Balance', 'Chq', 'Date', 'Deposit', ...]
    //
    // listing the operator's own column headings back at them as if they had
    // invented them. No profile could ever be saved from this page. SPEC §41.
    mutationFn: () =>
      api.post<{ bank: string; path: string }>('/banks', {
        bank,
        deriveTxnId: derive,
        metadata: metaLabels,
        // Whatever the try used. Inferred from the operator's own dates, and
        // **it has to be saved** — the try would pass and the real import would
        // then fail on `unparseable date`, which is the worst possible place to
        // find out, because the try just said it was fine.
        dates: attempt?.dateCandidates ?? [],
        columns: Object.fromEntries(
          Object.entries(columns)
            .filter(([, header]) => header.trim())
            .map(([field, header]) => [header.trim(), field]),
        ),
      }),
    onSuccess: (result) =>
      toast({
        kind: 'ok',
        title: 'Profile saved',
        detail: `${result.path} — now upload the statement on Add account.`,
      }),
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  const headerAt = pickedHeader ?? seen?.headerRow ?? null
  const header = seen && headerAt !== null ? (seen.grid[headerAt] ?? []) : []
  // `txn_id` drops out of the requirement when the bank prints none (§44).
  // Everything else still has to be answered — the balance chain needs all five
  // and refusing to save half a profile is cheaper than a ledger that imports
  // and does not add up.
  const needed = (seen?.required ?? []).filter((f) => !(derive && f === 'txn_id'))
  /** Every column answered. Enough to *try* — trying needs no name. */
  const mapped = needed.length > 0 && needed.every((f) => (columns[f] ?? '').trim())
  /** Enough to *save*, which also needs somewhere to save it. */
  const ready = mapped && bank.trim()

  return (
    <div className="page">
      <h1>Add a bank</h1>
      <p className="lede">
        {/* SPEC §55. "One bank out of the box" stopped being true when profiles
            started shipping, and the list is the first thing worth knowing on
            this page — most people arriving here do not need it. */}
        <Known />{' '}
        Any other bank needs a one-off description of
        where its columns are — no code, and the file never leaves this machine.
      </p>

      <h2 className="section">1 — show it your statement</h2>
      <p className="muted">
        Read only. Nothing is staged, pushed or registered, and it is safe on a file
        passbook cannot parse yet — that is what it is for.
      </p>
      <input
        ref={file}
        type="file"
        accept=".xls,.xlsx,.pdf,.csv,application/vnd.ms-excel,application/pdf"
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
              differently, so passbook asks rather than guesses. It never leaves this
              machine. <strong>Never use an online PDF unlocker.</strong>
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
              disabled={!password || inspect.isPending}
              data-working={inspect.isPending}
              onClick={() => chosen.current && send(chosen.current, password)}
            >
              Unlock
            </button>
          </div>
        </>
      )}
      {inspect.isPending && <Progress label="Reading the file" />}

      {seen && (
        <>
          <h2 className="section">2 — what a parser sees</h2>
          <p className="muted">
            {seen.container} · {seen.rows} rows.{' '}
            {headerAt !== null
              ? `Row ${headerAt} holds your column names.`
              : 'No header row recognised.'}{' '}
            <strong>Click a row number</strong> to say which row holds them — passbook
            guesses, and on a bank it has never seen there is nothing to guess from.
          </p>
          <div className="sheet">
            <div className="sheet__scroll">
              <table className="changes">
                <caption className="visually-hidden">The first rows, as a parser reads them</caption>
                <tbody>
                  {seen.grid.map((row, index) => (
                    <tr key={index} className={index === headerAt ? 'now' : undefined}>
                      <td className="num muted">
                        {/* Clickable, always. Passbook's guess is right often
                            enough to keep, and wrong often enough that it must
                            not be the only answer available. */}
                        <button
                          type="button"
                          className="linklike"
                          aria-pressed={index === headerAt}
                          title="These are my column names"
                          onClick={() => {
                            setPickedHeader(index)
                            setColumns({})
                          }}
                        >
                          {index}
                        </button>
                      </td>
                      {row.map((cell, column) => (
                        <td key={column} className="date">
                          {cell}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
          <Why label="Reading this — four things that have each caused a bug">
            <p className="muted">
              Every cell is shown as <code>repr()</code> on purpose. <code>' '</code> is a
              single space and <code>''</code> is empty, and confusing the two turns "no
              value" into <code>0</code> — the likeliest silent bug here. Also look for: the
              header's <em>exact</em> text including any typo, the date format, and whether
              amounts carry thousands separators.
            </p>
          </Why>

          <h2 className="section">3 — name the columns</h2>
          <div className="fields">
            <label className="field">
              <span>Bank (short name)</span>
              <input
                value={bank}
                placeholder="union"
                onChange={(event) => setBank(event.target.value.toLowerCase())}
              />
            </label>
          </div>
          <div className="fields">
            {[...seen.required]
              .sort((a, b) => READING_ORDER.indexOf(a) - READING_ORDER.indexOf(b))
              .map((field) => (
              <label className="field" key={field} hidden={field === 'txn_id' && derive}>
                <span>{field}</span>
                <select
                  value={columns[field] ?? ''}
                  onChange={(event) => {
                    setColumns({ ...columns, [field]: event.target.value })
                    // The attempt described the previous mapping. Keeping it on
                    // screen next to a different one is how "it said it was
                    // fine" happens.
                    setAttempt(null)
                  }}
                >
                  <option value="">— which column? —</option>
                  {header.map((cell, index) => (
                    <option key={index} value={cell.replace(/^'|'$/g, '')}>
                      {cell}
                    </option>
                  ))}
                </select>
                <span className="muted">{WHAT[field as keyof typeof WHAT]}</span>
              </label>
            ))}
          </div>
          {/* SPEC §44. Union Bank's export has a cheque-number column that is
              blank for every UPI and NEFT row, which is most of them — so
              there is genuinely nothing to map here, and requiring it was the
              wall. Opt-in, never inferred, and it says what it costs. */}
          <label className="tickbox">
            <input
              type="checkbox"
              checked={derive}
              onChange={(event) => {
                setDerive(event.target.checked)
                if (event.target.checked) {
                  const next = { ...columns }
                  delete next.txn_id
                  setColumns(next)
                }
              }}
            />
            <span>
              <strong>My bank prints no reference number for each row.</strong>{' '}
              passbook will build one from the row itself — its date, amount, narration and
              the balance after it. That is stable across re-downloads, so an overlapping
              statement still skips rows it already has.{' '}
              <em>
                The catch: if your bank ever restates a balance, every row below it counts
                as new. If there IS a reference column, map it instead.
              </em>
            </span>
          </label>

          {/* SPEC §45. Without this the statement parses perfectly — bands,
              columns, dates, the balance chain — and is then refused on the
              NEXT page for a label nobody could name from here. The answer
              belongs where the question is, so Check re-reads the file and says
              what these labels actually found. */}
          <h2 className="section">4 — where the account number is printed</h2>
          <p className="muted">
            Every row is filed under the account number, so passbook reads it from the
            statement and never lets you type one — a digit wrong there would build a
            second, silent ledger. Find it in the table above and give the words printed
            beside it. Case, spaces and punctuation do not matter.
          </p>
          <div className="fields">
            {META.map(({ field, label, why }) => {
              const current =
                Object.entries(metaLabels).find(([, f]) => f === field)?.[0] ?? ''
              return (
                <label className="field" key={field}>
                  <span>{label}</span>
                  <input
                    value={current}
                    placeholder={field === 'account_number' ? 'e.g. A/c No.' : 'optional'}
                    onChange={(event) => {
                      const next = Object.fromEntries(
                        Object.entries(metaLabels).filter(([, f]) => f !== field),
                      )
                      if (event.target.value.trim()) next[event.target.value] = field
                      setMetaLabels(next)
                    }}
                  />
                  <span className="muted">{why}</span>
                </label>
              )
            })}
          </div>
          <div className="actions">
            <button
              type="button"
              disabled={inspect.isPending || !chosen.current}
              data-working={inspect.isPending}
              onClick={() => chosen.current && send(chosen.current, password)}
            >
              {inspect.isPending ? 'Checking…' : 'Check these labels'}
            </button>
            {seen.metaFound?.account_number ? (
              <span className="found">
                <Tick title="" /> Account number found: {seen.metaFound.account_number}
              </span>
            ) : (
              <span className="muted">
                Not found yet — the profile can be saved, but the import will refuse.
              </span>
            )}
          </div>

          {headerAt === null && (
            <Notice kind="warn">
              <p>
                <strong>Point at the header row first.</strong> Click the number beside the
                row that holds your column names — <code>Date</code>,{' '}
                <code>Particulars</code> and the rest — in the table above. The dropdowns
                below fill from whichever row you pick.
              </p>
            </Notice>
          )}

          <h2 className="section">5 — try it before saving anything</h2>
          <p className="muted">
            Parses your statement with the mapping above and checks the balance chain.
            Nothing is written — no profile, no account, no transaction.
          </p>
          <div className="actions">
            <button
              type="button"
              disabled={!mapped || attemptParse.isPending || !chosen.current}
              data-working={attemptParse.isPending}
              onClick={() => attemptParse.mutate()}
            >
              {attemptParse.isPending ? 'Parsing…' : 'Try it'}
            </button>
          </div>
          {attemptParse.isPending && <Progress label="Parsing your statement" />}
          {attempt && <Attempted attempt={attempt} />}

          <div className="actions">
            <button
              type="button"
              className="primary"
              // Try first. Not pedantry: the date format is inferred by the try
              // and saved with the profile, so saving without one writes a
              // profile that cannot read its own bank's dates.
              disabled={!ready || save.isPending || !attempt}
              data-working={save.isPending}
              onClick={() => save.mutate()}
            >
              {save.isPending ? 'Saving…' : attempt ? 'Save this bank' : 'Try it first'}
            </button>
            <Link className="button" to="/accounts/add">
              Add account
            </Link>
          </div>

          <Notice>
            <p>
              Saving writes <code>config/banks/&lt;bank&gt;.yaml</code> and loads it back to
              prove it parses. Then go to <strong>Add account</strong>, upload the same
              statement and pick your bank. If the balance chain does not hold there, a
              column is mapped wrongly — fix it here, never the check.
            </p>
          </Notice>
        </>
      )}
    </div>
  )
}

/**
 * What the mapping actually produced. SPEC §49.
 *
 * On success, the numbers that matter — rows, period, opening and closing —
 * because a count alone can be right while every figure is in the wrong column.
 *
 * On failure, **the banded rows themselves.** The operator looks at their own
 * statement, in their own browser, and sees which column their balance landed
 * in. `row 10: transaction has no balance` is true and unactionable; the same
 * row with its cells laid out answers the question in one glance — and it is
 * the only way to diagnose this without someone else reading the file.
 */
function Attempted({ attempt }: { attempt: Attempt }) {
  const good = attempt.ok === true
  return (
    <>
      <Notice kind={good ? 'ok' : 'warn'}>
        {good ? (
          <p>
            <Tick title="" /> <strong>Parsed {count(attempt.rows ?? 0, 'transaction')}</strong>{' '}
            for {attempt.account}, {attempt.period?.[0]} to {attempt.period?.[1]}. Opening{' '}
            {attempt.opening}, closing {attempt.closing}.{' '}
            <strong>The balance chain holds.</strong> Save it.
          </p>
        ) : null}
        {good && attempt.dateCandidates && attempt.dateCandidates.length > 1 ? (
          <p>
            <strong>Two date formats read your file equally well</strong> —{' '}
            {attempt.dateCandidates.join(', ')} — because every date in it has a day of 12
            or less. Both are saved and the first wins; if the months come out wrong, that
            is why.
          </p>
        ) : null}
        {good ? (
          <p className="muted">
            Dates read as <code>{attempt.dateCandidates?.[0] ?? 'the built-in format'}</code>
            {attempt.dateSample && <> — e.g. {attempt.dateSample}</>}.
          </p>
        ) : (
          <>
            <p>
              <Cross title="" /> {attempt.error}
            </p>
            <p>
              {attempt.rows !== undefined
                ? `It read ${attempt.rows} rows and they do not add up, so a column is ` +
                  'mapped wrongly — nine times out of ten debit and credit are swapped, ' +
                  'or balance points at the wrong column.'
                : attempt.failedRow !== undefined
                  ? `Row ${attempt.failedRow} is highlighted below, as this mapping reads ` +
                    'it. A dash is a cell that came out empty — that is the column to fix.'
                  : 'Look at the rows below: each one is your statement as this mapping ' +
                    'reads it, and a dash is a cell that came out empty.'}
            </p>
          </>
        )}
      </Notice>

      {!good && attempt.banded && (
        <div className="sheet">
          <div className="sheet__scroll">
            <table className="changes">
              <caption className="visually-hidden">Your statement, as this mapping reads it</caption>
              <thead>
                <tr>
                  <th scope="col">row</th>
                  {attempt.order.map((field) => (
                    <th key={field} scope="col">
                      {field}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {attempt.banded.map((row, offset) => {
                  const index = (attempt.bandedFrom ?? 0) + offset
                  return (
                  <tr key={index} className={index === attempt.failedRow ? 'now' : undefined}>
                    <td className="num muted">{index}</td>
                    {attempt.order.map((field, column) => (
                      <td key={field} className="date">
                        {row[column] || <span className="muted">—</span>}
                      </td>
                    ))}
                  </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {!good && attempt.failedRowShapes && (
        <Notice>
          <p>
            <strong>The failing row, as shapes.</strong> Safe to paste anywhere — every
            value is reduced to its shape, and <code>—</code> is a cell that came out
            empty. That empty one is the column to fix.
          </p>
          <pre className="diff">{attempt.failedRowShapes}</pre>
        </Notice>
      )}

      {!good && attempt.shapes && <ShareShape shapes={attempt.shapes} />}

      {!good && attempt.edges && (
        <Why label="Where this mapping found each column">
          <p className="muted">
            Positions on the page, in points. <strong>edge</strong> is where the figures in
            a money column end — learned from the figures themselves, not from the heading,
            because the two only line up if your bank right-aligns both (§48).
          </p>
          <ul className="plain">
            {Object.entries(attempt.columns ?? {}).map(([field, [x0, x1]]) => (
              <li key={field}>
                <code>{field}</code> — heading at {x0}–{x1}
                {attempt.edges?.[field] !== undefined && <> · edge {attempt.edges[field]}</>}
              </li>
            ))}
          </ul>
          <p className="muted">
            If a money column has no <strong>edge</strong>, no figures were found lined up
            under it — that is the mapping to fix.
          </p>
        </Why>
      )}
    </>
  )
}


/**
 * The page as shapes, for sending to someone who must not see the page. §50.
 *
 * The operator offered their statement to get this fixed and did not want to,
 * which is the right instinct and should not be the price of a bug report.
 * Every token here is reduced to its shape and its position: `09-05-2026` is
 * `92-92-94`, a payee is `A5`, an amount is `9,999.99`. Two tokens with the
 * same shape are interchangeable to a parser, and none of it can be turned back
 * into a statement.
 *
 * That is enough to debug every banding failure this module has had, because a
 * banding failure is made of geometry.
 */
function ShareShape({ shapes }: { shapes: string[] }) {
  const [copied, setCopied] = useState(false)
  const text = shapes.join('\n')

  return (
    <Why label="Share this without sharing your statement">
      <p className="muted">
        Every word below is reduced to its <strong>shape</strong> and its position on the
        page — <code>09-05-2026</code> becomes <code>92-92-94</code>, a payee becomes{' '}
        <code>A5</code>, an amount becomes <code>9,999.99</code>. No name, no payee, no
        account number and no figure. It cannot be turned back into your statement, and it
        is enough to find a column that is banding wrongly.
      </p>
      <div className="actions">
        <button
          type="button"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(text)
              setCopied(true)
              window.setTimeout(() => setCopied(false), 4000)
            } catch {
              // Clipboard blocked (no HTTPS, or the browser said no). The text
              // is on screen and selectable; saying so beats a dead button.
              setCopied(false)
            }
          }}
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
        <span className="muted">or select it below</span>
      </div>
      <pre className="diff">{text}</pre>
    </Why>
  )
}


/**
 * Which banks passbook already reads. SPEC §55.
 *
 * Most people who land on this page do not need it: a profile for their bank
 * ships with the install, and the page's own opening line used to tell them
 * the opposite ("passbook reads one bank out of the box"). Saying what is
 * already known is the difference between a ten-minute task and none.
 */
function Known() {
  const { data } = useQuery({
    queryKey: ['accounts', 'candidates'],
    queryFn: () => api.get<{ banks: string[] }>('/accounts/candidates'),
    retry: false,
  })
  const banks = data?.banks ?? []
  if (banks.length === 0) return <>passbook reads a few banks out of the box.</>
  return (
    <>
      passbook already reads <strong>{banks.join(', ')}</strong> — if yours is there, you
      do not need this page. Go straight to <Link to="/accounts/add">Add an account</Link>.
    </>
  )
}
