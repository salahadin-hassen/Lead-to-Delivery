#!/usr/bin/env bash
# Smoke test of the REAL production image (built by `docker build`), run by
# .github/workflows/ci.yml. Usage: .github/ci/image-smoke.sh <image>
#
# Everything is throwaway: a private Docker network, a PostgreSQL container
# with a database created here, and security settings generated here (masked
# in the GitHub log). Containers and the network are removed on every exit.
# Requires: docker, curl, python3 (all present on GitHub's ubuntu runners).
set -euo pipefail

IMAGE=${1:?usage: image-smoke.sh <image>}
PG_IMAGE=postgres:16-bookworm@sha256:5264710be20f409750ac6dc4e5706dcef929c87e241605463f0f9aefa932a8d9
ID=ltdsmoke-$$
NET=$ID-net PG=$ID-pg API=$ID-api WORKER=$ID-worker
HOST_PORT=18000            # published on the runner's loopback only
BASE=http://127.0.0.1:$HOST_PORT
APP_HOST=ci.example.test

step() { printf '\n== %s\n' "$*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    for c in "$API" "$WORKER"; do
      docker logs "$c" >"$c.log" 2>&1 || true
      echo "--- last lines of $c ---"; tail -n 40 "$c.log" 2>/dev/null || true
    done
  fi
  docker rm -f "$API" "$WORKER" "$PG" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

psql_as() {  # psql_as <role> <sql>  (inside the PostgreSQL container)
  docker exec "$PG" psql -U "$1" -d ltd_smoke -XAtqv ON_ERROR_STOP=1 -c "$2"
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

# Throwaway values, generated for this run only.
MFA_KEY=$(python3 -c "import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())")
HOOK=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")
if [ -n "${GITHUB_ACTIONS:-}" ]; then echo "::add-mask::$MFA_KEY"; echo "::add-mask::$HOOK"; fi

OWNER_URL=postgresql://ltd_owner@$PG:5432/ltd_smoke
APP_URL=postgresql://ltd_app@$PG:5432/ltd_smoke
PROD=(-e APP_ENV=production -e "APP_BASE_URL=https://$APP_HOST")
SECRETS=(-e "ADMIN_MFA_ENCRYPTION_KEY=$MFA_KEY" -e "CRM_WEBHOOK_SECRET=$HOOK")

step "throwaway PostgreSQL with an isolated database and two roles"
docker network create "$NET" >/dev/null
docker run -d --name "$PG" --network "$NET" -e POSTGRES_HOST_AUTH_METHOD=trust "$PG_IMAGE" >/dev/null
for _ in $(seq 1 60); do docker exec "$PG" pg_isready -U postgres -q && break; sleep 1; done
docker exec "$PG" pg_isready -U postgres -q || fail "PostgreSQL did not start"
docker exec "$PG" psql -U postgres -XAtqv ON_ERROR_STOP=1 \
  -c "CREATE ROLE ltd_owner LOGIN" -c "CREATE ROLE ltd_app LOGIN CONNECTION LIMIT 20" \
  -c "CREATE DATABASE ltd_smoke OWNER ltd_owner"
docker exec "$PG" psql -U postgres -d ltd_smoke -XAtqv ON_ERROR_STOP=1 \
  -c "REVOKE ALL ON DATABASE ltd_smoke FROM PUBLIC" \
  -c "GRANT CONNECT ON DATABASE ltd_smoke TO ltd_owner, ltd_app" \
  -c "REVOKE CREATE ON SCHEMA public FROM PUBLIC" -c "ALTER SCHEMA public OWNER TO ltd_owner"

step "image facts: non-root, no Node, no sources/tests/secrets, runtime packages only"
inimg() { docker run --rm --network none --entrypoint sh "$IMAGE" -c "$1"; }
[ "$(inimg 'id -u')" = "10001" ] || fail "image does not run as UID 10001"
inimg '! command -v node && ! command -v npm' || fail "Node present in the runtime image"
inimg 'test -f /app/frontend/dist/index.html && ls /app/frontend/dist/assets/*.js >/dev/null' || fail "built frontend missing"
inimg 'test -f /app/backend/app/main.py && test -f /app/backend/sql/0006_worker_heartbeats.sql' || fail "application or migrations missing"
[ "$(inimg 'ls /app/backend/sql/*.sql | wc -l')" = "6" ] || fail "expected 6 migration files"
inimg 'for p in /app/frontend/src /app/frontend/node_modules /app/backend/tests /app/docs /app/n8n /app/var /app/.env /app/backend/.env; do test ! -e "$p" || exit 1; done' \
  || fail "unexpected files in the image"
inimg '! pip show pytest >/dev/null 2>&1' || fail "test dependencies in the runtime image"
# System CA certificates (/etc/ssl, /usr/share/ca-certificates) and Python's
# certifi bundle under /usr are public *.pem files and are excluded.
inimg '! find / -xdev \( -name ".env" -o -name "*.pem" -o -name "*.key" -o -name "*service-account*" -o -name "*.dump" -o -name "*.sqlite" \) -not -path "/proc/*" -not -path "/usr/*" -not -path "/etc/ssl/*" 2>/dev/null | grep .' \
  || fail "credential-like file in the image"
docker image inspect "$IMAGE" --format '{{json .Config.Env}}' | python3 -c "
import json, sys
names = {e.split('=', 1)[0] for e in json.load(sys.stdin)}
bad = names & {'DATABASE_URL', 'CRM_WEBHOOK_SECRET', 'ADMIN_MFA_ENCRYPTION_KEY', 'CLICKUP_API_TOKEN', 'SLACK_WEBHOOK_URL', 'GOOGLE_SERVICE_ACCOUNT_FILE'}
sys.exit(f'secret-bearing variables baked into the image: {sorted(bad)}' if bad else 0)"

step "production refuses to start without its security settings"
if docker run --rm --network "$NET" "${PROD[@]}" -e "DATABASE_URL=$APP_URL" "$IMAGE" \
     timeout 30 uvicorn app.main:app --port 8000 >refuse.log 2>&1; then
  fail "API started without ADMIN_MFA_ENCRYPTION_KEY / CRM_WEBHOOK_SECRET"
fi
grep -q "Insecure production configuration" refuse.log || fail "unexpected refusal output"

step "API with the image's DEFAULT command, port published to the runner"
docker run -d --name "$API" --network "$NET" -p "127.0.0.1:$HOST_PORT:8000" \
  "${PROD[@]}" "${SECRETS[@]}" -e "DATABASE_URL=$APP_URL" "$IMAGE" >/dev/null
for _ in $(seq 1 60); do [ "$(code "$BASE/api/health")" != "000" ] && break; sleep 1; done
[ "$(code "$BASE/api/health")" = "503" ] || fail "expected 503 before migrations (reachable from outside the container)"

step "API start-up did not migrate: the database is still empty"
[ "$(psql_as ltd_owner "select count(*) from information_schema.tables where table_schema = 'public'")" = "0" ] \
  || fail "tables exist before the explicit migration step"

step "explicit migration step (owner role) -> schema version 6"
docker run --rm --network "$NET" -e "DATABASE_URL=$OWNER_URL" "$IMAGE" python -m app.migrations
docker run --rm --network "$NET" -e "DATABASE_URL=$OWNER_URL" "$IMAGE" python -m app.migrations --status
[ "$(psql_as ltd_owner 'select max(version) from schema_migrations')" = "6" ] || fail "schema is not at version 6"

step "least-privilege app role (deploy/db-app-grants.sql verbatim, proven by deploy/db-app-check.sql)"
cat deploy/db-app-grants.sql deploy/db-app-check.sql \
  | docker exec -i "$PG" psql -U ltd_owner -d ltd_smoke -XAtqv ON_ERROR_STOP=1 -1 -f - || fail "ltd_app policy"
docker exec -i "$PG" psql -U ltd_app -d ltd_smoke -XAtqv ON_ERROR_STOP=1 -f - < deploy/db-app-check.sql \
  || fail "ltd_app policy check as ltd_app"
if psql_as ltd_app "create table ddl_probe (i int)" 2>/dev/null; then fail "ltd_app can create tables"; fi
if psql_as ltd_app "update project_templates set name = name" 2>/dev/null; then fail "ltd_app can write project_templates"; fi

step "API healthy; frontend, assets and security headers; host and docs rules"
for _ in $(seq 1 30); do [ "$(code "$BASE/api/health")" = "200" ] && break; sleep 1; done
[ "$(code "$BASE/api/health")" = "200" ] || fail "API not healthy after migration"
curl -fsS -H "Host: $APP_HOST" -D portal.headers -o portal.html "$BASE/portal"
grep -q '<div id="root">' portal.html || fail "portal page is not the built app"
grep -qi '^content-security-policy:' portal.headers || fail "CSP header missing"
grep -qi '^strict-transport-security:' portal.headers || fail "HSTS header missing"
grep -qi '^referrer-policy: no-referrer' portal.headers || fail "Referrer-Policy missing"
ASSET=$(grep -o '/assets/[^"]*\.js' portal.html | head -n 1)
[ -n "$ASSET" ] || fail "no script asset referenced"
[ "$(code -H "Host: $APP_HOST" "$BASE$ASSET")" = "200" ] || fail "asset $ASSET not served"
[ "$(code -H 'Host: evil.example' "$BASE/api/portal/me")" = "400" ] || fail "invalid host not rejected"
[ "$(code -H "Host: $APP_HOST" "$BASE/docs")" = "404" ] || fail "API docs exposed in production"
[ "$(code -H "Host: $APP_HOST" "$BASE/api/admin/me")" = "401" ] || fail "admin API reachable without a session"

step "worker from the same image"
docker run -d --name "$WORKER" --network "$NET" "${PROD[@]}" "${SECRETS[@]}" -e "DATABASE_URL=$APP_URL" \
  "$IMAGE" python -m app.jobs.worker >/dev/null
for _ in $(seq 1 45); do [ "$(code "$BASE/api/health/worker")" = "200" ] && break; sleep 1; done
[ "$(code "$BASE/api/health/worker")" = "200" ] || fail "worker heartbeat not seen"

step "graceful SIGTERM shutdown"
docker stop --time 30 "$WORKER" >/dev/null
[ "$(docker inspect "$WORKER" --format '{{.State.ExitCode}}')" = "0" ] || fail "worker exit code"
docker logs "$WORKER" 2>&1 | grep -q "signal SIGTERM received" || fail "worker did not handle SIGTERM"
docker logs "$WORKER" 2>&1 | grep -q "worker stopped" || fail "worker did not stop cleanly"
docker stop --time 30 "$API" >/dev/null
case "$(docker inspect "$API" --format '{{.State.ExitCode}}')" in 0|143) ;; *) fail "API exit code" ;; esac
docker logs "$API" 2>&1 | grep -q "Application shutdown complete" || fail "API did not shut down gracefully"

step "no generated secret appears in any log"
for c in "$API" "$WORKER"; do
  if docker logs "$c" 2>&1 | grep -qF -e "$MFA_KEY" -e "$HOOK"; then fail "secret value in $c log"; fi
done
if grep -qF -e "$MFA_KEY" -e "$HOOK" refuse.log; then fail "secret value in refusal log"; fi

echo; echo "IMAGE SMOKE TEST PASSED"
