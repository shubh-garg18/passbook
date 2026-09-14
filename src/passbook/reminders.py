"""A statement reminder, delivered by a calendar that is actually awake. SPEC §24.

`service.sync_status` already says the quiet part out loud when a sync is late:

    "There is no cron to catch this for you (SPEC D7: WSL2 sleeps with Windows)."

That is the whole design constraint. A reminder fired by this machine is exactly
the reminder that will not fire — the distro stops when Windows sleeps, there is
no systemd, and CLAUDE.md is explicit that nothing scheduled here is reliable. So
passbook does not try to be the alarm clock. It **writes the schedule** and hands
it to something that is awake: Google Calendar, or any other calendar, via an
RFC 5545 `.ics` file with an `RRULE` and a `VALARM`.

What that buys, concretely:

  * It fires on a phone, so it survives the laptop being shut.
  * It is one file and one import, with no account to create and no service to
    trust — Google Calendar is where the operator already looks.
  * Turning it into an **email** reminder is a per-event setting in the calendar,
    so "mail me" is covered without passbook holding SMTP credentials or needing
    a process running at 9am to send them.

**It carries no financial data.** The event text names the bank and the action —
download the statement, run the sync — and nothing else. No balance, no payee,
no account number, not even the masked last four (§11). The file goes into
Google Drive on import, so this is the one place where "it is only a reminder"
has to be true rather than nearly true.

Nothing here talks to the network.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from .yamlfile import read_yaml

log = logging.getLogger(__name__)

REMINDER_FILE = Path("config/reminder.yaml")

# The bank's own window is what makes lateness expensive: rows that age out of
# Canara's download range are gone from every copy, backups included. Weekly is
# the cadence the rest of the project is built around (§0), so it is the default.
FREQUENCIES = ("weekly", "fortnightly", "monthly")

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# RFC 5545 two-letter day names, indexed like `date.weekday()` (Monday = 0).
_ICS_DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

# Monthly on the 29th, 30th or 31st silently skips February — RFC 5545 drops an
# occurrence that does not exist rather than clamping it. A reminder that
# vanishes for a month is worse than one a few days early, so the UI offers 1-28
# and this is the guard behind it.
MAX_DAY_OF_MONTH = 28

# Asia/Kolkata has no daylight saving and has been UTC+05:30 since 1945, so the
# VTIMEZONE below is a single standing rule rather than a ruleset. Written out
# in full anyway: a floating time is interpreted in the *viewer's* zone, which
# for a calendar synced to a phone that travels is not the same thing.
TZID = "Asia/Kolkata"
_UTC_OFFSET = "+0530"


@dataclass
class Schedule:
    """When to be reminded to download a statement.

    `uid` is generated once and then kept, because re-importing an `.ics` with
    the same UID *updates* the event in the calendar instead of adding a second
    copy of it. Changing the schedule and re-importing should not leave the
    operator deleting duplicates by hand.
    """

    enabled: bool = True
    frequency: str = "weekly"
    weekday: int = 6  # Sunday; `date.weekday()` numbering
    day_of_month: int = 1
    hour: int = 19
    minute: int = 0
    lead_minutes: int = 30
    uid: str = ""
    # Bumped on every save. A calendar treats a lower-or-equal SEQUENCE as a
    # duplicate and ignores the change, so an edited reminder would import as a
    # no-op — visibly successful and completely inert.
    sequence: int = 0

    def __post_init__(self) -> None:
        self.frequency = str(self.frequency).lower().strip()
        if self.frequency not in FREQUENCIES:
            raise ValueError(f"frequency must be one of {FREQUENCIES}, not {self.frequency!r}")
        self.weekday = _bounded(self.weekday, 0, 6, "weekday")
        self.day_of_month = _bounded(self.day_of_month, 1, MAX_DAY_OF_MONTH, "day_of_month")
        self.hour = _bounded(self.hour, 0, 23, "hour")
        self.minute = _bounded(self.minute, 0, 59, "minute")
        self.lead_minutes = _bounded(self.lead_minutes, 0, 10080, "lead_minutes")
        self.sequence = max(0, int(self.sequence))
        if not self.uid:
            # Random, not derived from the account: a UID travels into Google
            # Calendar and there is nothing about this install worth putting in
            # it (§11).
            self.uid = f"{uuid.uuid4()}@passbook.localhost"

    @property
    def label(self) -> str:
        """How the schedule reads in a sentence."""
        at = f"{self.hour:02d}:{self.minute:02d}"
        if self.frequency == "monthly":
            return f"monthly on day {self.day_of_month} at {at}"
        every = "every " if self.frequency == "weekly" else "every other "
        return f"{every}{WEEKDAYS[self.weekday]} at {at}"

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "frequency": self.frequency,
            "weekday": self.weekday,
            "day_of_month": self.day_of_month,
            "hour": self.hour,
            "minute": self.minute,
            "lead_minutes": self.lead_minutes,
            "uid": self.uid,
            "sequence": self.sequence,
        }


@dataclass
class Mail:
    """How to send the invite. Configured from the Reminder page, not `.env`.

    It lives beside the schedule in `config/reminder.yaml`, which is gitignored
    and written **0600**. That is the same exposure `.env` already has —
    plaintext on the operator's own disk — with the difference that they never
    have to open a file or restart anything to change it.

    `password` is never returned by the API and never logged. `configured` is
    what the UI asks; the value itself only ever leaves this module inside an
    SMTP conversation.
    """

    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    to: str = ""

    def __post_init__(self) -> None:
        self.host = str(self.host or "").strip()
        self.user = str(self.user or "").strip()
        self.password = str(self.password or "")
        self.to = str(self.to or "").strip()
        self.port = _bounded(self.port or 587, 1, 65535, "port")

    @property
    def recipient(self) -> str:
        """Where the invite goes. The sender's own address unless told otherwise."""
        return self.to or self.user

    @property
    def ready(self) -> bool:
        return bool(self.host and self.user and self.password and self.recipient)

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "to": self.to,
        }


def _bounded(value, low: int, high: int, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a whole number, not {value!r}") from exc
    if not low <= number <= high:
        raise ValueError(f"{name} must be between {low} and {high}, not {number}")
    return number


# --- when it fires -----------------------------------------------------------


def next_occurrences(schedule: Schedule, after: datetime, count: int = 5) -> list[datetime]:
    """The next `count` firings strictly after `after`.

    Computed here rather than left to the calendar so the UI can show what the
    RRULE will actually do. A recurrence rule is easy to write and easy to get
    subtly wrong — `BYMONTHDAY=31` is the obvious one — and a schedule the
    operator cannot see before saving is a schedule they will not trust.
    """
    out: list[datetime] = []
    if count <= 0:
        return out

    if schedule.frequency == "monthly":
        cursor = after.replace(
            day=1, hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0
        )
        while len(out) < count:
            moment = cursor.replace(day=schedule.day_of_month)
            if moment > after:
                out.append(moment)
            # Day 28 at most, so stepping to the 28th of the next month by way
            # of the 1st needs no end-of-month arithmetic.
            cursor = (cursor.replace(day=1) + timedelta(days=32)).replace(day=1)
        return out

    step = 7 if schedule.frequency == "weekly" else 14
    first = _first_weekday_on_or_after(after.date(), schedule.weekday)
    moment = datetime.combine(first, after.time()).replace(
        hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0
    )
    if moment <= after:
        moment += timedelta(days=step)
    while len(out) < count:
        out.append(moment)
        moment += timedelta(days=step)
    return out


def _first_weekday_on_or_after(day: date, weekday: int) -> date:
    return day + timedelta(days=(weekday - day.weekday()) % 7)


def suggest(last_sync_weekday: int | None = None) -> Schedule:
    """A default worth accepting without editing.

    Seeded from the weekday the operator actually last synced on, because the
    habit that already exists is a better guess than a number picked here.
    """
    schedule = Schedule()
    if last_sync_weekday is not None:
        schedule.weekday = _bounded(last_sync_weekday, 0, 6, "weekday")
    return schedule


# --- the file ----------------------------------------------------------------

_HEADER = """\
# When to be reminded to download a statement. SPEC §24.
#
# GITIGNORED, like the rest of config/. It holds no credential and no
# counterparty — only a schedule and the calendar UID that lets a re-import
# update the existing event instead of adding a second one.
#
# passbook does NOT fire this. WSL2 stops when Windows sleeps and there is no
# systemd here, so a reminder this machine owns is one that will not arrive.
# `passbook reminder --ics` writes a calendar file; the calendar does the
# reminding, on a device that is awake.
"""


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = read_yaml(path)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} is not readable YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} should hold a mapping, not {type(data).__name__}")
    return data


def load(path: Path | None = None) -> Schedule:
    """The saved schedule, or a sensible default. A broken file is never guessed around."""
    data = _read(path or REMINDER_FILE)
    known = {f.name for f in Schedule.__dataclass_fields__.values()}
    return Schedule(**{k: v for k, v in data.items() if k in known})


def load_mail(path: Path | None = None) -> Mail:
    """The mail settings, from the same file. Absent is not an error."""
    block = _read(path or REMINDER_FILE).get("mail") or {}
    if not isinstance(block, dict):
        return Mail()
    known = {f.name for f in Mail.__dataclass_fields__.values()}
    return Mail(**{k: v for k, v in block.items() if k in known})


def _write(path: Path, data: dict) -> Path:
    """Atomic, and **owner-only**: the file holds an app password.

    `chmod` on the temp file before the rename, not after — between a
    world-readable create and a later chmod there is a window, and a window on a
    credential is a bug even on a single-user laptop.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(_HEADER + body, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    return path


def save(schedule: Schedule, path: Path | None = None) -> Path:
    """Write atomically, bumping SEQUENCE so a re-import is not ignored.

    Read-modify-write: the `mail` block lives in the same file and must survive
    a schedule change. Dumping only the schedule wiped it — which would have
    looked like the app password being silently forgotten every time the
    operator changed the time.
    """
    path = path or REMINDER_FILE
    schedule.sequence += 1
    data = _read(path)
    data.update(schedule.to_dict())
    return _write(path, data)


def save_mail(mail: Mail, path: Path | None = None) -> Path:
    """Same file, same care, the other block."""
    path = path or REMINDER_FILE
    data = _read(path)
    data["mail"] = mail.to_dict()
    return _write(path, data)


def mail_settings(settings=None, path: Path | None = None) -> Mail:
    """What to send with: the UI's settings, else `.env`.

    The file wins, because it is the one the operator can change without
    touching a terminal. `.env` stays supported so an install configured before
    this existed keeps working untouched.
    """
    saved = load_mail(path)
    if saved.ready or settings is None:
        return saved
    return Mail(
        host=settings.passbook_smtp_host or "",
        port=settings.passbook_smtp_port or 587,
        user=settings.passbook_smtp_user or "",
        password=settings.passbook_smtp_password or "",
        to=settings.passbook_reminder_email or "",
    )


# --- the calendar file -------------------------------------------------------


def _escape(text: str) -> str:
    """RFC 5545 §3.3.11. Backslash first, or the escapes escape each other."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 §3.1: content lines are folded at 75 **octets**, not characters.

    Counted in octets because the folding limit is a byte limit and the
    description below contains a `₹`-free but non-ASCII em dash; splitting a
    UTF-8 sequence across a fold makes the file unparseable by some clients and
    subtly wrong in others.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    chunks, start = [], 0
    limit = 75
    while start < len(encoded):
        end = min(start + limit, len(encoded))
        # Never split inside a UTF-8 sequence: continuation bytes are 10xxxxxx.
        while end > start and end < len(encoded) and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(encoded[start:end].decode("utf-8"))
        start = end
        limit = 74  # continuation lines carry a leading space
    return "\r\n ".join(chunks)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S")


def _rrule(schedule: Schedule) -> str:
    if schedule.frequency == "monthly":
        return f"FREQ=MONTHLY;BYMONTHDAY={schedule.day_of_month}"
    day = _ICS_DAYS[schedule.weekday]
    if schedule.frequency == "fortnightly":
        return f"FREQ=WEEKLY;INTERVAL=2;BYDAY={day}"
    return f"FREQ=WEEKLY;BYDAY={day}"


# The whole ritual, not just its first step. A reminder that says only
# "download the statement" gets the file onto the disk and stops there, which is
# the half that produces no ledger and answers no question. Three steps, in the
# order they happen, short enough to read on a lock screen.
SUMMARY = "Bank statement — download, upload, review"

STEPS = (
    # No bank named, here of all places: this text is synced to a phone and
    # into Google Drive, and "which bank" is exactly the kind of detail §11
    # keeps out of anything that leaves the machine.
    "Download this week's statement from your bank.",
    "Upload it to passbook (or run `make sync`).",
    "Review where the money went.",
)

DESCRIPTION = (
    "\n".join(f"{n}. {step}" for n, step in enumerate(STEPS, 1))
    + "\n\n"
    + "Statements are only served going back so far, so a gap is data loss "
    "rather than lateness — rows that age out of the download window are gone "
    "from every copy, including the backups.\n"
    "\n"
    "Generated by passbook. The schedule lives in config/reminder.yaml; editing "
    "this event changes only the calendar."
)


def to_ics(
    schedule: Schedule,
    *,
    now: datetime | None = None,
    stamped: datetime | None = None,
    url: str = "http://passbook.localhost",
    method: str = "PUBLISH",
    organizer: str | None = None,
    attendee: str | None = None,
) -> str:
    """One recurring VEVENT, as a complete iCalendar object.

    Deliberately plain: one event, one rule, one alarm. Everything a calendar
    needs to take over the job, and nothing that would make the file worth
    keeping private beyond ordinary tidiness.

    Emitted with `TZID=Asia/Kolkata` and an accompanying VTIMEZONE rather than
    as a floating time, because a floating time is read in whatever zone the
    viewing device is in — which is wrong the moment the phone crosses a border.
    """
    now = now or datetime.now()
    # DTSTAMP is when the object was *written* and RFC 5545 requires it in UTC,
    # unlike DTSTART which is local and carries a TZID. Two different clocks in
    # one file, which is exactly the sort of thing that is wrong for a year
    # before anyone notices.
    stamped = stamped or datetime.now(timezone.utc)
    first = next_occurrences(schedule, now, 1)
    if not first:  # unreachable with a valid schedule; never emit a ruleless event
        raise ValueError("schedule produced no occurrences")
    start = first[0]
    end = start + timedelta(minutes=15)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//passbook//statement reminder//EN",
        "CALSCALE:GREGORIAN",
        # PUBLISH for a downloaded file; REQUEST for one mailed as an
        # invitation. The difference is not cosmetic: Gmail only renders the
        # "Add to calendar" card, and Google Calendar only auto-adds, for a
        # REQUEST that carries both an ORGANIZER and an ATTENDEE. A REQUEST
        # missing either is shown as a plain attachment. (§24.4)
        f"METHOD:{method}",
        "BEGIN:VTIMEZONE",
        f"TZID:{TZID}",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        f"TZOFFSETFROM:{_UTC_OFFSET}",
        f"TZOFFSETTO:{_UTC_OFFSET}",
        "TZNAME:IST",
        "END:STANDARD",
        "END:VTIMEZONE",
        "BEGIN:VEVENT",
        f"UID:{schedule.uid}",
        f"SEQUENCE:{schedule.sequence}",
        f"DTSTAMP:{_stamp(stamped)}Z",
        f"DTSTART;TZID={TZID}:{_stamp(start)}",
        f"DTEND;TZID={TZID}:{_stamp(end)}",
        f"RRULE:{_rrule(schedule)}",
        *(
            [f"ORGANIZER;CN=passbook:mailto:{organizer}"]
            if organizer
            else []
        ),
        *(
            [
                "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;"
                f"PARTSTAT=ACCEPTED;RSVP=FALSE:mailto:{attendee}"
            ]
            if attendee
            else []
        ),
        f"SUMMARY:{_escape(SUMMARY)}",
        f"DESCRIPTION:{_escape(DESCRIPTION)}",
        f"URL:{url}",
        "TRANSP:TRANSPARENT",
        "STATUS:CONFIRMED",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"TRIGGER:-PT{schedule.lead_minutes}M",
        f"DESCRIPTION:{_escape(SUMMARY)}",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    # CRLF throughout, and a trailing one. RFC 5545 §3.1, and some importers
    # reject a file whose last line is unterminated.
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def filename(schedule: Schedule) -> str:
    """A name that says what it is once it is sitting in a Downloads folder."""
    return f"passbook-reminder-{schedule.frequency}.ics"


_SAFE_NAME = re.compile(r"^[a-z0-9.-]+$")


def assert_safe_filename(name: str) -> None:
    """Belt for a name that reaches a Content-Disposition header."""
    if not _SAFE_NAME.match(name):
        raise ValueError(f"unsafe filename {name!r}")


# --- delivery by email -------------------------------------------------------
# SPEC §24.4. Downloading a file and importing it is two steps and a file
# manager; mailing the invite to the address Google Calendar already watches is
# one click in Gmail, and on a phone it is the same click.
#
# **This still does not make passbook the alarm clock.** The mail is sent ONCE
# and carries the recurrence; the calendar owns every firing after that. There
# is no scheduler here and nothing has to be running at 09:00.
#
# stdlib only — `smtplib` and `email` ship with Python. No dependency was added
# for this.


class SendFailed(RuntimeError):
    """The invite could not be sent. Carries a message safe to show a user."""


def send_invite(
    schedule: Schedule, settings=None, *, now: datetime | None = None, mail: "Mail | None" = None
) -> str:
    """Mail the recurring event as a calendar invitation. Returns the recipient.

    The message is `multipart/alternative` with a text part and a
    `text/calendar; method=REQUEST` part. That shape is what makes Gmail render
    an "Add to calendar" card instead of showing an attachment — and with
    Google Calendar's default "add invitations from everyone" it lands on the
    calendar without a click at all.

    A `.ics` copy rides along as an attachment so the mail is still useful in a
    client that does not understand the inline part.
    """
    from email.message import EmailMessage

    mail = mail if mail is not None else mail_settings(settings)
    if not mail.ready:
        raise SendFailed(
            "Email is not set up. Fill in the mail server on the Reminder page — "
            "for Gmail that is smtp.gmail.com, your address, and an App Password "
            "(Google Account -> Security -> 2-Step Verification -> App passwords), "
            "never your account password."
        )

    sender = mail.user
    to = mail.recipient
    ics = to_ics(schedule, now=now, method="REQUEST", organizer=sender, attendee=to)

    message = EmailMessage()
    message["Subject"] = f"{SUMMARY} — {schedule.label}"
    message["From"] = sender
    message["To"] = to
    message.set_content(
        f"passbook will remind you {schedule.label} ({TZID}).\n\n"
        + "\n".join(f"{n}. {step}" for n, step in enumerate(STEPS, 1))
        + "\n\nThis message carries a recurring calendar invitation. Accepting it "
        "once is all that is needed — your calendar handles every reminder after "
        "that, including when this machine is off.\n\n"
        "To be reminded by email rather than a notification, change this event's "
        "notification to \"Email\" in your calendar.\n"
    )
    # The inline calendar part. `method=REQUEST` in the Content-Type as well as
    # in the body: Gmail reads the header.
    message.add_alternative(ics, subtype="calendar", params={"method": "REQUEST", "charset": "UTF-8"})
    message.add_attachment(
        ics.encode("utf-8"),
        maintype="text",
        subtype="calendar",
        filename=filename(schedule),
    )

    _deliver(message, mail)
    log.info("reminder invite sent to %s (%s)", _mask_email(to), schedule.label)
    return to


def send_text(subject: str, body: str, *, settings=None, to: str | None = None) -> str:
    """A plain message over the same mail server. SPEC §29.

    Shared with the sign-in recovery code, which needs exactly this and must not
    grow a second SMTP implementation — a second one would be the place the
    password gets logged.
    """
    from email.message import EmailMessage

    mail = mail_settings(settings)
    if not mail.ready:
        raise SendFailed(
            "Email is not set up. Add the mail server on the Reminder page — for "
            "Gmail that is smtp.gmail.com, your address, and an App Password."
        )
    recipient = (to or mail.recipient).strip()
    if not recipient:
        raise SendFailed("No recipient.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = mail.user
    message["To"] = recipient
    message.set_content(body)
    _deliver(message, mail)
    log.info("sent %r to %s", subject, _mask_email(recipient))
    return recipient


def _deliver(message, mail: "Mail") -> None:
    """One SMTP conversation, one place the password is touched."""
    import smtplib
    import ssl

    host = mail.host
    port = mail.port
    sender = mail.user
    password = mail.password

    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
                smtp.login(sender, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.starttls(context=context)
                smtp.login(sender, password)
                smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        # Gmail rejects an account password here with a 534 and a help link. The
        # distinction is worth making: "wrong password" sends people to reset a
        # password that was never the problem.
        raise SendFailed(
            "The mail server rejected the login. For Gmail this must be an App "
            "Password (Google Account -> Security -> 2-Step Verification -> App "
            f"passwords), not the account password. Server said: {exc.smtp_code}"
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        # Never interpolate the password into a message, and `exc` here cannot
        # contain it — smtplib does not echo credentials in its exceptions.
        raise SendFailed(f"Could not reach {host}:{port} — {type(exc).__name__}: {exc}") from exc


def _mask_email(address: str) -> str:
    """`s***@gmail.com`. An address is personal data and this goes to a log."""
    name, _, domain = address.partition("@")
    if not domain:
        return "***"
    head = name[:1] if name else ""
    return f"{head}***@{domain}"


# --- straight into Google Calendar -------------------------------------------


def google_calendar_url(schedule: Schedule, *, now: datetime | None = None) -> str:
    """A link that opens Google Calendar with the event already filled in.

    **This needs no credentials, no SMTP and no configuration at all** — which
    makes it the right default and the reason the email path is optional. Google
    takes the event as query parameters on `/calendar/render`, including the
    recurrence rule, so one click gives the operator a pre-filled Google
    Calendar page with the repeat already set; they press Save.

    Documented by Google as `action=TEMPLATE`. The parameters that matter:

      * `dates=<start>/<end>` in **UTC, basic format** — `20260906T133000Z`.
        Not local: the `ctz` parameter sets the display zone, and sending a
        local time without one is read as the *viewer's* zone.
      * `ctz` — the zone the times are shown in, so a phone in another country
        still shows 19:00 IST.
      * `recur=RRULE:...`, the same rule the .ics carries, so all three routes
        (link, email, file) produce the identical recurrence.

    No alarm: Google applies the calendar's own default notification, and there
    is no query parameter for a VALARM. The page says so rather than implying
    the "Alert" dropdown reached it.
    """
    from urllib.parse import urlencode

    now = now or datetime.now()
    first = next_occurrences(schedule, now, 1)
    if not first:
        raise ValueError("schedule produced no occurrences")
    start = first[0]
    end = start + timedelta(minutes=15)

    params = {
        "action": "TEMPLATE",
        "text": SUMMARY,
        "details": DESCRIPTION,
        "dates": f"{_utc_basic(start)}/{_utc_basic(end)}",
        "ctz": TZID,
        "recur": f"RRULE:{_rrule(schedule)}",
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def _utc_basic(local: datetime) -> str:
    """A naive Asia/Kolkata time as `YYYYMMDDTHHMMSSZ` in UTC.

    The offset is applied by hand rather than through `zoneinfo`: IST has been a
    fixed +05:30 since 1945 with no daylight saving, `TZID`/`_UTC_OFFSET` above
    already encode that for the .ics, and one definition of the offset is better
    than two that can disagree.
    """
    hours, minutes = int(_UTC_OFFSET[1:3]), int(_UTC_OFFSET[3:5])
    delta = timedelta(hours=hours, minutes=minutes)
    utc = local - delta if _UTC_OFFSET.startswith("+") else local + delta
    return utc.strftime("%Y%m%dT%H%M%SZ")
