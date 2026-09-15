"""What version this install is, and whether a newer one exists.

**The web container cannot update itself, and that is a decision rather than a
limitation.** Applying an update means `git pull` and rebuilding an image,
which needs the Docker socket — and the one container that listens on a port
and parses uploaded files is the last place that socket may ever appear. A web
compromise would become a host compromise, plus the power to delete every
backup including the off-site copies. `test_ops_only_ever_executes_rclone`
asserts that at AST level and this module does not change it.

So the split is: **the app detects and explains; one command applies.** The
page tells you a newer version exists, what changed in it, and the exact thing
to run — which on Windows is a double-click. Nothing here executes anything.

Checking is **optional, cached and offline-tolerant**. It asks GitHub's public
API, which needs no account and no token; a machine with no internet gets
"could not check" rather than an error page, and the answer is held for a few
hours so opening the app ten times does not ask ten times.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: The public repository this is released from. A public repo needs no token:
#: 60 requests an hour unauthenticated, against a check that runs at most once
#: every few hours.
REPO = "shubh-garg18/passbook"
API = f"https://api.github.com/repos/{REPO}"

#: How long an answer is kept. Long enough that opening the app repeatedly
#: costs one request; short enough that an update is noticed the same day.
CACHE_SECONDS = 6 * 60 * 60

#: Where the answer is kept, so a restart does not re-ask.
CACHE = Path("config/.update-check.json")

TIMEOUT = 6

_MEMO: dict[str, object] = {}


@dataclass
class Release:
    """What this checkout is, and what is published.

    `behind` is **tri-state on purpose**: `None` means the check could not be
    made, which is not the same as "up to date" and must never be rendered as
    one. Non-negotiable 11, applied to a version number.
    """

    current: str = ""
    current_date: str = ""
    latest: str = ""
    latest_date: str = ""
    behind: bool | None = None
    commits: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.behind is False


def _git(*args: str) -> str:
    """Run one read-only git command in the checkout, or return ""."""
    try:
        done = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


#: Written by `make up` and `make reload`, and mounted into the web container
#: with the rest of `config/`. The container has no `.git` — the image holds a
#: copy of the source, not a repository — so this is how the running app knows
#: which commit it was built from.
STAMP = Path("config/.version")


def stamp(commit: str, when: str) -> None:
    """Record what is being started. Called by the Makefile, never by a route."""
    try:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text(f"{commit} {when}\n", encoding="utf-8")
    except OSError:
        log.debug("could not stamp the version", exc_info=True)


def installed() -> tuple[str, str]:
    """`(commit, date)` for what is running, or `("", "")`.

    **Two sources, and the order matters.** Git first, because on the host it
    is the authority on what this tree is. The stamp second, for the web
    container, which has no repository in it.

    The stamp is not a worse answer there — it is the better one. It records
    the commit the image was *built* from, and an operator who pulled without
    rebuilding is still running the old code. "What is running" is the question
    being asked, and the stamp is the only thing that answers it honestly.

    A ZIP download has neither, and that is reported rather than hidden: the
    caller says a clone is what makes updating one command.
    """
    commit, when = _git("rev-parse", "HEAD"), _git("log", "-1", "--format=%cs")
    if commit:
        return commit, when
    try:
        parts = STAMP.read_text(encoding="utf-8").split()
    except OSError:
        return "", ""
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def _fetch(url: str) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            # GitHub asks for one, and an anonymous request without it is more
            # likely to be throttled.
            "User-Agent": f"passbook ({REPO})",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _cached() -> dict | None:
    try:
        held = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - float(held.get("at") or 0) > CACHE_SECONDS:
        return None
    return held


def _remember(payload: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({**payload, "at": time.time()}), encoding="utf-8")
    except OSError:
        # A read-only config directory is not a reason to fail a page.
        log.debug("could not cache the update check", exc_info=True)


def check(force: bool = False) -> Release:
    """Is there a newer passbook? Never raises.

    Every failure — no network, no git, GitHub throttling, a ZIP download with
    no history — comes back as `behind=None` and a sentence saying which. The
    page renders that as *unchecked*, in amber, never as a tick.
    """
    current, current_date = installed()
    release = Release(current=current, current_date=current_date)
    if not current:
        release.error = (
            "this is not a git checkout, so there is nothing to compare against — "
            "`git clone` instead of a ZIP download makes updating one command"
        )
        return release

    held = None if force else _cached()
    if held is None:
        try:
            head = _fetch(f"{API}/commits/main")
            held = {
                "latest": str(head.get("sha") or ""),
                "latest_date": str(
                    (head.get("commit") or {}).get("committer", {}).get("date") or ""
                )[:10],
            }
            _remember(held)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            release.error = f"could not reach GitHub: {exc}"
            return release

    release.latest = str(held.get("latest") or "")
    release.latest_date = str(held.get("latest_date") or "")
    if not release.latest:
        release.error = "GitHub did not say what the latest version is"
        return release

    release.behind = release.latest != release.current
    if release.behind:
        release.commits = _changes(current)
    return release


def _changes(since: str) -> list[dict]:
    """What landed since this checkout, newest first. Best-effort.

    Shown so "there is an update" is a sentence with content in it rather than
    a nag. An empty list means the comparison could not be made — the checkout
    is ahead, or on a branch of its own — which is reported as no list rather
    than as no changes.
    """
    try:
        compared = _fetch(f"{API}/compare/{since}...main")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []
    out = []
    for entry in reversed((compared or {}).get("commits") or []):
        message = str((entry.get("commit") or {}).get("message") or "")
        out.append(
            {
                "sha": str(entry.get("sha") or "")[:7],
                # The subject only. A body is for `git log`, not for a card.
                "title": message.splitlines()[0] if message else "",
                "date": str(
                    (entry.get("commit") or {}).get("committer", {}).get("date") or ""
                )[:10],
            }
        )
    return out[:20]
