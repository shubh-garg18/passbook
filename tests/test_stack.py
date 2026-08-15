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


def _firefly_host_port() -> str:
    """The host port `make up` published Firefly on — 8080 unless `.env` moved
    it. Read rather than assumed: on WSL with `networkingMode=mirrored` the
    distro shares Windows's port space, and a Windows service on 8080 forces
    the move. Hardcoding it made this file fail against a working stack."""
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "FIREFLY_HOST_PORT" and value.strip():
                return value.strip()
    return "8080"


FIREFLY = f"http://127.0.0.1:{_firefly_host_port()}"


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


needs_caddy = pytest.mark.skipif(
    not _up(CADDY, "passbook.localhost"), reason="stack is down; run `make up`"
)
needs_ports = pytest.mark.skipif(not _up(WEB), reason="stack is down; run `make up`")
# Firefly gets its own gate rather than riding on the web UI's. Its HOST port is
# settable and the web UI's is not, so on a checkout whose `.env` names a
# different port — or has no `.env` at all, which is every fresh clone — the two
# are not the same question. Measured on a fresh clone beside a running stack:
# gated on the web UI, this failed with a socket timeout after six seconds
# instead of skipping, because something unrelated was holding 8080.
needs_firefly = pytest.mark.skipif(
    not _up(FIREFLY), reason=f"nothing answering on {FIREFLY}; run `make up`"
)


# --- routing by Host -------------------------------------------------------


@needs_caddy
def test_passbook_localhost_reaches_the_web_container():
    status, body = fetch(f"{CADDY}/api/session", host="passbook.localhost")
    assert status == 200
    payload = json.loads(body)
    # Shape unique to our API — Firefly has no such endpoint.
    assert {"authenticated", "configured", "stage"} <= set(payload)


@needs_caddy
def test_khata_localhost_reaches_firefly_not_the_web_container():
    status, body = fetch(f"{CADDY}/login", host="khata.localhost")
    assert status == 200
    assert "Firefly III" in body, "khata is not being routed to the app container"

    # And the passbook API is genuinely absent on that host — proof the two
    # routes point at different upstreams rather than both at `web`.
    status, _ = fetch(f"{CADDY}/api/session", host="khata.localhost")
    assert status == 404


@needs_caddy
def test_the_two_hosts_are_not_the_same_upstream():
    _, passbook = fetch(f"{CADDY}/", host="passbook.localhost")
    _, khata = fetch(f"{CADDY}/login", host="khata.localhost")
    assert "<title>passbook</title>" in passbook
    assert "Firefly III" in khata


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


@needs_firefly
def test_the_firefly_host_port_still_answers_directly():
    status, _ = fetch(f"{FIREFLY}/")
    # Firefly redirects an anonymous request to /login; either is "answering".
    assert status in (200, 302)


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
