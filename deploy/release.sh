#!/usr/bin/env bash
# Release step for one deployment: build the frontend, apply migrations as the
# schema owner, then restart the API and worker. Run from the repository root
# on the server, at the commit being deployed.
#
#   MIGRATION_DATABASE_URL  ltd_owner connection (from the secret manager)
#   DATABASE_URL            ltd_app connection, used by the services
#
# Migrations are forward-only and run under an advisory lock; a failed
# migration rolls back and stops the release before the services restart.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${MIGRATION_DATABASE_URL:?set MIGRATION_DATABASE_URL (ltd_owner) for the release step}"

echo "release: $(git rev-parse --short HEAD)"
git diff --quiet && git diff --cached --quiet || { echo "refusing: uncommitted changes" >&2; exit 1; }

(cd frontend && npm ci --no-audit --no-fund && npm run build)
(cd backend && python3.12 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q .)
(cd backend && DATABASE_URL="$MIGRATION_DATABASE_URL" .venv/bin/python -m app.migrations)

if command -v systemctl >/dev/null && systemctl list-unit-files ltd-api.service >/dev/null 2>&1; then
  sudo systemctl restart ltd-api.service ltd-worker.service
  sleep 3
  systemctl is-active ltd-api.service ltd-worker.service
fi
echo "release done: $(git rev-parse HEAD)"
