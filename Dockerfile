# syntax=docker/dockerfile:1
# Base image pinned by digest (python:3.12-slim-bookworm, 2026-10-06)
ARG BASE_REGISTRY=docker.io/library
FROM ${BASE_REGISTRY}/python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

ENV PYTHONDONTWRITEBYTECODE=1 \
    LOGURU_DIAGNOSE=NO \
    LOGURU_BACKTRACE=NO \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    PORT=8080 \
    USAGE_FILE=/data/usage.json

WORKDIR /app

# Dependencies are installed only if every hash in requirements.txt matches
COPY requirements.txt .
RUN pip install --require-hashes --no-deps -r requirements.txt

COPY src/ ./src/
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh

# Unprivileged user; code is read-only for it, only /data (usage counter) is writable.
# The entrypoint starts as root only to chown the mounted volume, then drops all privileges.
RUN groupadd --system --gid 10001 mcp \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin mcp \
    && mkdir -p /data && chown mcp:mcp /data \
    && python -m compileall -q src

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
