# passbook web UI. SPEC §16.3.
#
# The only image this project builds. Firefly and Postgres are pinned upstream
# images; this one has to exist because it runs our own code.
#
# Two stages. Node builds the React bundle and is then thrown away — the
# runtime image is a plain python:slim with no Node, no npm, and no build
# toolchain in it. The bundle is served by Flask from the same origin as the
# API, so there is no CORS configuration and no second port.

# ── stage 1: build the bundle ────────────────────────────────────────────────
FROM node:22-slim AS frontend

WORKDIR /build

# Lockfile layer first, so editing a component does not reinstall node_modules.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
# Vite writes to ../src/passbook/web/dist by config; inside this stage that
# resolves to /src/passbook/web/dist. Pinned explicitly so a config edit cannot
# silently drop the bundle somewhere the next stage does not copy from.
RUN npm run build && test -f /src/passbook/web/dist/index.html

# ── stage 2: the runtime image ───────────────────────────────────────────────
FROM python:3.12-slim

# uv is already how the project is managed; reuse it rather than introducing pip
# conventions that would drift from the lockfile.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer first, so editing source does not reinstall the world.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY --from=frontend /src/passbook/web/dist ./src/passbook/web/dist
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Runs unprivileged, and stays that way: no Docker socket, no backup
# passphrase, no rclone credentials. The status page lists the off-site
# archives read-only and says why. SPEC §15.3.
RUN useradd --uid 1000 --create-home passbook && chown -R passbook:passbook /app
USER passbook

EXPOSE 8081

# Threads=4: the SPA fires several API requests per page, and a parse holding
# the GIL should not stall the health check.
CMD ["waitress-serve", "--host=0.0.0.0", "--port=8081", "--threads=4", \
     "--call", "passbook.web:create_app"]
