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
  /** `****1111`. The full number never crosses this boundary (§11). */
  account: string
  assetAccount: string
  label: string
  selected: boolean
}

export type Accounts = {
  accounts: AccountSummary[]
  selected: string | null
  /** Server-decided: the switcher exists only when this is true, so a
   *  single-account install never learns the feature is there (§21.3). */
  multiple: boolean
}

/** One row of a breakdown. `amount` is a decimal string, like every amount. */
export type Slice = { name: string; amount: string; count: number }

/**
 * The Ledger page's charts. SPEC §18.
 *
 * `spend`/`income` are the figures that respect §8 and §8.1; `grossSpend` and
 * `grossIncome` are what Firefly reports by transaction type, kept so the page
 * can show what was excluded rather than quietly differing from the statement.
 */
export type Analysis = {
  spend: string
  grossSpend: string
  income: string
  grossIncome: string
  withdrawals: number
  deposits: number
  categories: Slice[]
  excludedSpend: Slice[]
  excludedSpendTotal: string
  excludedIncome: Slice
  refunds: Slice
  rollups: { tag: string; amount: string; count: number; parts: Slice[] }[]
  months: { month: string; spend: string; income: string; partial: boolean }[]
  /** 24 buckets, spend rows only. Sums to `clocked`, NOT to `counted`. */
  hours: number[]
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
  rows: PayeeRow[]
  categories: string[]
  total: number
  totalClocked: number
}

export type DiffResponse = {
  changes: { path: string; diff: string }[]
  aliasChanges: Record<string, string>
  categoryChanges: Record<string, string>
}

export type ReapplyChange = {
  externalId: string
  date: string
  amount: string
  oldDescription: string
  newDescription: string
  oldCategory: string
  newCategory: string
  nameChanged: boolean
  categoryChanged: boolean
}

export type ReapplyPreview = {
  considered: number
  renames: number
  recats: number
  changes: ReapplyChange[]
  /** The precondition, not a warning: a purge is refused without a recent dump.
   *  This container cannot take one — it can only read `backups/`. §18.7. */
  dump: {
    name: string | null
    ageMinutes: number | null
    maxAgeMinutes: number
    fresh: boolean
  }
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
  ledger: {
    ok: boolean | null
    headline: string
    failed?: number
    unchecked?: number
    checks: { name: string; ok: boolean | null; detail: string }[]
  }
  drift: string[]
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
