import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { api } from '../lib/api'
import type { Activity } from '../lib/types'
import { Card } from '../components/ui'
import { Skeleton, Why, describe } from '../components/feedback'

/**
 * What has been done to this ledger, newest first.
 *
 * **The half this exists for is config, not money.** Every statement is in
 * `archive/` and the Status page's ledger check compares the ledger against
 * it, so the rows already have a paper trail. Renaming a payee, moving a
 * category, deleting one, removing an account — those change what the ledger
 * *says*, and they left no trace anywhere: the config files are deliberately
 * not in version control, because they name real people.
 *
 * So the question a person actually asks a week later — *"why does this read
 * differently from last time?"* — had no answer but memory. Now it has a page.
 *
 * It reports and never computes. No figure anywhere comes from this table.
 */
export function ActivityPage() {
  const [action, setAction] = useState<string>('')
  const { data, isPending, error } = useQuery({
    queryKey: ['activity', action],
    queryFn: () => api.get<Activity>(`/activity${action ? `?action=${action}` : ''}`),
  })

  if (isPending) return <Skeleton rows={8} />
  if (error)
    return (
      <div className="page">
        <h1>Activity</h1>
        <p className="lede">{describe(error).detail}</p>
      </div>
    )

  return (
    <div className="page">
      <h1>Activity</h1>
      <p className="lede">
        Everything that changed your ledger or the names in it, most recent first.
      </p>

      {/* The same control the date range uses, because it is the same act:
          one of these is chosen and the rest are not. A second pill style for
          the same job would be two answers to one question — and the range
          picker's selected state is already measured against its ground. */}
      <div className="range">
        <div className="range__row" role="group" aria-label="Filter by what happened">
          <button
            type="button"
            className={action === '' ? 'chipbtn chipbtn--on' : 'chipbtn'}
            aria-pressed={action === ''}
            onClick={() => setAction('')}
          >
            Everything
          </button>
          {data.actions.map((name) => (
            <button
              key={name}
              type="button"
              className={action === name ? 'chipbtn chipbtn--on' : 'chipbtn'}
              aria-pressed={action === name}
              onClick={() => setAction(name)}
            >
              {LABELS[name] ?? name}
            </button>
          ))}
        </div>
      </div>

      {data.error && <p className="warn">Could not read the log — {data.error}</p>}

      {data.entries.length === 0 ? (
        <Card title="Nothing yet">
          <p className="muted">
            {action
              ? 'Nothing of that kind has happened yet.'
              : 'Upload a statement or name a payee and it will show up here.'}
          </p>
        </Card>
      ) : (
        <Card title={`${data.entries.length} entr${data.entries.length === 1 ? 'y' : 'ies'}`}>
          <ol className="activity">
            {data.entries.map((entry, i) => (
              <li key={`${entry.at}-${i}`}>
                <time dateTime={entry.at}>{when(entry.at)}</time>
                <span className={`activity__tag activity__tag--${entry.action}`}>
                  {LABELS[entry.action] ?? entry.action}
                </span>
                <span className="activity__what">{entry.summary}</span>
              </li>
            ))}
          </ol>
        </Card>
      )}

      <Why label="What this does and does not tell you">
        <p>
          It records what was <em>done</em>. Whether the ledger is <em>right</em> is a
          different question, and <Link to="/status">Status</Link> answers that one by
          comparing every row against the statements in your archive.
        </p>
        <p>
          Nothing on any other page is calculated from this list. It is a record, not a
          second copy of your ledger.
        </p>
      </Why>
    </div>
  )
}

/** Plain words. The stored values are slugs so they can be filtered on. */
const LABELS: Record<string, string> = {
  import: 'Imported',
  rename: 'Renamed',
  categorise: 'Categories',
  resync: 'Applied',
  purge: 'Deleted',
  account: 'Account',
  backup: 'Backup',
  upgrade: 'Updated',
  rebuild: 'Rebuilt',
}

/** `2026-09-15T07:26:11+05:30` → `15 Sep, 07:26`. Local, because the person
 *  reading it was in their own timezone when they did the thing. */
function when(iso: string): string {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return iso
  return at.toLocaleString(undefined, {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}
