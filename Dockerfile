# syntax=docker/dockerfile:1
#
# Lead-to-Delivery production image: ONE image for both processes.
#
#   API (default):  docker run … ltd           -> uvicorn on 0.0.0.0:${PORT:-8000}
#   Worker:         docker run … ltd python -m app.jobs.worker
#   Migrations:     docker run … -e DATABASE_URL=<owner URL> ltd python -m app.migrations
#
# * Node is used only in the build stage; the runtime image contains Python,
#   the locked runtime dependencies, backend/app, backend/sql and the built
#   frontend (served by the API via FRONTEND_DIST_DIR).
# * No ARG is declared: some platforms pass every environment variable as a
#   build argument, and undeclared build arguments never reach the image.
#   No secret is read at build time.
# * Nothing runs migrations at startup (see docs/build-and-release.md).
# * Base images are pinned by version and by multi-platform index digest
#   (resolved 2026-11-11); update tag and digest together, deliberately.
#   Only linux/amd64 is the intended target.

# ---------------------------------------------------------------- frontend
FROM node:24.22.2-bookworm-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9 AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# Typecheck + production build (same command as local and CI).
RUN npm run build

# ----------------------------------------------------------------- runtime
FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LTD_DISABLE_DOTENV=1 \
    FRONTEND_DIST_DIR=/app/frontend/dist
WORKDIR /app/backend

# Locked, hash-verified, wheels only (no compilers in the image).
COPY backend/requirements.lock ./
RUN pip install --root-user-action=ignore --require-hashes --only-binary=:all: -r requirements.lock

# Application source and migrations, owned by root and read-only for the
# runtime user. The project itself is not pip-installed: uvicorn and
# `python -m` import it from this working directory.
COPY backend/app ./app
COPY backend/sql ./sql
COPY --from=frontend /build/frontend/dist /app/frontend/dist
RUN python -m compileall -q app \
 && useradd --system --uid 10001 --user-group --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin ltd

USER 10001:10001
EXPOSE 8000

# exec: uvicorn is PID 1 and receives SIGTERM directly (graceful shutdown).
# Proxy trust comes from FORWARDED_ALLOW_IPS (uvicorn's own variable; default
# 127.0.0.1). Set it to the platform's proxy range; never "*". Process count
# from WEB_CONCURRENCY (uvicorn's own variable; default 1).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --proxy-headers --no-server-header"]
