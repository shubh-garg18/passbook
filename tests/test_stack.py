"""Live routing checks against the running stack. SPEC §17.4.

**A deliberate, narrow exception to "tests use fixtures, never the network".**
Everything else in this suite is hermetic and stays that way. Host-based
routing cannot be asserted from a file: `Caddyfile` says what was *intended*,
and `test_assets.py` pins that, but only a request proves Caddy parsed it,
resolved the upstreams over the compose network, and put each host on the
right one. Nothing here reaches beyond 127.0.0.1 and nothing here writes.

Every test auto-skips when the stack is down, so `make test` on a laptop with
containers stopped stays green and honest rather than red and ignored.

    make up && uv run pytest tests/test_stack.py -v
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

CADDY = "http://127.0.0.1:80"
WEB = "http://127.0.0.1:8081"
TIMEOUT = 6


def _database_host_port() -> str:
    """The host port `make up` published the database on — 5433 unless `.env`
    moved it. Read rather than assumed: a Postgres already installed on this
    machine forces the move, and hardcoding it made this file fail against a
    working stack."""
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "PASSBOOK_DB_PORT" and value.strip():
                return value.strip()
    return "5433"


def fetch(url: str, host: str | None = None) -> tuple[int, str]:
    """(status, body). Redirects are NOT followed — a redirect is itself the
    evidence for which upstream answered."""
    request = urllib.request.Request(url)
    if host:
        request.add_header("Host", host)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _up(url: str, host: str | None = None) -> bool:
    try:
        fetch(url, host)
        return True
    except Exception:
        return False


def _own_services() -> frozenset[str]:
    """Which of THIS checkout's containers are running.

    Not "is something listening on the port". Both repositories declare
    `name: passbook` in their compose file and both publish 80, 8081 and the
    database port, so on a machine holding a public clone and a private one
    only one install can hold each port — and a probe cannot tell which. These
    tests therefore ran against the *other* install's containers and passed,
    because it is the same product and the assertions held. The thing under
    test had never been started.

    What surfaced it was the one assertion the other install could not satisfy:
    its database answered the connect and then refused the credentials. That is
    the tell, and it only exists because one test opens the database; the six
    HTTP ones would have gone on passing indefinitely.

    Containers carry the working directory of the project that started them, so
    that is the question asked. Anything that stops it being answerable —
    docker absent, the daemon down, a permission error — reports nothing
    running, and every test here skips rather than asserting against whatever
    happens to hold the port.
    """
    import subprocess

    root = str(Path(__file__).resolve().parents[1])
    try:
        done = subprocess.run(
            [
                "docker", "ps",
                "--filter", f"label=com.docker.compose.project.working_dir={root}",
                "--format", '{{.Label "com.docker.compose.service"}}',
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    if done.returncode != 0:
        return frozenset()
    return frozenset(line.strip() for line in done.stdout.splitlines() if line.strip())


OURS = _own_services()
MINE = "this checkout's stack is not running; run `make up`"


needs_caddy = pytest.mark.skipif(
    "caddy" not in OURS or not _up(CADDY, "passbook.localhost"),
    reason=MINE,
)
needs_ports = pytest.mark.skipif("web" not in OURS or not _up(WEB), reason=MINE)


def _database_listening() -> bool:
    """A TCP connect, not a query: the CLI runs on the host and reaches the
    database on a published port, and this checks that the port is there.

    Its own gate rather than riding on the web UI's. The database's host port
    is settable and the web UI's is not, so on a checkout whose `.env` names a
    different port — or has no `.env` at all, which is every fresh clone — the
    two are not the same question.
    """
    import socket

    with socket.socket() as probe:
        probe.settimeout(2)
        return probe.connect_ex(("127.0.0.1", int(_database_host_port()))) == 0


needs_database = pytest.mark.skipif(
    "db" not in OURS or not _database_listening(),
    reason=f"{MINE} (database port {_database_host_port()})",
)


# --- routing by Host -------------------------------------------------------


@needs_caddy
def test_passbook_localhost_reaches_the_web_container():
    status, body = fetch(f"{CADDY}/api/session", host="passbook.localhost")
    assert status == 200
    payload = json.loads(body)
    # Shape unique to our API — the ledger has no such endpoint.
    assert {"authenticated", "configured", "stage"} <= set(payload)


@needs_caddy
def test_there_is_exactly_one_host_and_the_other_is_gone():
    """A second hostname used to serve a separate ledger application beside
    this one. It is gone, and so is that application — so the second host has
    to answer like any other unknown name, not like a door left ajar.

    Asserted on the fallback rather than on the page a host serves. `:80` and
    `passbook.localhost` are not namespaced by checkout, so on a machine
    running a passbook from somewhere else this file is talking to *that*
    stack — and a test that asserted on its page content would fail for a
    reason that has nothing to do with this one.
    """
    status, body = fetch(f"{CADDY}/", host="khata.localhost")
    assert status == 404
    assert "passbook.localhost" in body

    status, page = fetch(f"{CADDY}/", host="passbook.localhost")
    assert status == 200
    assert '<div id="root">' in page, "the SPA shell is what this host serves"


@needs_caddy
def test_an_unknown_host_gets_a_plain_answer():
    status, body = fetch(f"{CADDY}/", host="nope.localhost")
    assert status == 404
    assert "passbook.localhost" in body, "the fallback should name the real hosts"


@needs_caddy
def test_caddy_is_not_serving_https():
    """`auto_https off`. If Caddy ever provisions a certificate it also starts
    redirecting :80 to :443, which would break every URL in the runbook."""
    status, _ = fetch(f"{CADDY}/api/session", host="passbook.localhost")
    assert status != 308, "Caddy is redirecting to HTTPS — auto_https is back on"


# --- the numbered ports still answer ---------------------------------------
# The runbook, the DR drill and every healthcheck use these, and they run when
# Caddy may not be up. Caddy is an addition, never a replacement.


@needs_ports
def test_port_8081_still_answers_directly():
    status, body = fetch(f"{WEB}/api/session")
    assert status == 200
    assert "configured" in json.loads(body)


@needs_database
@pytest.mark.live_ledger
def test_the_database_port_is_published_for_the_cli():
    """`passbook sync`, `verify-ledger` and `upgrade` all run on the HOST while
    the web container reaches the same database over the compose network. If
    this port stops being published, every one of them stops working and the
    UI keeps going — which is the confusing half of that failure."""
    from passbook.config import load_settings
    from passbook.store import open_ledger

    with open_ledger(load_settings()) as store:
        assert isinstance(store.asset_accounts(), list)


@needs_ports
def test_the_manifest_is_manifest_json_over_the_real_server():
    """The Flask test client and waitress can disagree about headers, so this
    asserts the type on the wire rather than in-process."""
    request = urllib.request.Request(f"{WEB}/manifest.webmanifest")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        assert response.headers["Content-Type"].startswith("application/manifest+json")
        assert json.loads(response.read())["display"] == "standalone"


@needs_caddy
def test_the_manifest_survives_the_proxy():
    """Caddy must not rewrite the content type on the way through — the same
    silent failure, one hop later."""
    request = urllib.request.Request(f"{CADDY}/manifest.webmanifest")
    request.add_header("Host", "passbook.localhost")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        assert response.headers["Content-Type"].startswith("application/manifest+json")
