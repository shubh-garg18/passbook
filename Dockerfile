# passbook web UI.
#
# The only image this project builds. Postgres and Caddy are pinned upstream
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

# `pg_dump`, so a backup can be taken from the UI. SPEC §31.
#
# This does NOT give the container the Docker socket — §15.1/§15.3 still hold
# and it still cannot see or control another container. It reaches Postgres the
# ordinary way, over the compose network.
#
# ── The version is pinned to the SERVER's, and that is not fussiness ────────
# Debian trixie ships postgresql-client 17. Its pg_dump happily dumps a 16
# server — and emits output that 16 cannot read back:
#
#     ERROR:  unrecognized configuration parameter "transaction_timeout"
#
# plus psql 17's \restrict / \unrestrict meta-commands. `make restore` runs
# psql with ON_ERROR_STOP=1, so the restore aborts on the first line. Measured
# against a scratch database, not reasoned about.
#
# A backup that cannot be restored is not a backup, and this one would have
# looked perfect until the day it was needed. So the client comes from PGDG at
# the server's own major version. **Bump PG_MAJOR with the `db` image.**
ARG PG_MAJOR=16
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl ca-certificates gnupg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install --no-install-recommends -y "postgresql-client-${PG_MAJOR}" \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

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
