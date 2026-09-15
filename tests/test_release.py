"""Whether a newer passbook exists, and the promise that asking never breaks.

Nothing here reaches the network: `_fetch` is replaced, because a test that
depends on GitHub answering is a test that fails on a train.
"""

from __future__ import annotations

import pytest

from passbook import release


@pytest.fixture(autouse=True)
def _no_cache(tmp_path, monkeypatch):
    """The cache is a file in `config/`. A test that shared the operator's
    would be a test whose result depended on when it last ran."""
    monkeypatch.setattr(release, "CACHE", tmp_path / "check.json")
    monkeypatch.setattr(release, "STAMP", tmp_path / "version")


def test_a_checkout_at_the_published_commit_is_not_behind(monkeypatch):
    monkeypatch.setattr(release, "installed", lambda: ("abc123", "2026-09-15"))
    monkeypatch.setattr(release, "_fetch", lambda url: {
        "sha": "abc123", "commit": {"committer": {"date": "2026-09-15T00:00:00Z"}},
    })
    found = release.check()
    assert found.behind is False
    assert found.ok


def test_a_checkout_behind_says_what_changed(monkeypatch):
    monkeypatch.setattr(release, "installed", lambda: ("old", "2026-09-01"))

    def fetch(url):
        if "/compare/" in url:
            return {"commits": [
                {"sha": "new0000", "commit": {
                    "message": "Make the ledger fast\n\nbody ignored",
                    "committer": {"date": "2026-09-15T00:00:00Z"}}},
            ]}
        return {"sha": "new0000", "commit": {"committer": {"date": "2026-09-15T00:00:00Z"}}}

    monkeypatch.setattr(release, "_fetch", fetch)
    found = release.check()
    assert found.behind is True
    assert not found.ok
    # The subject only. A commit body is for `git log`, not for a card.
    assert found.commits[0]["title"] == "Make the ledger fast"
    assert found.commits[0]["sha"] == "new0000"


def test_no_network_is_UNCHECKED_never_up_to_date(monkeypatch):
    """Tri-state, applied to a version number. `behind=None` is "could not
    ask", which the page paints amber — a tick for something never checked is
    the thing this project exists to not do."""
    import urllib.error

    monkeypatch.setattr(release, "installed", lambda: ("abc123", "2026-09-15"))
    monkeypatch.setattr(release, "_fetch", lambda url: (_ for _ in ()).throw(
        urllib.error.URLError("no route to host")))
    found = release.check()
    assert found.behind is None
    assert not found.ok
    assert "could not reach GitHub" in found.error


def test_a_zip_download_says_so_rather_than_failing(monkeypatch):
    """No `.git`, no stamp, no comparison — and the message names the fix."""
    monkeypatch.setattr(release, "_git", lambda *a: "")
    found = release.check()
    assert found.behind is None
    assert "git clone" in found.error


def test_the_answer_is_cached_so_ten_page_loads_are_one_request(monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        return {"sha": "abc123", "commit": {"committer": {"date": "2026-09-15T00:00:00Z"}}}

    monkeypatch.setattr(release, "installed", lambda: ("abc123", "2026-09-15"))
    monkeypatch.setattr(release, "_fetch", fetch)
    for _ in range(5):
        release.check()
    assert len(calls) == 1

    release.check(force=True)
    assert len(calls) == 2, "asking again on purpose still asks"


def test_the_container_reads_the_stamp_when_there_is_no_git(monkeypatch):
    """The image holds a copy of the source, not a repository. The stamp is how
    the running app knows which commit it was built from — and it is the better
    answer there: an operator who pulled without rebuilding is still running
    the old code."""
    monkeypatch.setattr(release, "_git", lambda *a: "")
    release.stamp("deadbee", "2026-09-15")
    assert release.installed() == ("deadbee", "2026-09-15")
