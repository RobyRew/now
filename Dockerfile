# syntax=docker/dockerfile:1.10
# ─────────────────────────────────────────────────────────────────────────────
# Admin page — Node 26 builds the static Astro page (type check included).
# ─────────────────────────────────────────────────────────────────────────────
FROM node:26-alpine AS admin
WORKDIR /admin

COPY admin/package.json admin/package-lock.json ./
# --ignore-scripts: nothing here needs an install script, and with scripts npm
# "prepares" the git dependency @robyrew/web-config.
RUN npm ci --ignore-scripts --no-audit --no-fund

COPY admin/ ./
RUN npm run build

# ─────────────────────────────────────────────────────────────────────────────
# Runtime — Python 3.15 on Debian trixie, one uvicorn worker, non-root.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.15-slim-trixie AS runtime
WORKDIR /app

# XDG_CACHE_HOME: SpotAPI keeps its persisted-query hash cache there, and the app user has no home directory.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    STATIC_DIR=/app/admin/dist \
    PORT=8080 \
    XDG_CACHE_HOME=/data/cache

RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

# Every package pinned with its hashes; wheels only (all of them ship one for
# CPython 3.15 on x86_64), so nothing is compiled here.
COPY requirements.txt ./
RUN pip install --require-hashes --only-binary=:all: --no-deps -r requirements.txt

COPY now/ ./now/
COPY --from=admin /admin/dist ./admin/dist

# The sealed sessions and the last plays live here. Mount the named volume
# now-spotify-data on this path in Dokploy, or every redeploy starts with no accounts.
RUN mkdir -p /data && chown app:app /data

USER app
EXPOSE 8080

# No exec healthcheck on vps01: exec probes leaked pids and kernel memory there
# (2026-09). The host's site probe calls /healthz instead.
HEALTHCHECK NONE

CMD ["python", "-m", "now"]
