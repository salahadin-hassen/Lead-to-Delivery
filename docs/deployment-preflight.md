# Deployment preflight

Prepared 2026-11-11 against commit `9a0ec2a` (`main` = `origin/main`). Nothing
has been deployed or provisioned. Each statement is marked **verified** (run
or read in this repository), **documented** (provider documentation), or
**unverified** (must be checked on the real platform).

## 1. Repository state (verified)

- Branch `main`, HEAD `9a0ec2aba22ba9b3ee1c3b3c2ca8a04d96246d6d`, identical to
  `origin/main`; working tree clean.
- Ignored/untracked, intentionally not committed: `.env` (local credentials),
  `candidate.id` (empty, origin unknown), `scripts/start-relay.sh` (empty,
  created by another agent), `backend/.pytest_cache` (left by another
  agent's test runs on 2026-11-07..09), `var/n8n` (local demo instance).
- Application version `0.1.0` (backend `pyproject.toml`, frontend
  `package.json`); no release tag exists.
- Schema: code expects version **6** (`0001`–`0006`). Local `ltd` is at **4**
  and was not touched; `ltd_test` is at 6. Production uses a new, separate
  managed database, so `ltd`'s state does not affect deployment. (Local admin
  login against `ltd` will not work until 0005–0006 are applied there — a
  local-development decision for the owner, not part of deployment.)

## 2. Architecture audit

| Area | Finding | Status |
|---|---|---|
| API start | `uvicorn app.main:app`; lifespan builds Settings (production fails closed), opens the pool **without waiting**; `/api/health` is 503 until PostgreSQL answers and the schema is at 6 | verified |
| API stop | Lifespan closes the pool on shutdown | verified |
| Worker start/stop | `python -m app.jobs.worker`; SIGTERM/SIGINT stop after the current job; heartbeat written at start, ≥ every 15 s, and on stop | verified (tests + staging) |
| Worker safety | `FOR UPDATE SKIP LOCKED`, 120 s leases, attempt-number fencing, lease expiry → `unknown` outcome requiring reconciliation | verified (suite) |
| Migrations | Forward-only, transactional, checksummed, **session** advisory lock (`pg_try_advisory_lock`) | verified |
| Frontend | `npm run build` → `frontend/dist`; served by the API when `FRONTEND_DIST_DIR` is set; production refuses a dir without `index.html` | verified |
| Health | `/api/health` (DB + schema), `/api/health/worker` (heartbeat < 90 s); both exempt from the host check | verified |
| Hosts | `ALLOWED_HOSTS`; production default = host of `APP_BASE_URL`; wildcard refused | verified |
| CORS | None configured anywhere; cross-origin admin POST → 403 | verified |
| Cookies | `ltd_admin_session` HttpOnly, SameSite=Strict, Path=/api/admin, Secure in production | verified |
| HTTPS | App does not redirect HTTP→HTTPS itself; relies on the platform edge; sends HSTS in production | verified (code) / platform redirect **unverified** |
| CSP/headers | Strict CSP (no inline scripts/styles); app ran clean under it in Chrome | verified |
| Pool | min 1, max 5 per process, 10 s checkout timeout, 15 s statement timeout set **per session** | verified |
| Connections | API (`--workers N`) 5·N + worker 5 + release 1 → 16 with N = 2 | verified (arithmetic) |
| Logging | App logs: event names, IDs, error codes; no bodies, tokens, secrets or emails (staging log scan). uvicorn access log: client address, method, path, status (no query secrets exist) | verified |
| Backups | Procedure tested on local staging (dump + restore, counts identical) | verified locally; platform backups **unverified** |
| Dependencies | `pip-audit` (35 pinned-environment packages): no known vulnerabilities; `npm audit` (prod and all): 0 vulnerabilities | verified 2026-11-11 |

### Discrepancies and defects found

| # | Finding | Severity | Action |
|---|---|---|---|
| D1 | `deploy/ltd-api.service` / `deployment.md` use `--forwarded-allow-ips 127.0.0.1`. On Render (and similar platforms) the proxy is **not** on loopback, so every request would appear to come from the load balancer: one attacker's 5 failed logins would throttle **all** administrators behind that address for 15 min. Setting `*` instead is worse: uvicorn 0.54 then takes the **leftmost** `X-Forwarded-For` entry, and Render appends to (does not replace) a client-supplied header, so the per-source limit becomes forgeable. Correct: trust only the platform's private proxy range (uvicorn then walks the header from the right). | **P1** | Determine the range on the real platform (Phase C check) before public launch; docs corrected |
| D2 | Session-level `statement_timeout` and psycopg's automatic prepared statements, plus the migration session lock, are incompatible with transaction-mode poolers (Neon pooler, PgBouncer). | P1 | Use the **direct** connection URL; documented |
| D3 | Render's pre-deploy command runs with the service's environment. Running migrations there means the owner (`ltd_owner`) URL lives in the API's environment, weakening least privilege. | P1 | Recommended: migrate from an operator machine before triggering the deploy (Phase F); alternative documented with its risk |
| D4 | Only top-level Python dependencies are pinned; transitive ones (Starlette, anyio, …) float. Builds are not byte-reproducible; a future transitive release could break or change behaviour. | P1 | **Resolved in the working tree (Task 12, uncommitted):** `backend/requirements.lock` / `requirements-dev.lock` with hashes, pinned to the tested versions; see [build-and-release.md](build-and-release.md) |
| D5 | Node/npm availability inside Render's Python build environment is not confirmed by Render's docs. | P0 for the Render plan | **Resolved by design (Task 12, uncommitted):** a multi-stage `Dockerfile` builds the frontend in a Node stage, so the platform's native Python environment is no longer used. Remaining check: the first platform/CI build of the image ([build-and-release.md](build-and-release.md)) |
| D6 | The admin Integrations page reports the **API's** configuration, but jobs run with the **worker's** environment. Different values on the two services would make the page wrong. | P2 | Use one environment group shared by API and worker |
| D7 | Unconfigured integrations still enqueue 12 jobs per onboarding, which become `dead` (`*_NOT_CONFIGURED`). There is no bulk retry; enabling an integration later means retrying jobs one by one (10 ClickUp jobs per project). | P2 | Acceptable for a pilot; document; consider a bulk retry later |
| D8 | `deploy/release.sh` runs `pip install .`, which also copies `app` into site-packages; the services import the checked-out source (uvicorn/`-m` put the working directory first). Works, but confusing. | P2 | `deploy/release.sh` now installs from `requirements.lock`; the image does not pip-install the project at all |
| D9 | `docs/security.md` said no dependency audit had run. | doc | Corrected |
| D10 | The worker's database connections report `application_name = ltd-api`. | P2 | Cosmetic |

## 3. Exact deployment configuration (recommended: Render)

All commands relative to the repository root. Values in `<>` are decided at
provisioning time. **unverified** = needs confirmation on the platform.

| Item | Value |
|---|---|
| Build | Docker, `./Dockerfile`, context = repository root (one image for API and worker; Python 3.12.16 runtime, Node only in the build stage) |
| API start command | The image's default command: `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --no-server-header`; proxy trust from `FORWARDED_ALLOW_IPS=<platform proxy CIDR>` (D1); process count from `WEB_CONCURRENCY` (default 1; 2 on 512 MB is reasonable) |
| Worker start command | Same image, command override: `python -m app.jobs.worker` |
| Worker shutdown delay | ≥ 60 s (`maxShutdownDelaySeconds`; a job's HTTP timeout is 15 s, lease 120 s) |
| Health check path | `/api/health` (platform health check); `/api/health/worker` (external monitor) |
| Migration | `cd backend && DATABASE_URL=<ltd_owner direct URL> python -m app.migrations`, **before** the new code starts; verify with `--status` |
| Extensions | None. `gen_random_uuid()` is built into PostgreSQL 13+; target PostgreSQL 16 |
| Database privileges | `ltd_owner`: owns DB and `public` schema. `ltd_app`: CONNECT; USAGE on `public`; SELECT/INSERT/UPDATE/DELETE on operational tables, `schema_migrations` and `project_templates` read-only, no TRUNCATE/REFERENCES/TRIGGER; USAGE/SELECT on sequences; default privileges from the owner ([deploy/db-roles.sql](../deploy/db-roles.sql), [deploy/db-app-grants.sql](../deploy/db-app-grants.sql)). Creating `ltd_app` on Render Postgres via `CREATE USER` or the Credentials API is **unverified** |
| Connectivity | Services → database over the provider's private (internal) URL in the same region; direct, not pooled; `sslmode=require` if the URL is external |
| `FRONTEND_DIST_DIR` | Set in the image (`/app/frontend/dist`); do not override |

### Environment variables (names exactly as read by `backend/app/config.py`)

| Variable | API | Worker | Value / generation |
|---|---|---|---|
| `APP_ENV` | ✓ | ✓ | `production` |
| `APP_BASE_URL` | ✓ | ✓ | `https://<confirmed domain>` (or the platform URL during staging) |
| `ALLOWED_HOSTS` | ✓ | – | `<custom domain>,<service>.onrender.com` while both are used |
| `DATABASE_URL` | ✓ | ✓ | `ltd_app` direct internal URL (secret) |
| `FORWARDED_ALLOW_IPS` | ✓ | – | the platform's proxy source range (D1); never `*` |
| `ADMIN_MFA_ENCRYPTION_KEY` | ✓ | – | new: `python -m app.mfa generate-key` (secret; store a copy in a password manager — losing it forces MFA re-enrolment) |
| `CRM_WEBHOOK_SECRET` | ✓ | – | new: `python3 -c "import secrets;print(secrets.token_urlsafe(48))"` (secret) |
| `LTD_DISABLE_DOTENV` | ✓ | ✓ | `1` |
| `CLICKUP_*`, `SLACK_WEBHOOK_URL`, `GOOGLE_*` | ✓ | ✓ | **unset** at launch (see §5); same values on both when enabled |
| `WORKER_*`, `HTTP_TIMEOUT_SECONDS`, `JOB_MAX_ATTEMPTS` | – | ✓ | defaults |
| `TEST_DATABASE_URL` | – | – | never set in production |

Not used by the code (do not create): `JWT_SECRET`, `CORS_ORIGINS`. Never copy
values from the local `.env`.

## 4. Integration launch policy (verified in code, tests and staging)

Launching with ClickUp, Slack and Google Sheets **disabled is safe**:

- Missing credentials → the adapter returns a permanent
  `CLICKUP_/SLACK_/SHEETS_NOT_CONFIGURED` result **before any network code**;
  no request is sent (staging: 25 jobs, all `dead … NOT_CONFIGURED`, no HTTP).
- No false success: such jobs end `dead`, visible in Admin → Integrations with
  the error code; the status card shows which credentials are configured
  (booleans only).
- These attempts are recorded as permanent, not `unknown`, so retrying them
  later needs no reconciliation; the unknown-outcome rules for real
  timeouts/lease expiry are unchanged.
- Re-enabling: set the credentials on API and worker (D6), restart the
  worker, run the provider's live check against a **test** target, then retry
  the dead jobs (one by one, D7). ClickUp creates are idempotent by marker;
  Sheets upserts by project ID; Slack messages are sent once per retry.
- Do not enable an integration in production before its live check passed
  ([integration-verification.md](integration-verification.md): ClickUp and
  Slack blocked by missing credentials; Sheets failed — spreadsheet not
  accessible, no separate test spreadsheet).

## 5. Security and operations review

| Topic | State | Before public launch | Acceptable for limited pilot |
|---|---|---|---|
| Dependency vulnerabilities | None known (pip-audit, npm audit, 2026-11-11); minor updates available (FastAPI 0.142→0.162, psycopg 3.3.6→3.3.8, Vite 8.3.4→8.3.8, …) | Lock transitive deps (D4); re-audit at release | Yes |
| Secret leakage | No secrets in Git (scanned per commit) or logs (staging scan); settings are `SecretStr`; production fails closed | Generate all production secrets fresh; never reuse demo/staging values | Yes |
| Admin auth/MFA | Individual accounts, mandatory TOTP, single-use hashed recovery codes, generic failures, timing-equalised | — | Yes |
| Session revocation | Logout, logout-all, CLI `revoke-sessions`, disable → immediate; 12 h max, 60 min idle | — | Yes |
| Invitation tokens | 256-bit, hash-only, expiring, revocable, invitation-scoped; fragment-only, memory-only in the browser | — | Yes |
| Rate limiting / proxy trust | DB-backed per source/account/global throttling; **proxy range not yet set for the platform (D1)** | **Must fix D1** | Yes if the admin URL is not advertised and the account-level limit is relied on |
| No edge rate limiting | Webhook and portal endpoints have no request-rate limit (HMAC/token required; bounded bodies) | Add platform/edge limits if traffic warrants | Yes |
| Database permissions | Two-role model verified locally; platform user creation unverified | Must verify `ltd_app` cannot run DDL on the real DB | No: the services must never run with the owner role; if `ltd_app` cannot be created, staging/pilot stops ([render-staging.md](render-staging.md) §6) |
| Backups | Platform PITR (3 days on Render Hobby) + manual logical exports; restore tested locally only | Run one real PITR restore drill on the platform | Yes after the drill |
| Backup encryption/retention | Provider-managed at rest (provider docs); local dumps must be stored encrypted; `ADMIN_MFA_ENCRYPTION_KEY` kept separately | Decide off-platform storage for weekly dumps | Yes |
| Log redaction | Verified on staging; uvicorn access logs contain client addresses (personal data) | Set platform log retention | Yes |
| Admin recovery | Server CLI (`app.admin_users`); on Render it runs from the operator's machine through a temporary `/32` database allow-list entry — Render Shell/SSH needs a login shell the image deliberately lacks ([render-staging.md](render-staging.md)) | Confirm shell/job access before launch | Yes, if verified |
| Incident response / rollback | Runbook exists ([operations-runbook.md](operations-runbook.md)); platform rollback = redeploy previous commit; schema forward-only | — | Yes |
| Penetration test | **Not performed** | Recommended before wider public exposure | Not required for a small pilot |

## 6. Blockers

**P0 — must be resolved before provisioning starts**
1. Provider decision and account owner (your account; payment method on it).
2. Region decision (data location for client personal data).
3. A first successful build of the production image (the CI `image` job
   after these changes are pushed, or the platform's first build) — D5.

**P1 — must be resolved before public launch (pilot may start with them tracked)**
1. Trusted proxy range on the platform (D1), verified by checking that a
   failed login records the real client address in `admin_login_attempts`.
2. Restricted `ltd_app` role created and verified (no DDL) on the managed DB.
3. Migrations run outside the service environment, or the owner-credential
   exposure accepted in writing (D3).
4. ~~Hashed dependency lock file (D4)~~ — lock files exist (uncommitted, Task 12).
5. One real PITR restore drill on the platform; weekly off-platform dump.
5a. Services and migrations use the **direct** (session) PostgreSQL URL, not
    a transaction-mode pooler (D2).
5b. Worker stop timeout (platform shutdown delay) ≥ 60 s.
6. Custom domain confirmed; HTTPS and HSTS verified on it.
7. External uptime monitoring with alerts on both health endpoints.

**P2 — acceptable for a pilot**
D6–D10, no bulk job retry, no admin roles, no edge rate limiting, no
penetration test (must stay true in all communication), integrations disabled.

## 6a. Render findings (2026-11-11) — paid reference

> The paid Render staging (≈ $20.30/month) was **not approved**: the staging
> budget is $0/month. The $0 options and the recommended intermittent staging
> (Render Free + Neon Free + the manual [staging worker](staging-worker.md))
> are in [free-staging-options.md](free-staging-options.md): publicly
> reachable, application-authenticated, synthetic data only — Render web
> services (free or paid) are internet-reachable, and Neon Free has no IP
> Allow. The findings below still apply to any Render deployment.

Researched for a publicly reachable, application-authenticated staging ([render-staging.md](render-staging.md),
[`deploy/render/render.paid-staging.yaml`](../deploy/render/render.paid-staging.yaml), validated against Render's schema):

- **D1 (proxy trust): unresolved, evidence recorded.** Render documents no
  proxy address range; its own articles state the app sees the proxy's
  address, Cloudflare appends to `X-Forwarded-For` (leftmost entry
  forgeable), and Cloudflare overwrites `CF-Connecting-IP` on every request.
  Staging keeps `FORWARDED_ALLOW_IPS=127.0.0.1` (shared throttling bucket,
  nothing spoofable). A public pilot needs `CF-Connecting-IP` support in the
  application (code change) — still P1.
- **D2 (direct connections): satisfied.** Render Postgres has no built-in
  pooler; the internal URL is a direct connection; 16 of 100 connections.
- **D3 (migration credentials): resolved by procedure.** No
  `preDeployCommand`; migrations and admin bootstrap run from the operator's
  machine through a temporary `/32` allow-list entry; `ipAllowList: []`
  otherwise. `fromDatabase.connectionString` (owner) is never used.
- **Role separation:** `ltd_app` via `CREATE ROLE`; whether Render's default
  user may create roles is undocumented — a stop condition in staging step 4.
- **Worker shutdown:** `maxShutdownDelaySeconds` 1–300 is documented for
  background workers; 120 s configured.
- **Port:** Render's `PORT` defaults to 10000; the image honours `$PORT`.

## 7. Readiness to provision

Ready to proceed to **provisioning a staging environment** on the chosen
provider once the P0 decisions are made. Not ready for a public production
launch until the P1 items are closed. Provisioning, payment, DNS and secrets
require the owner's accounts and approval; see
[production-runbook.md](production-runbook.md).
