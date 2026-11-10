# Agency Lead-to-Delivery Automation Hub

When a deal is won in the CRM, this application starts client onboarding,
creates the project and its tasks, and keeps ClickUp, Google Sheets and Slack
up to date, with every integration failure recorded and safely retryable.

> **Status: Tasks 1–9 of the build. Not production-ready.** What exists:
> the signed CRM deal-won webhook, client onboarding portal API with
> template-driven project generation, the durable integration worker, and
> three outbound adapters (**ClickUp** task creation, **Slack** notifications,
> **Google Sheets** project rows), plus an administrator console (React) with
> authentication, client and project management, access-link regeneration
> and integration monitoring. **None of the three adapters has been tested
> against its real API yet** (all await credentials; they are tested against
> fake APIs). A local **n8n demonstration workflow** signs synthetic CRM
> deal-won events and replays them against the webhook through a local relay
> that keeps the client invitation link out of n8n
> ([docs/n8n-demo.md](docs/n8n-demo.md)); it is not a live CRM integration.
> The client portal UI and deployment are not implemented.
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
| `.venv/bin/python -m app.security hash-password` | Generate `ADMIN_PASSWORD_HASH` |
| `.venv/bin/uvicorn app.main:app --port 8010` | Run the API |
| `.venv/bin/python -m app.jobs.worker` | Run the integration worker (separate process; Ctrl-C / SIGTERM stops it cleanly) |
| `CLICKUP_LIVE_TEST=1 .venv/bin/python -m app.integrations.clickup_live_check` | One controlled write to a dedicated ClickUp test List (needs credentials; uses `ltd_test`) |
| `SLACK_LIVE_TEST=1 .venv/bin/python -m app.integrations.slack_live_check` | One controlled post to a Slack sandbox channel (needs `SLACK_WEBHOOK_URL`; uses `ltd_test`) |
| `GOOGLE_SHEETS_LIVE_TEST=1 .venv/bin/python -m app.integrations.sheets_live_check` | Append/update/cleanup cycle on a dedicated test spreadsheet (needs a service account; uses `ltd_test`) |
| `.venv/bin/python -m pytest` | Run the test suite (needs `TEST_DATABASE_URL` → `ltd_test`) |

### 3. Administrator console

```bash
cd backend && .venv/bin/python -m app.security hash-password   # put the output in .env as ADMIN_PASSWORD_HASH
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

### Production notes (not a deployment guide)

`APP_ENV=production` requires `ADMIN_PASSWORD_HASH`, a 32+ character
`CRM_WEBHOOK_SECRET` and an `https` `APP_BASE_URL`, and marks the admin cookie
`Secure`. Serve the built console and the API from the same origin (a reverse
proxy routing `/api` to uvicorn); no CORS is configured. Login throttling keys
on the TCP peer address, so behind a proxy all clients share one bucket.

## Tests

The suite uses a real PostgreSQL database and **only** `ltd_test`. Before any
destructive setup it checks both the configured URL and the database it is
actually connected to; anything else (including `ltd`) aborts the run.
