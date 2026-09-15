/* Response shapes. Money is always a decimal STRING — see lib/money.ts. */

export type Stage = 'anonymous' | 'totp' | 'enroll' | 'done'

export type TotpStatus = {
  enrolled: boolean
  enrolledAt: string | null
  backupCodesLeft: number
  /** True at or below `webauth.LOW_BACKUP_CODES`. Computed server-side so every
   *  surface starts worrying at the same count — a lockout with no warning is
   *  the failure mode, and zero is too late to warn at. */
  backupCodesLow: boolean
  rememberedDevices: number
  /** Masked, or `null` when emailed recovery is not available. SPEC §29. */
  recoveryEmail: string | null
}

export type Session = {
  authenticated: boolean
  username: string | null
  stage: Stage
  configured: boolean
  totp: TotpStatus
}

export type SyncStatus = {
  state: 'never' | 'ok' | 'warn' | 'stale'
  age: number | null
  filename: string | null
  headline: string
  detail: string
  /** ISO. The masthead stamps it, so it travels with the age rather than being
   *  recomputed from a second field that could disagree with it. */
  date: string | null
}

export type Overview = {
  /** The selected account's balance, or the SUM across accounts for `all` —
   *  which is a true figure but cannot be reconciled against any one
   *  statement, so `parts` travels with it and the card says so (§21.9). */
  balance: string | null
  fireflyError: string | null
  account: string | null
  selected: string | null
  parts: { slug: string; label: string; account: string; balance: string | null }[]
  sync: SyncStatus
  history: { name: string; when: string; folder: string }[]
  pending: boolean
}

/** The account registry, masked. SPEC §21.9. */
export type AccountSummary = {
  slug: string
  bank: string
  /** `Canara`. The slug is a filename; nothing in the UI shows one. */
  bankName: string
  /** `****1111`. The full number never crosses this boundary (§11). */
  account: string
  assetAccount: string
  /** The operator's own name if they set one, else `Canara ****1111`. §40. */
  label: string
  /** Whether `label` is a decision or a default. */
  renamed: boolean
  selected: boolean
}

export type Accounts = {
  accounts: AccountSummary[]
  selected: string | null
  /** Server-decided: the switcher exists only when this is true, so a
   *  single-account install never learns the feature is there (§21.3). */
  multiple: boolean
}

/** What removing an account would leave behind. SPEC §38. */
export type Removal = {
  account: AccountSummary
  /** null when Firefly could not be asked; `countReason` then says why. */
  ledgerRows: number | null
  archiveFiles: number
  countReason: string
  last: boolean
}

/** One row of a breakdown. `amount` is a decimal string, like every amount. */
export type Slice = { name: string; amount: string; count: number }

/** A named total with the slices of another dimension inside it. SPEC §64. */
export type Breakdown = { name: string; amount: string; count: number; parts: Slice[] }

/**
 * The Ledger page's charts. SPEC §18.
 *
 * `spend`/`income` are the figures that respect §8 and §8.1; `grossSpend` and
 * `grossIncome` are what Firefly reports by transaction type, kept so the page
 * can show what was excluded rather than quietly differing from the statement.
 */
export type Analysis = {
  /** The window the server resolved, and what it is hiding. SPEC §25. */
  window: { range: string; from: string | null; to: string | null }
  outsideWindow: number
  spend: string
  grossSpend: string
  income: string
  grossIncome: string
  /** Every rupee in minus every rupee out — the change in the balance over
   *  the window. NOT `income - spend`: both of those already exclude movement,
   *  so their difference counts nothing that moved. */
  net: string
  withdrawals: number
  deposits: number
  categories: Slice[]
  /** Real spend by counterparty — Firefly's "expense accounts" report, but
   *  through `ledger_analysis` so §8/§8.1's exclusions apply (non-negotiable 9). */
  payees: Slice[]
  /** Counted income by counterparty. The not-earnings deposits are excluded. */
  sources: Slice[]
  /** One thing and what it is made of. Firefly ships these as three separate
   *  report screens (Category, Double, Tag); they are one shape. */
  payeesByCategory: Breakdown[]
  categoriesByPayee: Breakdown[]
  categoriesByTag: Breakdown[]
  sourcesByCategory: Breakdown[]
  categoriesBySource: Breakdown[]
  /** Five-number summaries, for the box plot. Only categories with enough
   *  transactions to summarise honestly appear (see `MIN_FOR_SPREAD`). */
  spread: {
    name: string
    count: number
    low: string
    q1: string
    median: string
    q3: string
    high: string
  }[]
  /** One row per entry in `categories`, same order. `amounts` is aligned with
   *  `months` BY POSITION and zero-padded — never re-key it by month name. */
  categoryMonths: { name: string; amounts: string[]; total: string }[]
  /** One line per account, never a sum. See the note in `/analysis`. */
  balances: {
    slug: string
    label: string
    points: { day: string; balance: string }[]
    /** The last balance before the window, so a windowed line does not open at
     *  its first transaction and read as the opening balance. */
    opening: { day: string; balance: string } | null
  }[]
  excludedSpend: Slice[]
  excludedSpendTotal: string
  excludedIncome: Slice
  refunds: Slice
  rollups: { tag: string; amount: string; count: number; parts: Slice[] }[]
  months: { month: string; spend: string; income: string; partial: boolean }[]
  /** 24 buckets, spend rows only. Sums to `clocked`, NOT to `counted`. */
  hours: number[]
  /** 7 buckets, Monday first. Unlike `hours` this needs only the date, so it
   *  covers every counted row rather than the ones carrying a clock. */
  weekdays: number[]
  weekdaySpend: string[]
  clocked: number
  counted: number
  uncategorised: Slice
  notSpend: string[]
  selected: string | null
  accounts: string[]
  coverage: { from: string; to: string } | null
}

export type Txn = {
  id: string
  date: string
  time: string | null
  channel: string
  payee: string | null
  alias: string | null
  display: string | null
  debit: string | null
  credit: string | null
  balance: string
  reversal: boolean
}

export type Parsed = {
  filename: string
  meta: {
    account: string
    periodFrom: string
    periodTo: string
    openingBalance: string
    closingBalance: string
  }
  count: number
  withdrawn: string
  deposited: string
  warnings: string[]
  transactions: Txn[]
  unknown: string[]
  /** Which account this statement was routed to, by its own metadata — not by
   *  whatever the switcher happens to be showing (§21.9). */
  routed?: { slug: string; label: string; account: string; registered: boolean }
}

export type PushResult = {
  parsed: number
  pushed: number
  /** Skipped because the ledger already holds that transaction, by id (§119). */
  already: number
  /** Refused by Firefly on its content hash — expected to be 0 since §119. */
  duplicates: number
  failed: number
  failures: { id: string; message: string }[]
  archived: string | null
}

export type PayeeRow = {
  token: string
  alias: string
  category: string
  channel: string
  count: number
  withdrawn: string
  deposited: string
  total: string
  first: string
  last: string
  needsDecision: boolean
  hours: number[]
  /** Transactions on this row that carry a clock. Sums the `hours` array, and
   *  is NOT the same as `count` — NEFT, CHG, SCHEME and INT have no clock. */
  clocked: number
}

export type Payees = {
  /** The window the server resolved, echoed so the label cannot drift. */
  window: { range: string; from: string | null; to: string | null }
  /** Rows the window is hiding. A filtered list that does not say it is
   *  filtered is how a token gets decided twice — or never. */
  outsideWindow: number
  rows: PayeeRow[]
  categories: string[]
  total: number
  totalClocked: number
}

/** SPEC §20. `ok: null` means the check could not be run — rendered as
 *  unverified, never as a tick (non-negotiable 11). */
export type LedgerVerdict = {
  ok: boolean | null
  headline: string
  failed?: number
  unchecked?: number
  checks: { name: string; ok: boolean | null; detail: string }[]
}

export type DiffResponse = {
  changes: { path: string; diff: string }[]
  aliasChanges: Record<string, string>
  categoryChanges: Record<string, string>
  /** What this config would do to rows ALREADY in Firefly, computed before the
   *  write from the submitted aliases and categories. `null` when Firefly is
   *  unreachable or unconfigured — writing config works either way. §23. */
  ledger: ReapplyPreview | null
  /** Categories this change would leave with no payees. They keep existing and
   *  can never match again, so a report on one is permanently empty. */
  emptied: string[]
}

export type ReapplyChange = {
  externalId: string
  /** The Firefly transaction group. Empty means the row was never matched, and
   *  an unmatched row is never updated — see `service.sync_ledger`. */
  groupId: string
  date: string
  amount: string
  kind: 'withdrawal' | 'deposit'
  oldDescription: string
  newDescription: string
  oldCategory: string
  newCategory: string
  /** The expense or revenue account on the other side. An alias rename moves
   *  this too, because the pusher uses one name for both. */
  oldCounterparty: string
  newCounterparty: string
  oldTags: string[]
  newTags: string[]
  nameChanged: boolean
  categoryChanged: boolean
  counterpartyChanged: boolean
  tagsChanged: boolean
}

export type ReapplyPreview = {
  considered: number
  renames: number
  recats: number
  counterparties: number
  retags: number
  /** Tags this change would REMOVE, and from how many rows. A tag is what the
   *  roll-ups are built on; losing one is a semantic loss a payee diff cannot
   *  show. SPEC §33. */
  tagsLost: { tag: string; rows: number }[]
  changes: ReapplyChange[]
  /** The precondition, not a warning: a purge is refused without a recent dump.
   *  This container cannot take one — it can only read `backups/`. §18.7.
   *  It does NOT gate the in-place sync, which deletes nothing. */
  dump: {
    name: string | null
    ageMinutes: number | null
    maxAgeMinutes: number
    fresh: boolean
  }
}

/** The result of an in-place sync. SPEC §23. */
export type SyncResult = {
  ok: boolean
  considered: number
  attempted: number
  updated: number
  failed: number
  failures: { externalId: string; message: string }[]
  /** Re-read from Firefly afterwards, not inferred from the request count.
   *  Anything left needs a re-push — an update cannot create a missing row.
   *  `null` means the re-read itself failed: unverified, which is a third
   *  state and must never render as a pass (non-negotiable 11). */
  remaining: number | null
  ledger?: LedgerVerdict
}

export type ReapplyResult = {
  steps: { state: 'ok' | 'bad'; message: string }[]
  balance: string | null
  expected: string | null
  reconciles: boolean
}

export type Artefact = {
  name: string
  size: number
  humanSize: string
  modified: string
  ageDays: number
}

export type Status = {
  sync: SyncStatus
  token: { shapeOk: boolean; expiry: string | null; daysLeft: number | null }
  firefly: {
    about: { version: string; api_version: string; driver: string } | null
    error: string | null
  }
  account: { assetAccount: string | null; assertionConfigured: boolean }
  /** SPEC §20. `ok: null` means the check could not be run here — the strip
   *  shows that as a warning, never as a tick. A green light for something never
   *  looked at is what let §19's incident sit for seven hours. */
  ledger: LedgerVerdict
  backups: {
    local: Artefact[]
    ageDays: number | null
    staleDays: number
    remote: Artefact[]
    remoteError: string | null
  }
  auth: TotpStatus
}

export type EnrollStart = {
  secret: string
  secretPretty: string
  uri: string
  qr: string
}

/** The statement reminder. SPEC §24.
 *
 * `uid` is never sent to the client — it is stable so a re-import updates the
 * calendar event instead of duplicating it, and has no business in a page. */
export type Reminder = {
  enabled: boolean
  frequency: 'weekly' | 'fortnightly' | 'monthly'
  weekday: number
  day_of_month: number
  hour: number
  minute: number
  lead_minutes: number
  label: string
  weekdays: string[]
  frequencies: string[]
  maxDayOfMonth: number
  timezone: string
  /** Computed server-side by the same function that writes the RRULE, so the
   *  list on screen cannot disagree with what the calendar will do. */
  upcoming: string[]
  filename: string
  /** One click into Google Calendar, event and recurrence pre-filled. Needs no
   *  credentials at all, which is why the mail server is optional. */
  googleUrl: string
  /** The mail server, as the page may edit it. **Never the password** —
   *  `hasPassword` only says whether one is stored. */
  email: {
    configured: boolean
    to: string | null
    host: string
    port: number
    user: string
    recipient: string
    hasPassword: boolean
  }
}

/** Taking a backup from the UI. SPEC §37. */
export type BackupState = {
  available: boolean
  reason: string
  dump: { name: string | null; ageMinutes: number | null; maxAgeMinutes: number; fresh: boolean }
}

/** One row of the ledger browser. SPEC §61.
 *
 * There is deliberately **no balance field**. §16.4 refuses a running balance
 * on any view that can be filtered or reordered, and this view is nothing but
 * filtering and reordering.
 */
export type LedgerRow = {
  id: string
  group: string
  account: string
  accountLabel: string
  date: string
  time: string | null
  description: string
  category: string
  counterparty: string
  tags: string[]
  kind: string
  amount: string
  /** The bank's raw narration. Searched, not rendered — a UTR is exactly what
   *  you look for when the display name is no help. */
  narration: string
}

export type Transactions = {
  rows: LedgerRow[]
  /** Rows the filters kept. `total` is every row in scope before them. */
  matched: number
  total: number
  outsideWindow: number
  page: number
  pages: number
  window: { range: string; from: string | null; to: string | null }
  selected: string | null
  accounts: string[]
  sort: string
  /** Every tag in scope, so the tag filter is a dropdown rather than something
   *  you have to already know the value of. */
  tags: string[]
}

/** §103. The credit-card split: which settlements exist, and how much of each
 *  is the paying month's own spending rather than the previous month's. */
export type Settlement = {
  externalId: string
  account: string
  date: string
  amount: string
  payee: string
  category: string
  /** null when nothing is kept — the absence of a rule, not a rule saying 0. */
  keep: string | null
}

export type AttributionData = {
  categories: string[]
  /** False means these rows are not being shifted yet; the first split turns it
   *  on. Said out loud, because "no effect" and "no rows" look identical. */
  configured: boolean
  beforeDay: number
  toDay: number
  rows: Settlement[]
  window: { range: string; from: string | null; to: string | null }
  selected: string | null
}

/** §112. Which categories money arrives under, and which count as earned. */
export type EarningsRow = {
  category: string
  amount: string
  count: number
  counts: boolean
}

export type EarningsData = {
  rows: EarningsRow[]
  window: { range: string; from: string | null; to: string | null }
  selected: string | null
}

