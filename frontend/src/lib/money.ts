/* Money formatting. SPEC §16.5.
 *
 * The API sends amounts as exact decimal STRINGS, never JSON numbers, because
 * a JSON number is an IEEE double the moment it is parsed and CLAUDE.md's
 * first non-negotiable — money is Decimal, never float — does not stop at the
 * process boundary. So these functions never call Number() on an amount.
 * Grouping is done on the digits themselves.
 *
 * Indian grouping, matching how the bank itself prints: the last three digits,
 * then pairs. 1234567.89 reads 12,34,567.89, not 1,234,567.89. Invisible below
 * a lakh, which is every row today — and correct the first time an annual view
 * crosses one, which is within a year.
 */

function groupIndian(digits: string): string {
  if (digits.length <= 3) return digits
  const last3 = digits.slice(-3)
  const rest = digits.slice(0, -3)
  return `${rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',')},${last3}`
}

/** Split a decimal string into sign, grouped integer part, and two decimals. */
function parts(value: string): { sign: string; int: string; frac: string } {
  const negative = value.trimStart().startsWith('-')
  const raw = negative ? value.trim().slice(1) : value.trim()
  const [intRaw = '0', fracRaw = ''] = raw.split('.')
  const int = intRaw.replace(/\D/g, '') || '0'
  const frac = `${fracRaw.replace(/\D/g, '')}00`.slice(0, 2)
  return { sign: negative ? '-' : '', int: groupIndian(int), frac }
}

export const EMDASH = '—'

/** `₹12,34,567.89`. An absent amount is an em dash, never a zero. */
export function formatINR(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return EMDASH
  const { sign, int, frac } = parts(value)
  return `${sign}₹${int}.${frac}`
}

/** Same, without the symbol — for columns whose header already says INR. */
export function formatAmount(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return ''
  const { sign, int, frac } = parts(value)
  return `${sign}${int}.${frac}`
}

/** `09 May 2026` from an ISO date, without constructing a Date. */
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

export function formatDate(iso: string): string {
  const [y, m, d] = iso.split('-')
  if (!y || !m || !d) return iso
  return `${d} ${MONTHS[Number(m) - 1] ?? m} ${y}`
}

/** `09 May` — the ledger column, where the year is in the period header. */
export function formatDayMonth(iso: string): string {
  const [, m, d] = iso.split('-')
  if (!m || !d) return iso
  return `${d} ${MONTHS[Number(m) - 1] ?? m}`
}

/** `12 May 26` — compact enough for a table column. */
export function formatShortDate(iso: string): string {
  const [y, m, d] = iso.split('-')
  if (!y || !m || !d) return iso
  return `${d} ${MONTHS[Number(m) - 1] ?? m} ${y.slice(2)}`
}

/**
 * `1 row` / `6 rows`. SPEC §17.5.2: `day(s)` is a form field, not a sentence.
 *
 * That rule was set when the sync wording was fixed and then left applying to
 * one string. Seven others were still writing `device(s)`, `duplicate(s)`,
 * `row(s)` — including a red button that deletes and re-pushes, which is the
 * last place to sound like a form.
 */
export function count(n: number, singular: string, plural = `${singular}s`): string {
  return `${n} ${n === 1 ? singular : plural}`
}

/** `01:51` from `01:51:33`. */
export function formatClock(time: string | null): string | null {
  if (!time) return null
  return time.slice(0, 5)
}

/** Fractional hours past midnight, for positioning on the Day Rail. */
export function hoursPastMidnight(time: string | null): number | null {
  if (!time) return null
  const [h, m, s] = time.split(':').map(Number)
  if (h === undefined || Number.isNaN(h)) return null
  return h + (m ?? 0) / 60 + (s ?? 0) / 3600
}
