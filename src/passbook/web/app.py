"""App factory. SPEC §16.1.

Flask does two things here and nothing else: it serves the JSON API under
`/api/*`, and it serves the built React bundle for everything else. There are no
templates left — the Jinja pages became components, but the *routes* they
implemented moved wholesale into `api.py`, which still delegates to
`service.py`. Same parser, same push path, same balance invariant.

**Single origin, single container.** The bundle is built by a Node stage in the
Dockerfile and copied in as static files; there is no Node at runtime, no dev
server, and no CORS configuration to get wrong. A cookie set here is
same-origin by construction.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, g, jsonify, request, send_from_directory

from ..config import load_settings
from ..webauth import WEB_AUTH_FILE, migrate_from_env, save
from . import auth as A
from .api import MAX_UPLOAD_BYTES, api

log = logging.getLogger(__name__)

# Written here by the Docker build's Node stage. Absent in a source checkout,
# which is fine: the API is fully usable (and fully tested) without it.
DIST = Path(__file__).parent / "dist"


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    settings = load_settings()

    app.config.update(
        SECRET_KEY=settings.passbook_web_secret or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        INBOX=Path("inbox"),
        ARCHIVE=Path("archive"),
        SESSION_COOKIE_HTTPONLY=True,
        # Strict, not Lax. Lax still sends the cookie on a top-level GET
        # navigation from another site, which is enough for a drive-by link to
        # act as the signed-in operator against any state-changing GET. There
        # are none today, and Strict means there cannot be one by accident.
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_NAME="pb_session",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        # http on 127.0.0.1 today, so Secure stays off — a Secure cookie is
        # simply never sent over http, which would present as "sign-in does
        # nothing". Phase 8 (Tailscale, with a real certificate) sets this to 1.
        SECURE_COOKIES=os.environ.get("PASSBOOK_WEB_SECURE_COOKIES", "0") == "1",
        # Tests inject a WebAuth here; None means "read config/web-auth.json".
        WEB_AUTH_FIXED=None,
    )
    if config:
        app.config.update(config)
    app.config["SESSION_COOKIE_SECURE"] = app.config["SECURE_COOKIES"]

    _migrate_credentials(app)

    app.register_blueprint(api)

    @app.before_request
    def _csrf():
        if request.path.startswith("/api/") and not A.csrf_ok():
            log.warning("CSRF check failed for %s %s", request.method, request.path)
            return jsonify({"error": "Stale session. Reload the page.", "code": "csrf"}), 403
        return None

    @app.after_request
    def _issue_csrf(response):
        # Readable by JS on purpose: the client echoes it back in a header, and
        # a value the client cannot read cannot be echoed. The session cookie
        # next to it stays httpOnly, which is the one that matters.
        token = g.get("csrf_token") or A.issue_csrf()
        if request.cookies.get(A.CSRF_COOKIE) != token:
            response.set_cookie(
                A.CSRF_COOKIE,
                token,
                httponly=False,
                samesite="Strict",
                secure=app.config["SECURE_COOKIES"],
                path="/",
            )
        return response

    @app.errorhandler(413)
    def _too_large(_):
        return (
            jsonify(
                {
                    "error": f"File is larger than {MAX_UPLOAD_BYTES // 1024 // 1024} MB.",
                    "code": "too_large",
                }
            ),
            413,
        )

    # --- the bundle -------------------------------------------------------

    @app.get("/assets/<path:filename>")
    def assets(filename: str):
        # Content-addressed filenames from Vite, so they can be cached hard.
        response = send_from_directory(DIST / "assets", filename)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.get("/", defaults={"path": ""})
    @app.get("/<path:path>")
    def spa(path: str):
        """Everything that is not the API is the single-page app.

        Routing lives in the client, so a deep link like /payees must return
        index.html rather than 404 — the client reads the URL and renders the
        right page.
        """
        if path.startswith("api/"):
            return jsonify({"error": "No such endpoint.", "code": "not_found"}), 404

        candidate = DIST / path
        if path and candidate.is_file():
            response = send_from_directory(DIST, path)
            if path.endswith(".webmanifest"):
                # Belt and braces, and honestly labelled as such: CPython has
                # known `.webmanifest` since 3.11, so `mimetypes` gets this
                # right on every interpreter this project supports, container
                # included (measured — python:3.12-slim ships no
                # /etc/mime.types and still guesses correctly).
                #
                # It is set explicitly anyway because the failure it prevents
                # is silent: served as anything else, Chromium ignores the
                # manifest with no console error and no warning, and the only
                # symptom is that "Install" never appears. One line to make
                # that independent of the interpreter's table.
                response.mimetype = "application/manifest+json"
            return response

        index = DIST / "index.html"
        if not index.is_file():
            return (
                "<h1>passbook</h1><p>The web bundle has not been built. "
                "Run <code>make web-build</code>, or <code>docker compose build web</code> "
                "which does it in a Node stage.</p>",
                503,
            )
        response = send_from_directory(DIST, "index.html")
        # index.html names the hashed asset files, so it must never be cached.
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


def _migrate_credentials(app: Flask) -> None:
    """Carry a Phase 7/9 credential forward so an upgrade is not a lockout.

    Password only — TOTP cannot be migrated because it never existed. The first
    sign-in after upgrading therefore lands on mandatory enrolment, which is the
    intended path, not a failure.
    """
    if app.config.get("WEB_AUTH_FIXED") is not None:
        return
    try:
        migrated = migrate_from_env()
    except Exception as exc:  # never let a migration stop the app booting
        log.warning("credential migration skipped: %s", exc)
        return
    if migrated is None:
        return
    save(migrated)
    log.info("migrated web credentials into %s — TOTP enrolment is required", WEB_AUTH_FILE)
