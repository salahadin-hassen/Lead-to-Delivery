# Deployment

How to deploy one agency's instance of Lead-to-Delivery. Written from the
actual application; nothing here is copied from another project's runbook.

**Status (2026-11-10): not deployed.** No hosting account or provider tooling
was available, so this is a reproducible plan exercised against a local
staging environment (see "What was verified"), not a record of a live
deployment.

## Shape of a deployment

One agency = one deployment = one database. This is **not** a multi-tenant
service: nothing in the schema separates agencies.

| Component | What runs | Notes |
|---|---|---|
| Reverse proxy | HTTPS termination (e.g. Caddy, or the platform's load balancer) | Forwards everything to the API on loopback/private network. |
| API | `uvicorn app.main:app` | Serves `/api/*` **and** the built React app (`FRONTEND_DIST_DIR`) at `/`, `/admin`, `/portal`, so the app, client portal and API share one origin. |
| Worker | `python -m app.jobs.worker` | Separate process/service. Several may run; claiming is `FOR UPDATE SKIP LOCKED` with fenced results. |
| PostgreSQL 16 | Managed or self-hosted | Dedicated database; never shared with `ltd`, `ltd_test` or another project. |
| Release step | `python -m app.migrations` as the schema owner | Run before starting new code. |

No Redis, Celery, Docker or object storage is needed. The local n8n demo and
its relay (`backend/app/demo_relay.py`) are **never** deployed.

## 1. Database

Use a dedicated PostgreSQL 16 database (TLS required on a managed provider:
add `?sslmode=require` to the URLs). Use the **direct** (session) connection
URL, not a transaction-mode pooler (PgBouncer, Neon's pooled URL): the pool
sets a session `statement_timeout`, psycopg prepares statements, and
migrations take a session advisory lock. Create two roles with
[`deploy/db-roles.sql`](../deploy/db-roles.sql):

- `ltd_owner` owns the schema and runs migrations (release step only);
- `ltd_app` is what the API and worker use: read/write data, no DDL.

Then, as `ltd_owner`, apply migrations once and run the `GRANT` block at the
end of that file. Default privileges then cover later migrations.

```bash
cd backend
DATABASE_URL="$MIGRATION_DATABASE_URL" .venv/bin/python -m app.migrations          # apply
DATABASE_URL="$MIGRATION_DATABASE_URL" .venv/bin/python -m app.migrations --status # verify: no PENDING
```

Migrations are forward-only, transactional and advisory-locked (two
concurrent release steps cannot both apply). The API never migrates at
startup; `/api/health` returns 503 until the schema matches the code. Never
seed production with demo data.

## 2. Configuration and secrets

All settings are environment variables; names and placeholders are in
[`.env.example`](../.env.example). Put secrets in the platform's secret
manager (or `/etc/lead-to-delivery/env`, root-owned, mode 640). Set
`LTD_DISABLE_DOTENV=1` on servers so a stray `.env` file is ignored.

| Setting | Production value |
|---|---|
| `APP_ENV` | `production` |
| `APP_BASE_URL` | `https://<confirmed domain>` (the origin users open) |
| `ALLOWED_HOSTS` | Optional; defaults to the host of `APP_BASE_URL`. Add the platform's internal host name only if requests arrive with it. |
| `DATABASE_URL` | `ltd_app` connection (`sslmode=require` when remote) |
| `FRONTEND_DIST_DIR` | Absolute path to `frontend/dist` |
| `ADMIN_MFA_ENCRYPTION_KEY` | **New** value: `python -m app.mfa generate-key` |
| `CRM_WEBHOOK_SECRET` | **New** value, 32+ characters; shared only with the CRM/automation that signs webhooks |
| `CLICKUP_*`, `SLACK_WEBHOOK_URL`, `GOOGLE_*` | Only after each integration is verified (see [integration-verification.md](integration-verification.md)); unset = jobs fail permanently with `*_NOT_CONFIGURED` and are retryable later |
| `WORKER_*`, `HTTP_TIMEOUT_SECONDS`, `JOB_MAX_ATTEMPTS` | Defaults are fine |

Never reuse development, demo or test secrets (including the n8n demo
webhook secret). Production refuses to start when `ADMIN_MFA_ENCRYPTION_KEY`
or `CRM_WEBHOOK_SECRET` is missing, when `APP_BASE_URL` is not https, when
`ALLOWED_HOSTS` has a wildcard, or when `FRONTEND_DIST_DIR` has no build.

`JWT_SECRET` and `CORS_ORIGINS` do not exist in this application: sessions
are opaque server-side tokens, and there is no cross-origin access.

## 3. Build and run

On a container platform use the production image instead of these commands:
see [build-and-release.md](build-and-release.md) (one image; API by default,
worker with `python -m app.jobs.worker`; proxy trust via
`FORWARDED_ALLOW_IPS`). The commands below are for a single VM.

```bash
# build (release step, at the commit being deployed)
cd frontend && npm ci && npm run build
cd backend && python3.12 -m venv .venv && \
    .venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.lock

# API: loopback only behind a proxy; trust X-Forwarded-For only from it
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 2 \
    --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-server-header

# Worker (separate service)
.venv/bin/python -m app.jobs.worker
```

`--forwarded-allow-ips` must be the proxy's address or range — **never `*`**
(uvicorn then uses the leftmost, client-forgeable `X-Forwarded-For` entry,
and some platforms, Render included, append to a client-supplied header) and
**not `127.0.0.1` on a hosted platform** (the proxy is not on loopback, so
every client would share one throttling bucket). On a platform, set it to
the proxy's private source range and verify it as described in
[production-runbook.md](production-runbook.md) step H5. On a platform the API
must also listen on `0.0.0.0:$PORT` instead of `127.0.0.1:8010`.

Single VM: [`deploy/ltd-api.service`](../deploy/ltd-api.service),
[`deploy/ltd-worker.service`](../deploy/ltd-worker.service),
[`deploy/Caddyfile.example`](../deploy/Caddyfile.example) and
[`deploy/release.sh`](../deploy/release.sh) (build → migrate as owner →
restart). On a platform (Render, Railway, Fly, etc.) the same commands map to
one web service (API) and one background worker. Migrations can run as a
platform pre-deploy command, but that puts the owner (`ltd_owner`) URL into
the service's environment; the first-deployment plan runs them from an
operator machine instead (see [production-runbook.md](production-runbook.md),
Phase F, and [deployment-preflight.md](deployment-preflight.md)).

## 4. Domain and HTTPS

Use the platform's generated HTTPS address for staging until the custom
domain is confirmed. Do not change DNS speculatively: confirm the exact host
name, point it at the proxy/platform, then set `APP_BASE_URL` to it (client
links are built from it). HSTS (`max-age=31536000; includeSubDomains`) is sent
in production, so only enable a custom domain when HTTPS works on it and all
its subdomains.

## 5. First administrator

On the server (or a one-off job with the same environment):

```bash
cd backend && .venv/bin/python -m app.admin_users create --email you@agency.example --name "Your Name"
```

It asks for the password twice, shows an authenticator link/key, requires a
current 6-digit code before saving, and prints 10 recovery codes once. There
are no default accounts.

## 6. Checks after each deploy

```bash
curl -fsS https://<domain>/api/health          # 200: database reachable, schema current
curl -fsS https://<domain>/api/health/worker   # 200: a worker reported in the last 90 s
curl -sI https://<domain>/portal | grep -i -E 'strict-transport|content-security'
curl -s -o /dev/null -w '%{http_code}\n' https://<domain>/docs   # 404 in production
```

Then sign in to `/admin`, open a synthetic invitation only on staging, and
record the deployed commit SHA (`git rev-parse HEAD`).

## What was verified (2026-11-10, local staging)

On a private PostgreSQL 16 instance with the two roles above: migrations as
`ltd_owner` (version 6); API and worker as `ltd_app` (which could not create
or drop tables); built frontend served by the API; production-mode startup
refused without `ADMIN_MFA_ENCRYPTION_KEY`; in production mode the API
answered 400 to an unknown host, 404 for `/docs` and `/openapi.json`, sent
HSTS and the strict CSP, no CORS headers, and 403 to a cross-origin admin
POST. Backup and restore: see [operations-runbook.md](operations-runbook.md).
Not verified: a real hosting platform, TLS termination, DNS, platform
logging/monitoring.
