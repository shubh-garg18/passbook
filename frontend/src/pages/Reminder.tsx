/* The statement reminder. SPEC §24.
 *
 * passbook is not the alarm clock and does not pretend to be: WSL2 stops when
 * Windows sleeps, so a reminder this machine owns is one that will not arrive.
 * This page picks a schedule and hands it to a calendar that is awake.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { api } from '../lib/api'
import type { Reminder as ReminderData } from '../lib/types'
import { Skeleton, describe, useToast } from '../components/feedback'

const HOURS = Array.from({ length: 24 }, (_, i) => i)
const MINUTES = [0, 15, 30, 45]
const LEADS = [
  { value: 0, label: 'At the time' },
  { value: 30, label: '30 minutes before' },
  { value: 120, label: '2 hours before' },
  { value: 1440, label: 'A day before' },
]

type Draft = Pick<
  ReminderData,
  'enabled' | 'frequency' | 'weekday' | 'day_of_month' | 'hour' | 'minute' | 'lead_minutes'
>

const FIELDS: (keyof Draft)[] = [
  'enabled',
  'frequency',
  'weekday',
  'day_of_month',
  'hour',
  'minute',
  'lead_minutes',
]

function draftOf(data: ReminderData): Draft {
  return Object.fromEntries(FIELDS.map((k) => [k, data[k]])) as Draft
}

export function Reminder() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Draft | null>(null)

  const { data, isPending, error } = useQuery({
    queryKey: ['reminder'],
    queryFn: () => api.get<ReminderData>('/reminder'),
  })

  // Seed once. Re-seeding on every render would fight the operator's typing.
  useEffect(() => {
    if (data && draft === null) setDraft(draftOf(data))
  }, [data, draft])

  const mail = useMutation({
    mutationFn: () => api.post<{ to: string; label: string }>('/reminder/email'),
    onSuccess: (result) =>
      toast({
        kind: 'ok',
        title: 'Invite sent',
        detail: `Sent to ${result.to}. Accept it once — your calendar does the rest.`,
      }),
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  const save = useMutation({
    mutationFn: (next: Draft) => api.put<ReminderData>('/reminder', next),
    onSuccess: (result) => {
      queryClient.setQueryData(['reminder'], result)
      setDraft(draftOf(result))
      toast({ kind: 'ok', title: 'Saved', detail: `Reminder set ${result.label}.` })
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  if (isPending || !draft)
    return (
      <div className="page">
        <h1>Reminder</h1>
        <Skeleton rows={5} />
      </div>
    )
  if (error)
    return (
      <div className="page">
        <h1>Reminder</h1>
        <p className="lede">{describe(error).detail}</p>
      </div>
    )

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft({ ...draft, [key]: value })

  return (
    <div className="page">
      <h1>Reminder</h1>
      <p className="lede">
        Download the statement, upload it, review where the money went — on a schedule you
        pick. Nothing here fires the reminder: your calendar does, so it still reaches you
        when this laptop is shut.
      </p>

      <form
        className="sheet sheet--pad"
        onSubmit={(event) => {
          event.preventDefault()
          save.mutate(draft)
        }}
      >
        <label className="field field--check">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(e) => set('enabled', e.target.checked)}
          />
          <span>Remind me</span>
        </label>

        <div className="fields">
          <label className="field">
            <span>Repeats</span>
            <select
              value={draft.frequency}
              onChange={(e) => set('frequency', e.target.value as Draft['frequency'])}
            >
              <option value="weekly">Weekly</option>
              <option value="fortnightly">Every 2 weeks</option>
              <option value="monthly">Monthly</option>
            </select>
          </label>

          {draft.frequency === 'monthly' ? (
            <label className="field">
              <span>On day</span>
              <select
                value={draft.day_of_month}
                onChange={(e) => set('day_of_month', Number(e.target.value))}
              >
                {Array.from({ length: data.maxDayOfMonth }, (_, i) => i + 1).map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <label className="field">
              <span>On</span>
              <select
                value={draft.weekday}
                onChange={(e) => set('weekday', Number(e.target.value))}
              >
                {data.weekdays.map((name, index) => (
                  <option key={name} value={index}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
          )}

          <label className="field">
            <span>At</span>
            <span className="field__pair">
              <select value={draft.hour} onChange={(e) => set('hour', Number(e.target.value))}>
                {HOURS.map((h) => (
                  <option key={h} value={h}>
                    {String(h).padStart(2, '0')}
                  </option>
                ))}
              </select>
              <select
                value={draft.minute}
                onChange={(e) => set('minute', Number(e.target.value))}
              >
                {MINUTES.map((m) => (
                  <option key={m} value={m}>
                    {String(m).padStart(2, '0')}
                  </option>
                ))}
              </select>
            </span>
          </label>

          <label className="field">
            <span>Alert</span>
            <select
              value={draft.lead_minutes}
              onChange={(e) => set('lead_minutes', Number(e.target.value))}
            >
              {LEADS.map((l) => (
                <option key={l.value} value={l.value}>
                  {l.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="actions">
          <button
            type="submit"
            className="primary"
            disabled={save.isPending}
            data-working={save.isPending}
          >
            {save.isPending ? 'Saving…' : 'Save schedule'}
          </button>
        </div>
      </form>

      <h2 className="section">Add it to your calendar</h2>
      <div className="ways">
        <a className="way way--primary" href={data.googleUrl} target="_blank" rel="noreferrer">
          <span className="way__title">Open in Google Calendar</span>
          <span className="way__note">
            Opens Google with the event and the repeat already filled in. Press Save. Nothing
            to set up.
          </span>
        </a>
        <button
          type="button"
          className="way"
          onClick={() => mail.mutate()}
          disabled={!data.email.configured || mail.isPending}
          data-working={mail.isPending}
        >
          <span className="way__title">
            {mail.isPending ? 'Sending…' : 'Email me the invite'}
          </span>
          <span className="way__note">
            {data.email.configured
              ? `One invitation to ${data.email.to}. Accept it once.`
              : 'Needs a mail server — set one up below.'}
          </span>
        </button>
        <a className="way" href="/api/reminder.ics" download>
          <span className="way__title">Download .ics</span>
          <span className="way__note">
            For Apple Calendar, Outlook, or anything else. Open the file once.
          </span>
        </a>
      </div>

      <Upcoming data={data} draft={draft} />

      <MailSettings data={data} />
    </div>
  )
}

/**
 * The mail server, edited here rather than in `.env`.
 *
 * Stored in `config/reminder.yaml`, gitignored and written 0600 — the same
 * exposure `.env` already has, with the difference that changing it needs no
 * file and no restart.
 *
 * The password field is always empty on load and an empty submit **keeps** what
 * is stored. Sending it back to be re-typed would mean the credential is
 * deleted every time a typo in the host is corrected.
 */
function MailSettings({ data }: { data: ReminderData }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [form, setForm] = useState({
    host: data.email.host,
    port: String(data.email.port),
    user: data.email.user,
    password: '',
    to: data.email.recipient,
  })

  const save = useMutation({
    mutationFn: () =>
      api.put<ReminderData>('/reminder/mail', {
        host: form.host,
        port: Number(form.port) || 587,
        user: form.user,
        to: form.to,
        ...(form.password ? { password: form.password } : {}),
      }),
    onSuccess: (result) => {
      queryClient.setQueryData(['reminder'], result)
      setForm((f) => ({ ...f, password: '' }))
      toast({
        kind: 'ok',
        title: 'Saved',
        detail: result.email.configured
          ? 'Email is set up. Try "Email me the invite".'
          : 'Saved, but not complete yet — host, address and password are all needed.',
      })
    },
    onError: (error) => toast({ kind: 'bad', ...describe(error) }),
  })

  const set = (key: keyof typeof form, value: string) => setForm({ ...form, [key]: value })

  return (
    <details className="why" open={!data.email.configured}>
      <summary>
        {data.email.configured ? 'Mail server — configured' : 'Set up email (optional)'}
      </summary>
      <div className="why__body">
        <p>
          Only needed for <strong>Email me the invite</strong>. The Google Calendar button
          above works without any of this.
        </p>
        <p className="muted">
          For Gmail: <code>smtp.gmail.com</code>, port 587, your address, and a
          16-character <strong>App Password</strong> — Google Account → Security → 2-Step
          Verification → App passwords. Not your account password; Google rejects that.
        </p>
        <form
          className="fields"
          onSubmit={(event) => {
            event.preventDefault()
            save.mutate()
          }}
        >
          <label className="field">
            <span>Server</span>
            <input
              value={form.host}
              placeholder="smtp.gmail.com"
              onChange={(e) => set('host', e.target.value)}
            />
          </label>
          <label className="field field--narrow">
            <span>Port</span>
            <input
              value={form.port}
              inputMode="numeric"
              onChange={(e) => set('port', e.target.value)}
            />
          </label>
          <label className="field">
            <span>Your address</span>
            <input
              type="email"
              value={form.user}
              placeholder="you@gmail.com"
              onChange={(e) => set('user', e.target.value)}
            />
          </label>
          <label className="field">
            <span>App password</span>
            <input
              type="password"
              value={form.password}
              autoComplete="new-password"
              placeholder={data.email.hasPassword ? 'stored — leave blank to keep' : ''}
              onChange={(e) => set('password', e.target.value)}
            />
          </label>
          <label className="field">
            <span>Send to (optional)</span>
            <input
              type="email"
              value={form.to}
              placeholder={form.user || 'same as your address'}
              onChange={(e) => set('to', e.target.value)}
            />
          </label>
        </form>
        <div className="actions">
          <button
            type="button"
            className="primary"
            onClick={() => save.mutate()}
            disabled={save.isPending}
            data-working={save.isPending}
          >
            {save.isPending ? 'Saving…' : 'Save mail server'}
          </button>
        </div>
        <p className="muted">
          Stored in <code>config/reminder.yaml</code>, owner-only. It is never shown here
          again and never leaves this machine except to your mail server.
        </p>
      </div>
    </details>
  )
}

/**
 * The next five firings/**
 * The next five firings, from the server.
 *
 * Computed by the same function that writes the RRULE, so what is listed here
 * is what the calendar will do rather than a second guess at it. Unsaved edits
 * show the *saved* schedule with a note, instead of quietly previewing one
 * thing and exporting another.
 */
function Upcoming({ data, draft }: { data: ReminderData; draft: Draft }) {
  const dirty = FIELDS.some((k) => data[k] !== draft[k])

  return (
    <>
      {/* §115.1. Two stacked warning bands for two states that are neither
          warnings nor stacked in practice — "these are the saved dates, not
          your edits" and "reminders are off". Both are facts about the list
          below, so they are a line above it in the same voice as the timezone
          note. A coloured band says "attend to this"; neither of these needs
          attending to. */}
      <h2 className="section">Next {data.upcoming.length}</h2>
      <ul className="dates">
        {data.upcoming.map((when) => (
          <li key={when}>{format(when)}</li>
        ))}
      </ul>
      <p className="muted">
        {!data.enabled && 'Reminders are off, so nothing is scheduled — the calendar file still exports. '}
        {dirty && `Showing the saved schedule (${data.label}); save to update these. `}
        Times are {data.timezone}.
      </p>
    </>
  )
}

function format(iso: string): string {
  const when = new Date(iso)
  return when.toLocaleString(undefined, {
    weekday: 'long',
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}
