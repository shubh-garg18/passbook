/* The sheet: a statement rendered as ledger paper. SPEC §16.4.
 *
 * Rows arrive in sheet order and complete. That is not a styling preference —
 * this table carries a Balance column, and a Balance column over a filtered or
 * reordered subset asserts a continuity that is not there. §6.6 is the spine
 * of this project; a view that appears to break it teaches the operator to
 * distrust the check that matters most.
 *
 * There is deliberately **no category column here.** Rules are applied by
 * Firefly at push time, so at preview no category exists yet. Showing one
 * would be either a guess (D10) or a lie.
 */

import { DayRail, DayRailScale } from './DayRail'
import { Money, Token } from './ui'
import { formatClock, formatDayMonth } from '../lib/money'
import type { Txn } from '../lib/types'

export function Ledger({ transactions }: { transactions: Txn[] }) {
  return (
    <div className="sheet">
      <div className="sheet__scroll">
        <table className="ledger">
          <caption className="visually-hidden">
            Every row in the statement, in sheet order, with a running balance.
          </caption>
          <thead>
            <tr>
              <th scope="col">Date</th>
              <th scope="col">Time</th>
              <th scope="col" className="th-rail">
                Day rail
                <DayRailScale />
              </th>
              <th scope="col">Payee</th>
              <th scope="col" className="num">
                Withdrawn
              </th>
              <th scope="col" className="num">
                Deposited
              </th>
              <th scope="col" className="num">
                Balance
              </th>
            </tr>
          </thead>
          <tbody>
            {transactions.map((t, index) => (
              <tr key={t.id}>
                <td className="date">{formatDayMonth(t.date)}</td>
                <td className="time">{formatClock(t.time) ?? '—'}</td>
                <td className="cell-rail">
                  <DayRail time={t.time} label={t.display ?? t.channel} index={index} />
                </td>
                <td>
                  <span className="party">
                    {t.alias ? (
                      <>
                        <span className="party__name">{t.alias}</span>
                        {t.payee && <Token token={t.payee} />}
                      </>
                    ) : (
                      /* No alias: the raw token IS the display, at full
                         strength. This is the row where the bank's ~10-char
                         truncation actually matters, so the cut mark is the
                         one signal the state needs. */
                      <span className="party__name party__name--raw">
                        <Token token={t.payee ?? t.channel} strong />
                      </span>
                    )}
                  </span>
                </td>
                <td className="num">
                  <Money value={t.debit} plain />
                </td>
                <td className="num col-deposit">
                  <Money value={t.credit} plain />
                </td>
                <td className="num bal">
                  <Money value={t.balance} plain />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
