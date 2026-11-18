# syntax=docker/dockerfile:1

# Teleminder production image.
# Python 3.12 slim base, non-root runtime user, SQLite on a named volume.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install frozen dependencies first for better layer caching.
# requirements.lock is exported from uv.lock with hashes.
COPY requirements.lock ./requirements.lock
RUN pip install --require-hashes -r requirements.lock

# Install the application itself without re-resolving dependencies.
COPY pyproject.toml ./pyproject.toml
COPY teleminder ./teleminder
RUN pip install --no-deps .

# Non-root runtime user and data directory for SQLite.
RUN groupadd --system --gid 10001 teleminder \
    && useradd --system --uid 10001 --gid teleminder --no-create-home --home-dir /app teleminder \
    && mkdir -p /data \
    && chown teleminder:teleminder /data

USER teleminder

# SQLite database lives on a named volume mounted at /data.
ENV DATABASE_PATH=/data/teleminder.db \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8765 \
    TIMEZONE=America/Toronto

EXPOSE 8765

VOLUME ["/data"]

# Health requires a fresh worker heartbeat, a reachable SQLite database,
# and a local HTTP /healthz probe (all implemented inside `teleminder health`).
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["teleminder", "health"]

# Default: run both the Telegram worker and the MCP HTTP endpoint.
# Use `teleminder stdio` for MCP-only stdio transport (no Telegram worker).
CMD ["teleminder", "serve"]
