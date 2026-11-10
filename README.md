# Agency Lead-to-Delivery Automation Hub

When a deal is won in the CRM, this application starts client onboarding,
creates the project and its tasks, and keeps ClickUp, Google Sheets and Slack
up to date, with every integration failure recorded and safely retryable.

> **Status (2026-11-10): local/staging release candidate. NOT deployed and
> NOT production-ready** — see [docs/production-readiness.md](docs/production-readiness.md).
> What exists: the signed CRM deal-won webhook; the **client portal**
> (invitation link, onboarding form, project dashboard); template-driven
> project generation; the durable integration worker; three outbound adapters
> (**ClickUp**, **Slack**, **Google Sheets**); and the administrator console
> with **individual accounts and mandatory TOTP MFA**. **None of the three
> adapters has been verified against its real API** (tested against fake APIs
> only; see [docs/integration-verification.md](docs/integration-verification.md)).
> Single-agency deployment: one application and database per agency; not a
> multi-tenant SaaS. A local **n8n demonstration workflow** signs synthetic
> CRM events through a local relay ([docs/n8n-demo.md](docs/n8n-demo.md)); it
> is not a native GoHighLevel or HubSpot integration.
> See [docs/requirements.md](docs/requirements.md),
> [docs/architecture.md](docs/architecture.md) and
> [docs/integrations.md](docs/integrations.md).

### Google Sheets header (row 1 of the `Projects` worksheet, A1:M1)

```
project_id	client_name	company_name	project_name	template_name	project_status	progress_percent	completed_tasks	total_tasks	start_date	due_date	clickup_url	updated_at
```

(one column each; full setup in [docs/integrations.md](docs/integrations.md))

## Requirements

- Python 3.12
- PostgreSQL 16 (local)
- Node.js 22+ (admin console)

## Setup

### 1. Databases (once, as a PostgreSQL administrator)

Local development uses peer authentication over the Unix socket, so no
database password is stored anywhere. Replace `$USER` with your OS user name if
your shell does not expand it.

```bash
sudo -u postgres psql -c '\l ltd*'                 # inspect first: do they exist?
sudo -u postgres createuser --no-superuser --no-createdb --no-createrole "$USER"
sudo -u postgres createdb --owner="$USER" ltd
sudo -u postgres createdb --owner="$USER" ltd_test
```

`createuser`/`createdb` fail harmlessly if the role or database already exists;
nothing is dropped.

### 2. Backend

```bash
cp .env.example .env                    # then review values
cd backend
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m app.migrations      # apply migrations to ltd
.venv/bin/uvicorn app.main:app --port 8010
curl -i http://localhost:8010/api/health
```

### Commands

| Command (from `backend/`) | Purpose |
|---|---|
| `.venv/bin/python -m app.migrations` | Apply pending migrations (advisory-locked, transactional) |
| `.venv/bin/python -m app.migrations --status` | Show applied/pending migrations; exit 1 if any are pending |
| `.venv/bin/python -m app.mfa generate-key` | Generate `ADMIN_MFA_ENCRYPTION_KEY` |
| `.venv/bin/python -m app.admin_users create --email … --name "…"` | Create an administrator (password + authenticator + recovery codes); see `--help` for reset/disable |
| `.venv/bin/uvicorn app.main:app --port 8010` | Run the API |
| `.venv/bin/python -m app.jobs.worker` | Run the integration worker (separate process; Ctrl-C / SIGTERM stops it cleanly) |
| `CLICKUP_LIVE_TEST=1 .venv/bin/python -m app.integrations.clickup_live_check` | One controlled write to a dedicated ClickUp test List (needs credentials; uses `ltd_test`) |
| `SLACK_LIVE_TEST=1 .venv/bin/python -m app.integrations.slack_live_check` | One controlled post to a Slack sandbox channel (needs `SLACK_WEBHOOK_URL`; uses `ltd_test`) |
| `GOOGLE_SHEETS_LIVE_TEST=1 .venv/bin/python -m app.integrations.sheets_live_check` | Append/update/cleanup cycle on a dedicated test spreadsheet (needs a service account; uses `ltd_test`) |
| `.venv/bin/python -m pytest` | Run the test suite (needs `TEST_DATABASE_URL` → `ltd_test`) |

### 3. Administrator console

```bash
cd backend && .venv/bin/python -m app.mfa generate-key           # put the output in .env as ADMIN_MFA_ENCRYPTION_KEY
cd backend && .venv/bin/python -m app.admin_users create --email you@agency.example --name "Your Name"
cd backend && .venv/bin/uvicorn app.main:app --port 8010         # API (Vite proxies /api here)
cd frontend && npm install && npm run dev                        # http://127.0.0.1:5173/admin
```

| Command (from `frontend/`) | Purpose |
|---|---|
| `npm run dev` | Development server with `/api` proxied to `LTD_API_URL` (default `http://127.0.0.1:8010`) |
| `npm run build` | Typecheck + production build into `dist/` |
| `npm run preview` | Serve `dist/` on :4173 with the same `/api` proxy |
| `npm test` | Vitest component and client tests |

`APP_BASE_URL` must be the origin the console is opened from (default
`http://localhost:5173`; use `http://127.0.0.1:5173` if you open that), because
state-changing admin requests are rejected when their `Origin` differs.

The client portal is at `/portal#t=<token>` (the link the webhook returns or
the admin regenerates).

### Production

See [docs/deployment.md](docs/deployment.md),
[docs/security.md](docs/security.md) and
[docs/operations-runbook.md](docs/operations-runbook.md).
`APP_ENV=production` refuses to start without `ADMIN_MFA_ENCRYPTION_KEY`, a
32+ character `CRM_WEBHOOK_SECRET` and an `https` `APP_BASE_URL`. The API
serves the built frontend itself (`FRONTEND_DIST_DIR`), so the app and API
share one origin and no CORS is configured.

## Tests

The suite uses a real PostgreSQL database and **only** `ltd_test`. Before any
destructive setup it checks both the configured URL and the database it is
actually connected to; anything else (including `ltd`) aborts the run.
