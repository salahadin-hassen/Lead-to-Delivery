# Production runbook: first deployment

Ordered plan for the first deployment of one agency instance on the
recommended configuration (Render web service + background worker + Render
Postgres; see [infrastructure-comparison.md](infrastructure-comparison.md)).
Prepared 2026-11-11; **nothing below has been executed**. Day-2 operations
are in [operations-runbook.md](operations-runbook.md); the audit behind this
plan is [deployment-preflight.md](deployment-preflight.md).

**Who:** **Owner** = you (accounts, payment, secrets, DNS, approvals).
**Agent** = work an assistant can do in the repository or with read-only/
explicitly delegated access. Anything that creates, changes or pays for
infrastructure is Owner-approved, even if the agent prepares it.

## Phase A — Provider and account setup

| Step | Action | Who | Inputs | Expected result | Verify | Failure / rollback |
|---|---|---|---|---|---|---|
| A1 | Decide provider (recommended Render) and region (e.g. Frankfurt for EU/East-Africa latency, or the clients' region) | Owner | This document set | Written decision | — | — |
| A2 | Use or create a Render workspace **dedicated to Lead-to-Delivery** (not shared with Opportunity Hub services or secrets); add a payment method; enable 2FA on the account | Owner | Email, card | Workspace on Hobby plan | Dashboard shows empty workspace, 2FA on | Delete workspace |
| A3 | Connect GitHub with access to `salahadin-hassen/Lead-to-Delivery` only | Owner | GitHub admin | Repo selectable in Render | — | Revoke the GitHub app |
| A4 | Set a monthly spend alert / budget (~$30) | Owner | — | Alert configured | — | — |

## Phase B — Managed PostgreSQL

| Step | Action | Who | Inputs | Expected result | Verify | Failure / rollback |
|---|---|---|---|---|---|---|
| B1 | Create Render Postgres 16, plan `0.1c-256mb` ($6), same region as services, name `ltd-prod-db`, database `ltd` | Owner | Region | DB "available"; default user = owner role | Dashboard; `psql "<external URL>" -c 'select version()'` from your machine | Delete DB (no data yet) |
| B2 | Restrict external access (IP allow list: your IP only, or none) | Owner | Your IP | Only internal + your IP can connect | Connection from another network fails | Edit allow list |
| B3 | Create the restricted app user `ltd_app` (Render Credentials page/API, or `CREATE ROLE ltd_app LOGIN PASSWORD …` as the default user); apply the database/schema statements from [deploy/db-roles.sql](../deploy/db-roles.sql) adapted to the default user as owner | Owner (agent can prepare exact SQL) | Owner URL | Two credentials: owner, `ltd_app` | `psql` as `ltd_app`: `create table x(i int)` → permission denied | Drop role; if user creation is impossible, record the decision to run as owner (P1 risk) |
| B4 | Store both URLs in your password manager; **direct internal URL** for services, external URL only for operator migrations | Owner | — | — | — | Rotate passwords in Render |

## Phase C — API and worker services (no public traffic yet)

| Step | Action | Who | Inputs | Expected result | Verify | Failure / rollback |
|---|---|---|---|---|---|---|
| C1 | Create an **environment group** `ltd-prod` shared by both services (D6) with the non-secret values from the preflight table (`APP_ENV=production`, `APP_BASE_URL=https://<service>.onrender.com` for now, `FORWARDED_ALLOW_IPS` once known — see H5) | Owner (agent prepares the list) | Preflight §3 | Group exists | Values listed (no secrets yet) | Edit group |
| C2 | Create the web service `ltd-api` (0.5c-512mb, $7), runtime **Docker**, Dockerfile `./Dockerfile`, context `.`, branch `main`, auto-deploy **off**, default command (no override), health check `/api/health` | Owner | C1 | First image build runs | Build log shows both stages succeeded; no build-argument warnings mention secrets; CI `image` job green on the same commit | Fix the Dockerfile in the repo; nothing is live yet |
| C3 | Create the background worker `ltd-worker` (0.5c-512mb, $7), runtime Docker, same Dockerfile, same group, Docker command `python -m app.jobs.worker`, shutdown delay ≥ 60 s | Owner | C1 | Worker starts | Log: `worker started: id=… handlers=[…]` | Suspend worker |
| C4 | Expect the API to start and report **503** on `/api/health` (schema not migrated) and to refuse to start until secrets exist (fail-closed) | — | — | 503 / startup error naming the missing setting | Logs name the setting, never a value | — |

## Phase D — Secrets and first administrator

| Step | Action | Who | Inputs | Expected result | Verify | Failure / rollback |
|---|---|---|---|---|---|---|
| D1 | Generate **new** secrets on your machine: `python -m app.mfa generate-key` → `ADMIN_MFA_ENCRYPTION_KEY`; `python3 -c "import secrets;print(secrets.token_urlsafe(48))"` → `CRM_WEBHOOK_SECRET`. Never reuse demo/staging/local values | Owner | — | Two values in your password manager | Length check only | Regenerate |
| D2 | Add as **secret** env vars: `DATABASE_URL` (`ltd_app`, internal), `ADMIN_MFA_ENCRYPTION_KEY`, `CRM_WEBHOOK_SECRET` | Owner | D1, B4 | API starts | `/api/health` still 503 until Phase F | Remove/replace values |
| D3 | (after F2) Create the first administrator from a trusted shell with the production environment (Render Shell, if available on the plan, or your machine with the **external** owner/app URL): `python -m app.admin_users create --email <you> --name "<name>"`; store recovery codes offline | Owner | Authenticator app | Account with MFA; 10 recovery codes | Sign in at `/admin` | `app.admin_users disable/reset-mfa` |

## Phase E — Domain and HTTPS

| Step | Action | Who | Inputs | Expected result | Verify | Failure / rollback |
|---|---|---|---|---|---|---|
| E1 | Run the pilot first on `https://<service>.onrender.com` (no DNS changes) | Owner | — | HTTPS by the platform | `curl -sI https://<service>.onrender.com/api/health` | — |
| E2 | Confirm the production host name (e.g. `hub.<agency domain>`) | Owner | Domain decision | Written decision | — | — |
| E3 | Add the custom domain in Render; create the DNS record **exactly** as Render shows; wait for the certificate | Owner | DNS access | Certificate issued | `curl -sI https://<domain>/api/health` → 200, `strict-transport-security` present | Remove the DNS record |
| E4 | Set `APP_BASE_URL=https://<domain>`, `ALLOWED_HOSTS=<domain>,<service>.onrender.com`; redeploy. Client links are built from `APP_BASE_URL` (regenerate any issued earlier) | Owner | E3 | Links use the domain | Admin regenerate-link shows the domain | Revert variables |

## Phase F — Database migration and first release

| Step | Action | Who | Inputs | Expected result | Verify | Failure / rollback |
|---|---|---|---|---|---|---|
| F1 | From your machine at commit `<SHA>` (checked out, clean): `cd backend && DATABASE_URL='<owner external URL, sslmode=require>' .venv/bin/python -m app.migrations` (D3: owner credentials stay off the services) | Owner (agent can run it only with delegated credentials and explicit approval) | Owner URL | `schema at version 0006` | `--status` shows no PENDING | Migration failure rolls back its own transaction; fix with a new migration; DB is empty so it can be dropped and recreated |
| F2 | If `ltd_app` exists, run the GRANT block at the end of `deploy/db-roles.sql` as the owner | Owner | — | Grants in place | `ltd_app`: `select count(*) from schema_migrations` works; `drop table clients` fails | Re-run grants |
| F3 | Manually deploy the same `<SHA>` on both services | Owner | — | Both live | `/api/health` → 200; `/api/health/worker` → 200 within 90 s | Render rollback to previous deploy |
| F4 | Record the deployed SHA in the deployment log | Owner/Agent | — | SHA recorded | Render dashboard "Deploy" shows SHA | — |

## Phase G — Integrations

| Step | Action | Who | Inputs | Expected result | Verify | Failure / rollback |
|---|---|---|---|---|---|---|
| G1 | Launch with ClickUp, Slack and Sheets **unset** ([preflight §4](deployment-preflight.md#4-integration-launch-policy-verified-in-code-tests-and-staging)) | Owner | — | Jobs end `dead … NOT_CONFIGURED`; no external calls | Admin → Integrations shows "not configured" | — |
| G2 | Per integration, later: provide test credentials/targets **locally**, run its live check against `ltd_test`; agent records the result in [integration-verification.md](integration-verification.md) | Owner (credentials) + Agent (run, record) | Test List/channel/spreadsheet | REAL API VERIFIED | Live check prints PASSED | Revoke the test credential |
| G3 | Only after G2: add production credentials to the shared group, restart the worker, retry the relevant dead jobs | Owner | G2 | Jobs succeed | Integrations page + target system | Remove the variables; restart worker |

## Phase H — Backups, monitoring, recovery

| Step | Action | Who | Inputs | Expected result | Verify | Failure / rollback |
|---|---|---|---|---|---|---|
| H1 | Confirm PITR is active (3 days on Hobby) | Owner | — | Recovery page shows window | Dashboard | — |
| H2 | **Restore drill**: PITR to a new instance, run `python -m app.migrations --status` against it, compare row counts, delete it | Owner (agent can script) | — | Restored DB matches | Counts and schema 6 | Delete the recovery instance |
| H3 | Before each release: on-demand logical export; weekly: `pg_dump -Fc` to encrypted off-platform storage | Owner | Storage choice | Dumps exist | File present and restorable | — |
| H4 | External uptime monitor (e.g. any HTTP checker) on `/api/health` and `/api/health/worker`, alert after 2–3 failures; platform deploy-failure notifications on | Owner | Monitor account | Alerts reach you | Stop the worker → alert fires; start it again | — |
| H5 | **Proxy trust (D1)**: identify the platform's proxy source range, set `--forwarded-allow-ips` to it; make one failed login and check `select source_key from admin_login_attempts order by created_at desc limit 1` shows **your** public IP | Owner + Agent | Read-only DB query | Real client address recorded | Query result | Revert start command |

## Phase I — Smoke tests and controlled pilot

| Step | Action | Who | Expected result |
|---|---|---|---|
| I1 | `/api/health` 200; `/api/health/worker` 200; `/docs` 404; unknown `Host` → 400; `curl -sI /portal` shows CSP, HSTS, `Referrer-Policy: no-referrer`; cross-origin admin POST → 403 | Agent (read-only HTTP) | All as listed |
| I2 | Admin sign-in with TOTP; wrong code rejected; recovery code works once; logout ends session | Owner | As listed |
| I3 | One **synthetic** signed `deal.won` (synthetic name/email, never real client data) → 201; identical replay → 200 duplicate; wrong signature → 401 | Owner (holds the webhook secret) | As listed |
| I4 | Open the synthetic invitation, submit onboarding, see the dashboard; jobs appear as `dead … NOT_CONFIGURED` | Owner | As listed |
| I5 | Archive the synthetic client (or keep it clearly labelled) — **never delete or reset production data**; there is no reset tool | Owner | Synthetic data labelled |
| I6 | Pilot: one real client, agency staff aware that integrations are off; review logs and the dead-job list daily for the first week | Owner | No unexplained errors |

**Stop conditions:** any secret in logs, health not 200 after a deploy,
throttling recording the proxy instead of client addresses, or a restore
drill that does not reproduce the data → stop the pilot and fix first.

## What the agent can do without your accounts

- Prepare exact SQL, environment-variable lists and commands for each step.
- Lock files, the production `Dockerfile` and the CI workflow exist in the
  working tree (Task 12); they take effect once you authorise committing
  and pushing them ([build-and-release.md](build-and-release.md)).
- Run read-only HTTP smoke checks against a URL you provide.
- Run integration live checks locally once you place test credentials in the
  local environment, and record results.

## What requires you

Account creation and payment, region and domain decisions, DNS records,
generating/entering every production secret, database user creation,
first administrator enrolment (your authenticator), production migrations
(or explicit delegation), deploy triggers, monitoring signup, and accepting
any P1 risk in writing.
