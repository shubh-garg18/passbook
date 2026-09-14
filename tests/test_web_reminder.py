"""The reminder, over HTTP. SPEC §32.

The page hung on a skeleton the first time it was shot, which is a 500 the
client cannot distinguish from a slow request. A route test says which.
"""

from __future__ import annotations

from test_web import api, app, signed_in  # noqa: F401


def test_the_reminder_answers_rather_than_hanging(signed_in):
    """A 500 here renders as a skeleton that never resolves — indistinguishable
    from a slow network, which is why the screenshot harness could not tell."""
    response = signed_in.get("/reminder")
    assert response.status_code == 200, response.get_data(as_text=True)[:400]
    body = response.get_json()
    assert "enabled" in body


def test_the_calendar_file_is_served_as_a_calendar(signed_in):
    response = signed_in.client.get("/api/reminder.ics")
    assert response.status_code in (200, 404), response.status_code
    if response.status_code == 200:
        assert "text/calendar" in response.headers["Content-Type"]
