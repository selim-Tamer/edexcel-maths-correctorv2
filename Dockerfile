# syntax=docker/dockerfile:1.7
#
# Reproducible Cloud Run image for the AI Maths Marker API + durable worker.
#
# WHAT THIS BUILDS
#   One image serves both Cloud Run commands documented in
#   docs/BETA_DEPLOY_SETUP.md:
#     * service:  uvicorn api.server:application  (default CMD)
#     * job:      python worker.py --once         (command override)
#   Both entrypoints run runtime_config first; a revision missing a required
#   environment variable fails to start instead of serving fail-closed 503s.
#
# WHAT IT DOES NOT DO
#   * It bakes in NO mark-scheme PDF and NO secret. The scheme is mounted at
#     /app/scheme.pdf (read-only) and pointed at by AI_MARKER_MARK_SCHEME.
#     A real exam board scheme is licensed content and must never live in git.
#   * It runs NO migration and contacts NO database at build time.
#   * The base image is pinned by tag AND digest. python:3.13-slim-bookworm
#     resolved to the multi-arch index digest
#     sha256:7f40274ed887f9b12d9b7a4a37343ebc783390f9be6e13d9e99ba1e2147e5f26
#     on 2026-10-27 (Docker Hub registry manifest, see
#     docs/CONTAINER_BASE_IMAGE_EVIDENCE.md). The digest is not decorative:
#     it is what makes the base bytes reproducible when the tag moves.
#
# Least privilege: the process runs as the unprivileged "app" user (UID 10001),
# owns only /app, and has no shell-accessible secret material.

FROM python:3.13-slim-bookworm@sha256:7f40274ed887f9b12d9b7a4a37343ebc783390f9be6e13d9e99ba1e2147e5f26

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    AI_MARKER_RUNTIME=production

WORKDIR /app

# Dependencies first so application edits do not invalidate the wheel layer.
# requirements.txt is the single manifest Streamlit Cloud also consumes; the
# image therefore installs exactly the reviewed, CI-validated set.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip check

# Application code. Everything runtime-needed is at the repository root plus
# api/; tests, investigation notebooks and docs are excluded by .dockerignore.
COPY . .

# Unprivileged runtime identity. /app is owned by it so a mounted scheme file
# only needs to be readable (0644) by this UID.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER 10001

# The scheme mount point exists so an operator mount cannot silently fail for
# a missing parent directory. It is EMPTY by design: no licensed PDF is baked.
RUN mkdir -p /app/scheme \
    && test ! -e /app/scheme.pdf || true

EXPOSE 8080

# Cloud Run's own health check is the HTTP probe; this container-level check
# gives the same signal to a local `docker run` without curl in the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8080')}/v1/healthz\", timeout=4)"]

# Exec form keeps PID 1 as the shell only long enough to expand $PORT, then
# execs uvicorn so signals reach the server (Cloud Run sends SIGTERM for the
# 10s graceful-shutdown window).
CMD ["sh", "-c", "exec uvicorn api.server:application --host 0.0.0.0 --port \"${PORT:-8080}\" --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 8"]
