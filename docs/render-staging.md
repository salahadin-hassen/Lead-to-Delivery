# Render staging — PAID REFERENCE (not approved)

> **Not an approved plan.** This is the paid Render design (≈ $20.30/month)
> researched in Task 16. The budget is $0/month; the approved direction is in
> [free-staging-options.md](free-staging-options.md). Keep this document as a
> reference for a later paid staging or production decision. The Blueprint
> lives at `deploy/render/render.paid-staging.yaml` (not the default
> `render.yaml` path) so it cannot be applied by accident.

Provider-specific plan for a **publicly reachable, application-authenticated
staging (synthetic data only)** — Render web services are reachable from the
internet on every plan; nothing here makes them network-private — of
Lead-to-Delivery on Render, using the temporary `*.onrender.com` URL,
synthetic data only, with ClickUp, Slack and Google Sheets disabled.
Generic deployment rules are in [deployment.md](deployment.md) and
[deployment-preflight.md](deployment-preflight.md); this file does not repeat
them.

**Status (2026-11-11): nothing provisioned or deployed.** The Blueprint
[`deploy/render/render.paid-staging.yaml`](../deploy/render/render.paid-staging.yaml) validates against Render's published schema;
it has never been applied. Every Render behaviour below is cited from Render's
documentation or articles (checked 2026-11-11) and is marked where it still
needs to be observed on the real platform.

## 1. What runs (from the code)

| Item | Value |
|---|---|
| Image | `./Dockerfile`, context `.`: Node 24.22.2 build stage, Python 3.12.16 runtime, UID 10001, no `ENTRYPOINT` |
| API | Image default `CMD`: `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --no-server-header` (Render sets `PORT`, default 10000) |
| Worker | Same image, `dockerCommand: python -m app.jobs.worker` |
| Migrations | `python -m app.migrations` (and `--status`), **owner role, run separately** — never at start-up |
| First admin | `python -m app.admin_users create --email … --name "…"` (interactive) |
| Health | `GET /api/health` (database + schema 6; Render health check), `GET /api/health/worker` (worker heartbeat < 90 s; external monitor) |
| Database privileges needed at runtime | `SELECT/INSERT/UPDATE/DELETE` on the tables; transaction advisory locks; session `statement_timeout`; no sequences (UUID keys), no DDL, no `TRUNCATE`, no extensions (`gen_random_uuid()` is built in) |
| Connections | API: `WEB_CONCURRENCY` (2) × pool max 5 = 10; worker 5; migration/admin tool 1 → ≈ 16 of the plan's 100 |

## 2. Render facts this plan relies on (checked 2026-11-11)

| Fact | Source |
|---|---|
| Docker web services and background workers; `dockerfilePath`, `dockerContext`, per-service `dockerCommand`; multi-stage builds | [Blueprint spec](https://render.com/docs/blueprint-spec), [Docker](https://render.com/docs/docker) |
| Environment variables passed as Docker **build arguments** (the Dockerfile declares no `ARG`, so they never reach the image) | [Docker](https://render.com/docs/docker) |
| `sync: false` secrets are prompted **only at initial Blueprint creation**, never overwritten later; not allowed in environment groups | [Blueprint spec](https://render.com/docs/blueprint-spec) |
| `fromDatabase.connectionString` = internal URL **with the database's default user** (so it is not used here) | [Blueprint spec](https://render.com/docs/blueprint-spec) |
| Omitted plans default to paid `0.5c-512mb` (services) / `0.1c-256mb` (database), not free; plans are set explicitly anyway | [Blueprint spec](https://render.com/docs/blueprint-spec) |
| `healthCheckPath` (web only); `maxShutdownDelaySeconds` 1–300 for web, private and **worker** services (default 30) | [Blueprint spec](https://render.com/docs/blueprint-spec) |
| Deploy order build → pre-deploy → start; old instance gets SIGTERM, then SIGKILL after the shutdown delay | [Deploys](https://render.com/docs/deploys) |
| Services must bind `0.0.0.0`; `PORT` defaults to 10000; Render terminates TLS and redirects HTTP to HTTPS | [Web services](https://render.com/docs/web-services) |
| Internal database URL = private network, same region; **no built-in pooler** (direct connections); external URL always TLS, `sslmode=require` | [Create and connect](https://render.com/docs/postgresql-creating-connecting) |
| Connection limit 100 below 8 GB RAM; PostgreSQL 13–18; `ipAllowList: []` blocks all external connections | [Create and connect](https://render.com/docs/postgresql-creating-connecting), [Blueprint spec](https://render.com/docs/blueprint-spec) |
| Extra Render-managed users become the new *default* user (credential rotation); users made with `CREATE USER` are allowed but "not managed by Render" | [Credentials](https://render.com/docs/postgresql-credentials) |
| PITR 3 days (Hobby) / 7 days (Pro+); restore creates a **new** database; logical exports kept 7 days; none for free databases | [Backups](https://render.com/docs/postgresql-backups) |
| Shell/SSH: paid services only; Docker images need a non-root user **with a shell** and `~/.ssh` — this image's user has neither, so the interactive admin tool cannot run in a Render shell | [SSH](https://render.com/docs/ssh) |
| Regions: `oregon`, `ohio`, `virginia`, `frankfurt`, `singapore` | [Blueprint spec](https://render.com/docs/blueprint-spec) |

## 3. Cost (list prices, [pricing](https://render.com/pricing.md), 2026-11-11)

| Resource | Plan | Monthly |
|---|---|---|
| Workspace | Hobby (1 seat, 5 GB bandwidth, 500 build minutes) | $0 |
| API web service | `0.5c-512mb` | $7.00 |
| Worker | `0.5c-512mb` | $7.00 |
| PostgreSQL 16 | `0.1c-256mb`, 1 GB disk ($0.30/GB) | $6.30 |
| **Total** | | **≈ $20.30 / month** |

Compute is prorated to the second, so a staging environment that is
suspended or deleted after testing costs proportionally less. Usage-based
extras: bandwidth beyond 5 GB at $0.15/GB, build minutes beyond 500 at $5 per
1,000, PITR restores (a new database billed at its plan). No promotional
credit or free tier is assumed (free tier: no workers, no backups,
databases expire after 30 days).

## 4. Proxy trust: investigation and decision

**Evidence (Render, first-party):**
- "Because traffic passes through Cloudflare and Render's load balancers,
  your app sees the proxy's IP by default."
  ([How Render handles DDoS attacks](https://render.com/articles/how-render-handles-ddos-attacks), 2026-04-01)
- "Cloudflare appends to `X-Forwarded-For` rather than replacing it, so a
  caller who sends their own copy of the header controls the leftmost entry
  and can claim any address." ([Host PocketBase on Render](https://render.com/articles/host-pocketbase-on-render), 2026-07-08)
- "Cloudflare writes [`CF-Connecting-IP`] on every request that reaches a
  Render web service, [and] it overwrites whatever the caller sent." (same
  article)
- Render's reference documentation publishes **no** proxy or load-balancer
  address range ([Web services](https://render.com/docs/web-services) is
  silent on headers and source addresses).

**Consequences for this application (uvicorn 0.54 behaviour, verified in its
source):**
- `FORWARDED_ALLOW_IPS=*` → uvicorn takes the **leftmost**
  `X-Forwarded-For` entry → visitors can forge their address. **Never.**
- A CIDR → uvicorn takes the rightmost untrusted entry; with only Render's
  (undocumented) load-balancer range trusted, that would be a Cloudflare
  address, and Render documents no range anyway. **Not possible safely.**
- **Staging setting:** `FORWARDED_ALLOW_IPS=127.0.0.1` (explicit in
  `deploy/render/render.paid-staging.yaml`): `X-Forwarded-For` is ignored and every request appears to
  come from Render's proxy. Nothing can be spoofed, but login throttling's
  per-source limit becomes one shared bucket: five failed logins from anyone
  throttle admin login for everyone behind that proxy address for 15
  minutes. Per-account and global limits still work. Acceptable for a
  publicly reachable staging with synthetic data only; **not** for a public
  pilot.
- **Resolution (P1, needs a code change and a separate task):** have the
  application take the client address from `CF-Connecting-IP` when running
  behind Render (an explicit setting, off by default), and verify on staging
  that the header is present and correct and cannot be overridden by the
  caller.

Observation to record during staging (step 10): after one deliberate failed
admin login, `select source_key from admin_login_attempts order by created_at
desc limit 1;` shows the address the application sees (expected: a Render
proxy address).

## 5. Worker shutdown

`maxShutdownDelaySeconds: 120` on the worker (documented range 1–300). On
SIGTERM the worker stops claiming, finishes the current job (outbound HTTP
timeout 15 s) and exits 0 — proved against the real image in CI
(`.github/ci/image-smoke.sh`: "signal SIGTERM received", "worker stopped",
exit 0). If an instance is killed mid-job anyway, the job's 120 s lease
expires and the next worker records the attempt as `unknown`, which requires
reconciliation before a retry (no blind duplicate). Whether Render briefly
runs the old and new worker together during a deploy is not documented; it
is safe either way (`FOR UPDATE SKIP LOCKED`, fencing).

## 6. Database roles

Two roles in one new, independent database (never `ltd`, `ltd_test`, or any
other project's database):

| Role | Created by | Used by | Can |
|---|---|---|---|
| `ltd_owner` | Render (Blueprint `user`) | Operator only: migrations, role setup, grants | Owns database and schema |
| `ltd_app` | Operator, `CREATE ROLE` | API, worker, admin tool | DML on the tables only |

SQL, run as `ltd_owner` over the external URL (`sslmode=require`), from the
operator's machine. Choose the `ltd_app` password in a password manager; type
it at the `\password` prompt so it never appears in a command line or file:

```sql
CREATE ROLE ltd_app LOGIN;
\password ltd_app
REVOKE ALL ON DATABASE ltd FROM PUBLIC;
GRANT CONNECT ON DATABASE ltd TO ltd_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
```

If `CREATE ROLE` is refused (Render documents neither way whether its default
user may create roles), **stop**: do not point the services at the owner
role. That makes staging blocked until resolved with Render support.

(This `\password` path is for Render PostgreSQL. On Neon it does not apply —
Neon accepts only plaintext SQL passwords — use the Neon CLI scoped-role
procedure in [free-staging-options.md](free-staging-options.md) §5 step 3–4.)

After migrations (step 5), as the owner, run
[`deploy/db-app-grants.sql`](../deploy/db-app-grants.sql) and prove the
result with [`deploy/db-app-check.sql`](../deploy/db-app-check.sql), in one
transaction (`psql -X -1 -v ON_ERROR_STOP=1 -f deploy/db-app-grants.sql -f
deploy/db-app-check.sql`; again after every migration session). The grants
file first removes every privilege `ltd_app` holds, so a re-run never widens
anything; the check fails on any deviation from the exact policy. It grants
DML on the operational tables, keeps `schema_migrations` and
`project_templates` read-only (only migrations write them), never grants
`TRUNCATE`, `REFERENCES`, `TRIGGER` or any DDL, and sets default privileges
for later migrations. Verify as `ltd_app` — each must fail with
"permission denied" or "must be owner":

```sql
CREATE TABLE ddl_probe (i int);
ALTER TABLE clients ADD COLUMN ddl_probe int;
DROP TABLE clients;
TRUNCATE clients;
```

and these must succeed: `select max(version) from schema_migrations;`
(→ 6), `select count(*) from clients;`.

## 7. Staging secrets and variables

Generate every value on the operator's machine for **this staging only**
(never from Opportunity Hub, local `.env`, CI, or any other project), store
it in a password manager, and paste it only into Render's dashboard prompt.
Do not paste any of them into chat, tickets or logs.

| Name | Purpose | Secret | API | Worker | Migration / admin tool | Generate |
|---|---|---|---|---|---|---|
| `APP_ENV` | production mode (fail-closed config, Secure cookie, HSTS, no docs) | no | `production` | `production` | not needed | — |
| `APP_BASE_URL` | origin for client links, CSRF origin, host allowlist default | no | `https://<api>.onrender.com` | same | not needed | shown after service creation |
| `DATABASE_URL` | database connection | **yes** | `ltd_app` **internal** URL | same | owner **external** URL (migrations); `ltd_app` **external** URL (admin tool); both `?sslmode=require`, typed into the operator's shell, never stored on Render | Render (host), operator (`ltd_app` password) |
| `ADMIN_MFA_ENCRYPTION_KEY` | encrypts admins' TOTP secrets | **yes** | staging key A | **different** worker-only key | admin tool: key A | `python -m app.mfa generate-key` |
| `CRM_WEBHOOK_SECRET` | HMAC for the CRM webhook | **yes** | staging secret B | **different** worker-only value | not needed | `python3 -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `FORWARDED_ALLOW_IPS` | uvicorn proxy trust | no | `127.0.0.1` (§4) | — | — | — |
| `WEB_CONCURRENCY` | uvicorn processes | no | `2` | — | — | — |
| `ALLOWED_HOSTS` | host allowlist | no | unset (defaults to the `APP_BASE_URL` host) | — | — | — |
| `CLICKUP_*`, `SLACK_WEBHOOK_URL`, `GOOGLE_*` | integrations | yes | **unset** | **unset** | — | — |

The worker never reads `ADMIN_MFA_ENCRYPTION_KEY` or `CRM_WEBHOOK_SECRET`;
production validation requires them in every process, so it gets its own
values and the real key and webhook secret stay out of the worker.
`LTD_DISABLE_DOTENV=1` and `FRONTEND_DIST_DIR` are set in the image.

Generated secrets print to the operator's terminal; redirect them straight
into the password manager or a file with mode 600, and clear the terminal.

## 8. From an empty workspace to (paid) staging

Assumptions: a Render workspace dedicated to Lead-to-Delivery (not shared
with Opportunity Hub), 2FA on the account, a payment method, GitHub access to
`salahadin-hassen/Lead-to-Delivery` only. Operator machine: this repository
at the `main` commit being deployed, `python3.12`, a virtualenv from
`backend/requirements.lock`, and `psql` 16+. Region: one of the five; all
three resources in the same one (`deploy/render/render.paid-staging.yaml` says `frankfurt` — change all
three lines before applying if another region is chosen).

1. **Secrets.** Generate the values in §7 (key A, secret B, worker-only key
   and secret, `ltd_app` password).
2. **Blueprint.** Render → New → Blueprint → repository, branch `main`,
   Blueprint file path `deploy/render/render.paid-staging.yaml`. Check plans, region and that no domain is listed.
   At the `sync: false` prompts enter: `APP_BASE_URL` =
   `https://ltd-staging-api.onrender.com` (corrected in step 6 if Render
   assigns a different name); `DATABASE_URL` = a placeholder that cannot
   connect (`postgresql://pending@invalid.invalid/ltd`); the API's and the
   worker's own secrets. Apply.
   *Expected:* the database becomes available; the API's first deploy does
   not go live (it cannot reach a database, so `/api/health` is never 200);
   the worker logs connection errors. Nothing can be stored anywhere.
   Suspend both services until step 7.
3. **External access, temporarily.** Database → Access control: add the
   operator's current public IP as `/32`. Copy the **external** URL (owner).
4. **Role.** `psql "<owner external URL>?sslmode=require"`, then the first
   SQL block of §6. Stop here if `CREATE ROLE` is refused.
5. **Migrations** (operator machine, owner credentials never stored on
   Render):
   ```bash
   cd backend
   export LTD_DISABLE_DOTENV=1        # never read the local .env
   read -rs DATABASE_URL && export DATABASE_URL   # paste the owner external URL + ?sslmode=require
   .venv/bin/python -m app.migrations
   .venv/bin/python -m app.migrations --status    # all applied, schema at version 0006
   unset DATABASE_URL
   ```
   Then the grant and verification SQL of §6.
6. **Service variables.** Set `DATABASE_URL` on both services to
   `postgresql://ltd_app:<password>@<internal host>:5432/ltd` (internal
   host from the database's Info page). Set `APP_BASE_URL` on both to the
   API's actual `onrender.com` URL.
7. **Start.** Resume the API and deploy the current `main` commit (manual
   deploy; auto-deploy is off). *Expected:* health check passes; the deploy
   goes live. Then resume and deploy the worker.
   `curl -s https://<api>.onrender.com/api/health` → `{"status":"ok"}`;
   `…/api/health/worker` → 200 within 90 s.
8. **First administrator** (operator machine, still allow-listed):
   ```bash
   cd backend && export LTD_DISABLE_DOTENV=1
   read -rs DATABASE_URL && export DATABASE_URL   # ltd_app EXTERNAL URL + ?sslmode=require
   read -rs ADMIN_MFA_ENCRYPTION_KEY && export ADMIN_MFA_ENCRYPTION_KEY   # key A (the API's)
   .venv/bin/python -m app.admin_users create --email <you> --name "<name>"
   unset DATABASE_URL ADMIN_MFA_ENCRYPTION_KEY
   ```
   It asks for the password twice, shows the authenticator link once,
   requires a current code before saving (MFA is mandatory), and prints 10
   recovery codes once: store them offline, separately from the password.
   Recovery later: the same tool with `reset-password`, `reset-mfa`,
   `new-recovery-codes`, `revoke-sessions`, `disable` (same temporary access
   procedure).
9. **Close external access.** Remove the `/32`; confirm the allow list is
   empty and that `psql` from the operator machine is refused.
10. **Verify** (all against the `onrender.com` URL):
    - `/docs` → 404; `curl -I …/portal` shows CSP, HSTS,
      `Referrer-Policy: no-referrer`; a wrong `Host` header → 400.
    - Admin sign-in at `/admin` with the TOTP code; a wrong code is
      refused; sign out ends the session.
    - Right database and role: as `ltd_owner` (temporary access again) or in
      the database's dashboard metrics, `select usename, application_name,
      count(*) from pg_stat_activity where datname = 'ltd' group by 1, 2;`
      shows `ltd_app` / `ltd-api` connections only.
    - No migration at start-up: the API/worker deploy logs contain no
      `applied` lines; `select version, applied_at from schema_migrations`
      shows only the step-5 times.
    - Integrations off: Admin → Integrations shows nothing configured; jobs
      end `dead` with `*_NOT_CONFIGURED`; no outbound requests.
    - Secrets not exposed: search the Render logs of both services for the
      first 8 characters of each secret (none); the browser's network panel
      shows the client token only in `Authorization` headers.
    - Proxy observation (§4) recorded.
    - Worker shutdown: restart the worker; its log shows "signal SIGTERM
      received … worker stopped"; `/api/health/worker` returns to 200.
11. **Synthetic data only.** Send a signed `deal.won` with an invented
    contact (`@example.com` addresses, no real names or companies), e.g.
    the payloads in `docs/examples/crm/`, signed with secret B read from a
    file (never on the command line); complete onboarding through the
    portal link; replay → 200 duplicate. Never enter real client
    information.

### Rollback and teardown

- Bad application deploy: Render "Rollback" to the previous deploy of the
  service (schema migrations are forward-only and additive, so older code
  keeps working).
- Bad data: PITR (3 days) restores into a **new** database; point both
  `DATABASE_URL`s at it after re-creating `ltd_app`'s grants.
- Teardown (staging only, after explicit approval): delete the Blueprint's
  services and the database. All staging data is destroyed; there is no
  real client data to preserve.

## 9. Risks

| Priority | Risk | State |
|---|---|---|
| **P0** | — | No issue prevents a safe *private* staging, provided the stop conditions hold: `ltd_app` can be created (step 4), and the services never receive owner credentials |
| P1 | **Proxy trust** | Unresolved. No documented proxy range; `X-Forwarded-For` forgeable; staging ignores it (shared throttling bucket). Needs `CF-Connecting-IP` support in the app plus staging verification before a public pilot |
| P1 | Role separation on Render | Procedure defined; `CREATE ROLE` permission of Render's default user **not yet observed** |
| P1 | Migration credential isolation | Achieved by running migrations from the operator machine through a temporary `/32`; requires discipline (no `preDeployCommand`, owner URL never stored on Render) |
| P1 | Backups | PITR 3 days on Hobby; restore drill not yet performed; no off-platform dump yet |
| P1 | Monitoring and alerting | None yet: add an external uptime check on both health endpoints and Render deploy-failure notifications before a pilot |
| P1 | HTTPS / domain | `onrender.com` HTTPS by Render (HTTP→HTTPS redirect documented); custom domain and HSTS on it not configured (out of scope) |
| P2 | Direct connections | Satisfied: Render offers only direct connections; 16 of 100 used |
| P2 | Worker shutdown | 120 s configured; image behaviour proved in CI; platform overlap during deploys undocumented but safe |
| P2 | Worker validation | The worker needs MFA/webhook values only to pass start-up validation; could be relaxed per process in code |
| P2 | Admin tool access | Requires temporary external database access; an in-platform path would need a login shell in the image (deliberately absent) |
| P2 | Blueprint updates | `sync: false` values are prompted once; later secret changes are made in the dashboard |

## 10. Decisions needed from the owner before provisioning

1. Approve staging on Render at ≈ $20.30/month (prorated), in a dedicated
   workspace with 2FA and a payment method you add.
2. Choose the region (all three resources); `deploy/render/render.paid-staging.yaml` currently says
   `frankfurt`.
3. Approve the temporary `/32` database access procedure for migrations and
   admin bootstrap.
4. Accept the staging-only proxy limitation (§4) and the stop conditions.
5. Approve teardown when staging is no longer needed.
