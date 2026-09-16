"""The statement reminder. SPEC §24.

The interesting assertions are not "does it produce a file" but "does the file
say what the page said it would". A recurrence rule is easy to write and easy to
get subtly wrong, and the failure mode — a reminder that quietly stops arriving
in February — is exactly the silent kind this project keeps running into.

So the preview and the RRULE are checked against each other, and the ICS is
checked for the line discipline RFC 5545 actually requires rather than for
looking about right.
"""

from datetime import datetime, timezone

import pytest

from passbook import reminders


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


# --- the schedule ------------------------------------------------------------


def test_a_weekly_schedule_lands_on_the_chosen_weekday():
    schedule = reminders.Schedule(frequency="weekly", weekday=6, hour=19)
    upcoming = reminders.next_occurrences(schedule, at("2026-08-30T12:00"), 3)

    assert [m.strftime("%A") for m in upcoming] == ["Sunday"] * 3
    assert upcoming[0] == at("2026-08-30T19:00")  # later the same day
    assert [(b - a).days for a, b in zip(upcoming, upcoming[1:])] == [7, 7]


def test_an_occurrence_already_past_today_rolls_to_the_next_one():
    schedule = reminders.Schedule(frequency="weekly", weekday=6, hour=9)
    upcoming = reminders.next_occurrences(schedule, at("2026-08-30T12:00"), 1)
    assert upcoming[0] == at("2026-09-06T09:00"), "09:00 today has gone"


def test_fortnightly_steps_by_two_weeks():
    schedule = reminders.Schedule(frequency="fortnightly", weekday=0, hour=8)
    upcoming = reminders.next_occurrences(schedule, at("2026-08-30T12:00"), 3)
    assert [(b - a).days for a, b in zip(upcoming, upcoming[1:])] == [14, 14]


def test_monthly_never_skips_february():
    """`BYMONTHDAY=31` silently drops the months that have no 31st — RFC 5545
    omits a non-existent occurrence rather than clamping it, so a reminder set
    on the 31st simply does not arrive in February. Capped at 28 instead."""
    with pytest.raises(ValueError, match="day_of_month"):
        reminders.Schedule(frequency="monthly", day_of_month=31)

    schedule = reminders.Schedule(frequency="monthly", day_of_month=28, hour=9)
    upcoming = reminders.next_occurrences(schedule, at("2026-01-30T12:00"), 3)
    assert [m.date().isoformat() for m in upcoming] == [
        "2026-02-28",
        "2026-03-28",
        "2026-04-28",
    ]


@pytest.mark.parametrize(
    "field,value",
    [("hour", 24), ("minute", 60), ("weekday", 7), ("day_of_month", 0), ("lead_minutes", -1)],
)
def test_an_out_of_range_field_is_refused_not_clamped(field, value):
    """A silently clamped hour is a reminder firing at a time nobody chose."""
    with pytest.raises(ValueError, match=field):
        reminders.Schedule(**{field: value})


def test_an_unknown_frequency_is_refused():
    with pytest.raises(ValueError, match="frequency"):
        reminders.Schedule(frequency="hourly")


# --- the file ----------------------------------------------------------------


def test_the_ics_recurrence_matches_the_preview_the_page_showed():
    """The two must not be able to disagree: the list on screen is the promise,
    the RRULE is what is kept."""
    dateutil = pytest.importorskip("dateutil.rrule")
    icalendar = pytest.importorskip("icalendar")

    for schedule in (
        reminders.Schedule(frequency="weekly", weekday=6, hour=19),
        reminders.Schedule(frequency="fortnightly", weekday=0, hour=8, minute=30),
        reminders.Schedule(frequency="monthly", day_of_month=28, hour=9, minute=15),
    ):
        now = at("2026-08-30T12:00")
        event = next(
            part
            for part in icalendar.Calendar.from_ical(
                reminders.to_ics(schedule, now=now)
            ).walk()
            if part.name == "VEVENT"
        )
        rule = dateutil.rrulestr(
            event["RRULE"].to_ical().decode(),
            dtstart=event["DTSTART"].dt.replace(tzinfo=None),
        )
        assert list(rule[:4]) == reminders.next_occurrences(schedule, now, 4), schedule.label


def test_the_ics_folds_at_seventy_five_octets_and_uses_crlf():
    """RFC 5545 §3.1. The limit is bytes, not characters, and the description
    contains an em dash — splitting a UTF-8 sequence across a fold makes the
    file unparseable in some clients and subtly wrong in others."""
    text = reminders.to_ics(reminders.Schedule(), now=at("2026-08-30T12:00"))

    assert text.endswith("\r\n")
    assert "\n" not in text.replace("\r\n", "")
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, line
    # And it still round-trips to the text we wrote.
    icalendar = pytest.importorskip("icalendar")
    event = next(
        p for p in icalendar.Calendar.from_ical(text).walk() if p.name == "VEVENT"
    )
    assert "Review where the money went." in str(event["DESCRIPTION"])


def test_the_event_names_all_three_steps():
    """A reminder that says only "download" gets the file onto the disk and
    stops there — the half that produces no ledger and answers no question."""
    text = reminders.to_ics(reminders.Schedule(), now=at("2026-08-30T12:00"))
    unfolded = text.replace("\r\n ", "")
    for step in reminders.STEPS:
        assert step.rstrip(".") in unfolded
    assert "download" in reminders.SUMMARY.lower()
    assert "review" in reminders.SUMMARY.lower()


def test_the_file_carries_no_financial_data():
    """It ends up in Google Drive on import (§11, §24).

    **The uid is pinned, and that is load-bearing.** With the generated one this
    scan searched a random `uuid4()` hex string for `1111`, which four hex
    characters produce by chance about once in 4,200 runs — measured over
    400,000 draws. A privacy check that fails at random for a reason that is not
    a privacy failure is a check that gets rewritten as "flaky" and then
    believed the day it is right. The following test covers the uid separately,
    on the property that actually matters: it is generated, not derived.
    """
    schedule = reminders.Schedule(uid="fixed-for-the-scan@passbook.localhost")
    text = reminders.to_ics(schedule, now=at("2026-08-30T12:00")).lower()
    for leak in ("balance", "payee", "inr", "₹", "account number", "1111"):
        assert leak not in text, f"{leak!r} reached the calendar file"


def test_the_uid_is_random_and_carries_nothing_from_the_install():
    """The one part of the file this harness cannot scan for a literal.

    It must come from `uuid4()` and a constant domain — never from the account
    number, the customer id or the asset account name, any of which would put
    real data in a file destined for Drive.
    """
    import re

    a, b = reminders.Schedule().uid, reminders.Schedule().uid
    assert a != b, "a fixed uid would collide across installs"
    for uid in (a, b):
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
            r"@passbook\.local",
            uid,
        ), uid


def test_dtstamp_is_utc_and_dtstart_is_local():
    """Two clocks in one file. DTSTAMP is when it was written and RFC 5545
    requires UTC; DTSTART is local and carries a TZID."""
    text = reminders.to_ics(
        reminders.Schedule(hour=19),
        now=at("2026-08-30T12:00"),
        stamped=datetime(2026, 8, 30, 6, 30, tzinfo=timezone.utc),
    )
    assert "DTSTAMP:20260830T063000Z" in text
    assert "DTSTART;TZID=Asia/Kolkata:20260830T190000" in text
    assert "TZOFFSETTO:+0530" in text


# --- the config file ---------------------------------------------------------


def test_saving_keeps_the_uid_and_bumps_the_sequence(tmp_path):
    """A calendar ignores a re-import whose SEQUENCE has not moved, so an edited
    reminder would import as a visibly-successful no-op. The stable UID is the
    other half: it makes the re-import an update rather than a duplicate."""
    path = tmp_path / "reminder.yaml"
    first = reminders.Schedule()
    reminders.save(first, path)

    loaded = reminders.load(path)
    assert loaded.uid == first.uid
    assert loaded.sequence == 1

    loaded.hour = 8
    reminders.save(loaded, path)
    again = reminders.load(path)
    assert again.uid == first.uid, "the same calendar event, not a second one"
    assert again.sequence == 2
    assert again.hour == 8


def test_a_missing_file_is_a_default_not_an_error(tmp_path):
    schedule = reminders.load(tmp_path / "absent.yaml")
    assert schedule.enabled and schedule.frequency == "weekly"


def test_a_broken_file_is_refused_rather_than_guessed_around(tmp_path):
    path = tmp_path / "reminder.yaml"
    path.write_text("frequency: [not, a, string\n")
    with pytest.raises(ValueError, match="not readable YAML"):
        reminders.load(path)


def test_an_unknown_key_in_the_file_is_ignored_not_fatal(tmp_path):
    """The file is hand-editable; a stale key from an older version must not
    stop the reminder working."""
    path = tmp_path / "reminder.yaml"
    path.write_text("frequency: weekly\nhour: 7\nsomething_removed: yes\n")
    assert reminders.load(path).hour == 7


def test_suggest_uses_the_weekday_the_operator_actually_syncs_on():
    assert reminders.suggest(2).weekday == 2
    assert reminders.suggest(None).weekday == 6


# --- delivery by email -------------------------------------------------------


class FakeSettings:
    def __init__(self, **kw):
        self.passbook_smtp_host = kw.get("host", "smtp.example.com")
        self.passbook_smtp_port = kw.get("port", 587)
        self.passbook_smtp_user = kw.get("user", "me@example.com")
        self.passbook_smtp_password = kw.get("password", "app-password")
        self.passbook_reminder_email = kw.get("to")

    @property
    def reminder_recipient(self):
        return self.passbook_reminder_email or self.passbook_smtp_user

    @property
    def smtp_ready(self):
        return all(
            [
                self.passbook_smtp_host,
                self.passbook_smtp_user,
                self.passbook_smtp_password,
                self.reminder_recipient,
            ]
        )


class FakeSMTP:
    """Records what was sent. Never opens a socket (§10)."""

    sent: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        self.user = user

    def send_message(self, message):
        FakeSMTP.sent.append(message)


@pytest.fixture
def smtp(monkeypatch):
    import smtplib

    FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_the_invite_is_a_calendar_request_not_an_attachment(smtp):
    """Gmail renders the "Add to calendar" card only for an inline
    `text/calendar; method=REQUEST` part carrying an ORGANIZER and an ATTENDEE.
    Get any of the three wrong and it is shown as a file to download — which is
    the thing this feature exists to avoid."""
    reminders.send_invite(reminders.Schedule(), FakeSettings())

    (message,) = smtp.sent
    types = [part.get_content_type() for part in message.walk()]
    assert "text/calendar" in types

    calendar = next(p for p in message.walk() if p.get_content_type() == "text/calendar")
    assert calendar.get_param("method") == "REQUEST"
    body = calendar.get_content()
    assert "METHOD:REQUEST" in body
    assert "ORGANIZER" in body and "mailto:me@example.com" in body
    assert "ATTENDEE" in body
    assert "RRULE:FREQ=WEEKLY" in body


def test_it_goes_to_the_reminder_address_when_one_is_set(smtp):
    reminders.send_invite(reminders.Schedule(), FakeSettings(to="calendar@example.com"))
    assert smtp.sent[0]["To"] == "calendar@example.com"
    assert smtp.sent[0]["From"] == "me@example.com"


def test_sending_is_refused_with_instructions_when_nothing_is_configured(smtp):
    with pytest.raises(reminders.SendFailed, match="App Password"):
        reminders.send_invite(reminders.Schedule(), FakeSettings(host=None))
    assert smtp.sent == [], "nothing was sent"


def test_an_auth_failure_says_app_password_rather_than_wrong_password(smtp, monkeypatch):
    """Gmail rejects an account password with a 534. Telling someone to reset a
    password that was never the problem sends them the wrong way."""
    import smtplib

    def boom(self, user, password):
        raise smtplib.SMTPAuthenticationError(534, b"Application-specific password required")

    monkeypatch.setattr(FakeSMTP, "login", boom, raising=False)
    with pytest.raises(reminders.SendFailed, match="App Password"):
        reminders.send_invite(reminders.Schedule(), FakeSettings())


def test_the_password_never_reaches_the_message_or_an_error(smtp, monkeypatch):
    import smtplib

    def boom(self, user, password):
        raise smtplib.SMTPException("connection reset")

    monkeypatch.setattr(FakeSMTP, "login", boom, raising=False)
    try:
        reminders.send_invite(reminders.Schedule(), FakeSettings(password="s3cr3t-app-pw"))
    except reminders.SendFailed as exc:
        assert "s3cr3t-app-pw" not in str(exc)
    else:
        raise AssertionError("expected SendFailed")


def test_a_logged_address_is_masked():
    assert reminders._mask_email("someone@gmail.com") == "s***@gmail.com"
    assert "@" not in reminders._mask_email("nonsense")
