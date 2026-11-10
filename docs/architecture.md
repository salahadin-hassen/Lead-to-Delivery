# Architecture

This document describes **what is implemented** (Task 1: backend foundation;
Task 2: CRM deal-won intake; Task 3: client portal, onboarding and project
generation; Task 4: durable integration worker; Task 5: ClickUp adapter;
Task 6: administrator authentication, management API and console; Task 7:
Slack notifications; Task 8: Google Sheets synchronization) and, separately,
what is planned. **Not production-ready.** Nothing under
"Planned" exists yet.

## Verification results

### Task 8.1 (2026-11-08)

- Coalesce-key mutual exclusion moved from an implicit invariant to an
  explicit lock (advisory xact lock + fresh re-check). Fixed a real defect:
  a retryable failure of a running keyed job while a newer version was
  pending crashed finalisation with a unique violation (job stuck `running`
  until lease expiry); such jobs are now marked superseded.
- 14 deterministic tests (barrier inside the claim's critical section, real
  OS processes). Negative controls: with the lock removed from the runner, 4
  of them fail (simultaneous claims, many workers, contention, processes).
- Test isolation fixed: the suite previously copied every `.env` key into the
  test environment, so real integration credentials added to `.env` reached
  tests and their subprocesses. Now only `TEST_DATABASE_URL` is read and
  `LTD_DISABLE_DOTENV=1` makes `Settings` ignore `.env` for the whole session
  (inherited by subprocesses); a test asserts it.
- A Task 8 stress test inserted jobs and set their coalesce key in a second
  statement, briefly exposing keyless jobs; the helper now writes the key in
  the same INSERT, like production code.
- Backend **609 passed** (two consecutive full runs); frontend **12 passed**.
  No migration.

### Task 8 (2026-11-08)

- Backend: **594 passed** (510 + 78 Sheets + 6 coalesce-serialization tests).
  Four earlier tests updated because Sheets is now registered (registry
  checks in the ClickUp and Slack suites, the worker subprocess test, the
  admin status shape).
- Negative controls: removing the coalesce `NOT EXISTS` clause makes the 3
  serialization tests fail; restoring it (byte-identical) makes them pass.
- Frontend: typecheck and build pass; **12 passed**.
- Sheets verified against fake Google endpoints only. **No real Google API
  call has been made** (no service account configured). ClickUp and Slack
  also still await real API verification.
- New dependency: `google-auth==2.62.1` (brought `cryptography` 50.0.2,
  `pyasn1` 0.6.5, `pyasn1_modules` 0.4.2). No migration; schema stays 0004.

### Task 7 (2026-11-08)

- Backend: **510 passed** (442 + 68 Slack tests). Three earlier tests were
  updated because Slack is now registered: the production registry check
  (Task 5), the worker subprocess test (Task 4/5: the Slack job now fails as
  `SLACK_NOT_CONFIGURED`; only Sheets stays pending) and the admin status shape
  (Task 6).
- Frontend: typecheck and build pass; **12 passed** (11 + 1).
- Slack verified against a fake endpoint only. **No real Slack request has
  been made** (`SLACK_WEBHOOK_URL` not configured). ClickUp also still awaits
  real API verification.
- No migration; schema version remains 0004.

### Task 6 (2026-11-08)

- Backend `python -m pytest -q`: **442 passed** (390 from Tasks 1–5 + 52 new).
  Task 1 migration tests were updated for the new migration 0004 (expected
  labels, version 4, table list); no other earlier test changed.
- Frontend: `npm run typecheck` and `npm run build` pass; `npm test`
  **11 passed**.
- Migration 0004 applied to `ltd` (schema version 4; second run applied
  nothing).
- Live: API + `vite preview` against seeded `ltd_test`, driven in headless
  Chrome: login (wrong password message, then success), dashboard with real
  counts, clients, client detail, link regeneration shown once, projects,
  task status change (progress 0/8 → 1/8), integrations, job detail with
  retry, CRM events, mobile width (no horizontal overflow), logout (`/me` →
  401 afterwards). Cookie observed as HttpOnly, SameSite=Strict,
  Path=/api/admin; browser storage empty.

### Task 5 (2026-11-08)

- `python -m pytest -q`: **390 passed** (327 from Tasks 1–4 + 63 new),
  0 failed, 0 skipped, three consecutive runs. One Task 4 test
  (`test_worker_process_starts_and_stops_cleanly_on_sigterm`) was updated:
  ClickUp is now registered, so its "untouched" example job is Slack/Sheets,
  and its output is read without blocking.
- All ClickUp behaviour verified against a fake API (`httpx.MockTransport`).
  **No live ClickUp request has been made**: `CLICKUP_API_TOKEN` and
  `CLICKUP_LIST_ID` are not configured; the live check exits with
  `CLICKUP_NOT_CONFIGURED`.
- No new migration; schema version remains 0003.

### Task 4 (2026-11-08)

- `python -m pytest -q`: **327 passed** (260 from Tasks 1–3 + 67 new), 0 failed,
  0 skipped, three consecutive runs.
- Live on `ltd_test`: an onboarding queued 12 jobs; `python -m app.jobs.worker`
  (default, empty registry) started, logged "no job handlers registered", left
  all 12 jobs `pending` with 0 attempts, and exited 0 on SIGTERM. A scripted
  run with fake adapters then produced: 10 ClickUp jobs `succeeded` (completion
  callback stored `tasks.clickup_task_id`), Slack retryable 503 → `pending`
  30 s later, Sheets unknown outcome → `dead` with
  `UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION` and an `integration.job_dead`
  activity event.
- No new migration; schema version remains 0003.

### Task 3 (2026-11-07)

- `python -m pytest -q`: **260 passed** (174 from Tasks 1–2 + 86 new), 0 failed,
  0 skipped, three consecutive runs.
- Live flow on `ltd_test` (`uvicorn`, curl): signed `gohighlevel-style` webhook
  → `GET /api/portal/me` 200 → `GET /api/portal/onboarding` 200 (SEO form)
  → `POST /api/portal/onboarding` 201 (3 milestones, 7 tasks, 6 client-visible)
  → repeat POST 409 `already_submitted`; foreign project id 404; unknown token
  401. 9 jobs queued (7 ClickUp, 1 Sheets, 1 Slack). Server log empty.
- No new migration; schema version remains 0003.

### Task 2 (2026-11-07)

- `python -m pytest -q`: **174 passed** (96 from Task 1 + 78 new), 0 failed,
  0 skipped, three consecutive runs.
- Live signed requests (bash + openssl HMAC) against `uvicorn` on `ltd_test`
  returned 201 created, 200 duplicate, 200 duplicate_deal, 409 event_id_reused,
  401 invalid_signature, 422 unknown_service_type and 503
  webhook_not_configured, leaving exactly 1 client, 1 invitation, 1 token and
  3 activity rows.
- No new migration; schema version remains 0003.

### Task 1 (2026-11-07, PostgreSQL 16.15)

Databases `ltd` and `ltd_test`, both owned by the non-superuser role
`sahelyon` (socket peer authentication), were empty before migration.

- `python -m app.migrations` on `ltd`: 0001–0003 applied (198 ms, 125 ms, 4 ms).
  `schema_migrations` checksums equal `sha256sum` of each file.
- Resulting objects in `public`: 14 tables, 52 indexes, 109 constraints,
  11 `updated_at` triggers, 1 function, 3 templates.
- Second run: "already applied" for all three; object and seed counts identical.
- `updated_at` trigger checked on `ltd` inside a rolled-back transaction.
- `python -m pytest -q` (backend/, `TEST_DATABASE_URL` → `ltd_test`):
  **96 passed, 0 failed, 0 skipped**, three consecutive runs. `ltd` object
  counts and timestamps were identical before and after the suite.
- With `TEST_DATABASE_URL=postgresql:///ltd` the suite aborts with
  `UnsafeTestDatabaseError` before any setup; `ltd` unchanged.
- Live: `uvicorn app.main:app` against `ltd` → `GET /api/health` 200 `{"status":"ok"}`.

## Implemented

### Components

```
backend/
  app/
    main.py         create_app(): lifespan opens/closes the DB pool; /api routers;
                    DB error handlers (503 unavailable, generic 500)
    config.py       Settings (pydantic-settings); secrets are SecretStr;
                    production fails closed; validation errors hide input values
    db.py           psycopg_pool ConnectionPool (dict_row), connection()/transaction(),
                    connection-held guard, ping/applied_schema_version
    migrations.py   versioned SQL migration runner + CLI (python -m app.migrations)
    security.py     scrypt password hashing, 256-bit tokens, SHA-256 token hashes,
                    constant-time compare
    mfa.py          TOTP (RFC 6238), recovery codes, AES-GCM encryption of TOTP secrets
    admin_users.py  administrator account CLI (create/reset/disable; python -m app.admin_users)
    web.py          security headers, host allowlist, body cap, built-frontend serving
    security.py     (also) webhook HMAC signing/verification
    errors.py       application error types (database, migrations, webhook intake)
    schemas/templates.py   Pydantic model for project_templates.definition
    schemas/crm.py         normalized deal.won payload (validation + normalisation)
    routes/health.py       GET /api/health
    routes/webhooks.py     POST /api/webhooks/crm/deal-won (auth, limits, responses)
    routes/portal.py       /api/portal/* (bearer-token dependency, no-store responses)
    routes/_http.py        bounded body reads, error bodies, input-free 422 details
    schemas/onboarding.py  onboarding form: core fields + per-service section
    schemas/portal.py      explicit portal response models + progress formula
    services/portal.py     token authentication -> PortalContext; read-only views
    services/onboarding.py one-transaction submission + template-driven generation
    services/crm_intake.py one-transaction intake: event claim, client resolution,
                           invitation, token, activity, retry on lost races
    store/                 SQL: webhook_events, clients, onboarding, projects, jobs, activity
    jobs/registry.py       Handler, HandlerRegistry, JobContext
    jobs/result.py         IntegrationResult (success/retryable/permanent/unknown), sanitising
    jobs/retry.py          backoff + Retry-After, HTTP status classification
    jobs/runner.py         claim -> execute -> fenced finalise, lease recovery
    jobs/handlers.py       production registry (empty until adapters exist)
    jobs/worker.py         process entry point: python -m app.jobs.worker
    services/job_admin.py  retry_dead_job (manual retry; no HTTP route yet)
    integrations/clickup.py           ClickUp adapter: create, reconcile, classify, persist
    integrations/clickup_live_check.py opt-in live check (CLICKUP_LIVE_TEST=1, ltd_test only)
    integrations/slack.py             Slack incoming-webhook adapter (validation, message, classification)
    integrations/slack_live_check.py  opt-in live check (SLACK_LIVE_TEST=1, ltd_test only)
    integrations/common.py            Retry-After parsing; webhook-URL log redaction filter
    integrations/google_auth.py       service-account loading/validation, httpx token-refresh transport
    integrations/sheets.py            Sheets upsert adapter (header check, project-ID scan, RAW writes)
    integrations/sheets_live_check.py opt-in live check (GOOGLE_SHEETS_LIVE_TEST=1, test spreadsheet only)
  sql/              0001_core_schema, 0002_events_and_integrations,
                    0003_seed_project_templates
  tests/            pytest suite against PostgreSQL ltd_test
```

Two processes share the database: the API (`uvicorn app.main:app`) and the
integration worker (`python -m app.jobs.worker`). They never call each other.

### Database access

- **Driver:** psycopg 3 with raw SQL. No ORM.
- **Pool:** `psycopg_pool.ConnectionPool`, min 1 / max 5, `dict_row`,
  `application_name=ltd-api`, connection check on checkout, 10 s checkout
  timeout, `statement_timeout=15s` per session. Opened without waiting, so the
  API starts while PostgreSQL is down and reports 503 until it is reachable.
- **Usage:** `db.connection()` for reads, `db.transaction()` for an explicit
  transaction (commit on success, rollback on exception).
- **External calls:** `db.assert_no_connection_held()` raises
  `ConnectionHeldError` if the current thread/task holds a pooled connection.
  Integration clients (later tasks) must call it before every HTTP request.
- **Errors:** pool timeouts and connection failures become
  `DatabaseUnavailableError` (HTTP 503, generic body). Deadlocks and
  serialization failures (`TransactionRollback`) are passed through unchanged
  so services can retry the transaction. Other driver errors are
  logged by class and SQLSTATE only and returned as a generic 500.

### Migrations

- Files: `backend/sql/NNNN_snake_case_name.sql`, applied in ascending order.
- Tracking table `schema_migrations(version, name, checksum, applied_at, execution_ms)`.
- Each migration runs in its own transaction together with its tracking row; a
  failure rolls back that migration entirely and reports version, name and
  SQLSTATE. Earlier migrations stay applied.
- Applied migrations are never re-run; editing an applied file is detected by
  SHA-256 checksum and stops the run. A recorded migration whose file is
  missing also stops the run.
- A session-level advisory lock (`pg_try_advisory_lock`, 60 s wait) serialises
  concurrent runners. `SET LOCAL lock_timeout = '10s'` inside each migration.
- Files may not contain transaction control or `CONCURRENTLY` (PL/pgSQL bodies
  are exempt from that check).
- Migrations are applied explicitly with `python -m app.migrations`, never at
  API startup. `/api/health` returns 503 while the schema is behind the code.

### Schema (14 tables + `schema_migrations`)

UUID primary keys (`gen_random_uuid()`), `timestamptz` throughout, text status
columns with CHECK constraints. Mutable tables have `updated_at`, maintained by
the `set_updated_at()` trigger; append-only tables (`onboarding_submissions`,
`activity_events`) have `created_at` only.

| Table | Purpose | Key constraints |
|---|---|---|
| clients | Agency customers | `UNIQUE (crm_source, crm_external_id)`; unique index on `lower(email)` (NULL allowed, many); CRM source must be lowercase |
| project_templates | Seeded blueprints, `definition` JSONB | `UNIQUE (key)` |
| onboarding_invitations | One per won deal (or manual) | `UNIQUE (crm_source, crm_deal_id)`; `submitted` ⇔ `submitted_at` |
| client_access_tokens | Portal tokens (SHA-256 only) | `UNIQUE (token_hash)`; `(invitation_id, client_id)` FK pins the token to the invitation's own client |
| onboarding_submissions | Client answers | `UNIQUE (invitation_id)`; same-client composite FK |
| projects | Delivery projects | `UNIQUE (invitation_id)`; same-client composite FK; `sync_version` for sync keys |
| milestones | Project phases | `UNIQUE (project_id, position)` (deferrable), `UNIQUE (project_id, template_milestone_key)` |
| tasks | Work items | `UNIQUE (project_id, template_task_key)`; `UNIQUE (clickup_task_id)`; milestone must belong to the same project |
| webhook_events | Inbound CRM events | `UNIQUE (source, external_event_id)`; `duplicate_count` |
| integration_jobs | Outbox + queue | `UNIQUE (idempotency_key)`; at most one *pending* job per `coalesce_key` (partial unique index); lease and completion consistency checks |
| integration_attempts | Attempt history | `UNIQUE (job_id, attempt_number)`; outcome ⇔ finished_at |
| activity_events | Timeline | `client_visible` flag for portal filtering |
| admin_sessions | Admin sessions (SHA-256 only) | `UNIQUE (token_hash)` |
| admin_login_attempts | Login throttling (migration 0004), append-only | indexes on `(source_key, created_at)` and `created_at` |

**Cross-client isolation in the schema.** `onboarding_invitations` has
`UNIQUE (id, client_id)`; tokens, submissions and projects reference
`(invitation_id, client_id)`, so a row can never point at another client's
invitation. Portal authorization (later task) is still enforced in queries;
this is a second line of defence.

**Email policy.** One client per normalized email. Intake code (Task 2) must
treat an email match with a different CRM identity or company as a conflict to
surface, never as a silent merge.

**Job idempotency.** Keys are operation-specific and permanent, for example
`clickup.create_task:{task_id}`, `slack.notify:project_created:{project_id}`,
`sheets.upsert_project:{project_id}:{sync_version}`. Synchronisation jobs can
also set `coalesce_key` (e.g. `sheets.upsert_project:{project_id}`): an
`INSERT … ON CONFLICT DO NOTHING` is skipped while one such job is still
pending (the pending job reads current state when it runs), while running,
succeeded or dead jobs never block a newer version.

### Templates

Three seeded templates (`website_build`, `seo_retainer`,
`marketing_automation_setup`), each with 3–4 milestones and 7–10 tasks.
`app.schemas.templates.TemplateDefinition` validates the JSON: unique milestone
and task keys, task due offsets within their milestone, ordered milestones.
There is no template management API.

### Configuration and secrets

All settings come from the environment or the gitignored repository-root
`.env`; `.env.example` lists names with safe placeholders. Integration
credentials are optional. In `APP_ENV=production` startup fails unless
`ADMIN_MFA_ENCRYPTION_KEY` is set, `CRM_WEBHOOK_SECRET` has at least 32
characters, `APP_BASE_URL` uses https, `ALLOWED_HOSTS` has no wildcard and a
configured `FRONTEND_DIST_DIR` contains `index.html`. A malformed MFA key is
rejected in every environment; there is no "auth disabled" mode.

### Health

`GET /api/health` → `200 {"status":"ok"}` only when PostgreSQL answers within
3 s **and** `schema_migrations` is at the version the code expects; otherwise
`503 {"status":"unavailable"}`. The reason is logged, never returned.

### Tests

pytest against a real PostgreSQL `ltd_test`. Destructive setup runs only after
two checks (and the suite reads only `TEST_DATABASE_URL` from `.env`; with
`LTD_DISABLE_DOTENV=1` no other `.env` value, such as integration
credentials, reaches tests or their subprocesses): `TEST_DATABASE_URL` names exactly `ltd_test`, and
`current_database()` on the live connection is exactly `ltd_test`
(`APP_ENV=production` refuses outright). Migration tests run in throwaway
schemas via `search_path`. No external network calls.

### CRM deal-won intake (Task 2)

`POST /api/webhooks/crm/deal-won`: HMAC-SHA256 over the raw body with a
±300 s timestamp window; 64 KiB limit; JSON only; then one PostgreSQL
transaction per delivery covering the event row, client resolution, invitation,
token hash, activity and the event status. Event-level and deal-level
idempotency come from the existing unique constraints; the losing side of a
deal race is rolled back to a savepoint. Full contract, guarantees and
mapping: [integrations.md](integrations.md).

Schema use without migration: duplicate deals and identity conflicts are
stored as `webhook_events.status = 'rejected'` with `error_code`
(`duplicate_deal`, `identity_mismatch`, `crm_identity_conflict`,
`company_conflict`); `received` is never committed.

### Client portal (Task 3)

**Authentication.** `Authorization: Bearer <token>` on every `/api/portal/*`
request. The FastAPI dependency `portal_context` hashes the token (SHA-256),
looks it up joined to its invitation and client, and returns a trusted
`PortalContext(token_id, client_id, invitation_id, …)`. Failures are 401 with
`WWW-Authenticate: Bearer` and a code: `token_missing`, `token_invalid`,
`token_expired`, `token_revoked` (token revoked, or invitation revoked).
Nothing from the path, query or body widens the scope; there is no client_id
parameter. GET requests write nothing (no `last_used_at` update). The token is
re-checked inside the submission transaction, so a token revoked after
authentication cannot submit.

**Invitation scoping.** Every portal query filters by the context's
invitation (projects) and client (milestones, tasks, activity via the
project). A client with several invitations sees, with each token, only that
invitation's project. `/api/portal/projects/{id}` returns 404 for any id other
than the token's own project.

| Endpoint | Success | Notes |
|---|---|---|
| `GET /api/portal/me` | 200 client name/company, invitation status (`pending`/`submitted`/`expired`/`revoked`), service, template, deal name, expiry, `next_step` | |
| `GET /api/portal/onboarding` | 200 `form_schema` (JSON Schema of the exact validation model), `prefill.project_name`, status | Form chosen by the invitation's `service_type` |
| `POST /api/portal/onboarding` | 201 `{"status":"submitted","project":{…}}` | 409 `already_submitted`, 409 `invitation_expired`/`invitation_revoked`, 422 validation, 413 > 32 KiB, 415 non-JSON, 503 `template_unavailable` |
| `GET /api/portal/project` | 200 project view | 404 `project_not_found` before submission |
| `GET /api/portal/projects/{id}` | 200 if `id` is this invitation's project | otherwise 404 |

All portal responses send `Cache-Control: no-store`.

**Onboarding form** (`app/schemas/onboarding.py`, `extra="forbid"` throughout):

| Field | Rules |
|---|---|
| `project_name` | required, 3–120 chars, single line |
| `project_description` | required, 10–2000 chars |
| `business_goals` | required, 10–2000 chars |
| `scope_requirements` | required, 10–4000 chars |
| `target_audience` | optional, ≤ 1000 |
| `desired_delivery_date` | optional ISO date, after today (UTC), within 730 days |
| `communication.preferred_channel` | required: `email`, `phone`, `video_call`, `slack` |
| `communication.update_frequency` | `weekly` (default), `biweekly`, `monthly` |
| `communication.contact_email` | optional, valid email, lower-cased |
| `additional_notes` | optional, ≤ 2000 |
| `service_details` | per service, below |

Service sections: **website_build**: `has_brand_assets` (required),
`existing_website_url`, `estimated_page_count` (1–200), `cms_preference`.
**seo_retainer**: `website_url` (required), `target_locations` (≤ 10),
`priority_keywords` (≤ 25), `has_analytics_access` (required).
**marketing_automation_setup**: `crm_platform` (required), `email_platform`,
`monthly_lead_volume` (0–1,000,000), `key_workflows` (≤ 10). Templates without
a section accept an empty `service_details`. Text is trimmed, `\r\n` becomes
`\n`, control characters are rejected, blank optional text becomes null.
Clients cannot send statuses, assignees, IDs, a service type or integration
settings: any unknown field is a 422.

**Project generation** (`services/onboarding.py`), in **one transaction**:
lock the invitation (`FOR UPDATE`), re-check the token, require `pending` and
not expired, load the invitation's template and validate it with
`TemplateDefinition`, then insert the submission, the project
(`start_date` = today in UTC, `due_date` = start + `default_duration_days`),
one milestone per template milestone (`position` = order, due = start +
offset, status `pending`), one task per template task (`template_task_key`,
title, description, `client_visible`, global `position`, due = start + offset,
status `todo`), the integration jobs, three activity events
(`onboarding.submitted` and `project.created`, client-visible;
`integrations.jobs_queued`, internal), and finally mark the invitation
`submitted`. Milestones and tasks come only from the template JSON, so editing
a template changes future projects without code changes. Any failure rolls
back everything.

**Outbox jobs** queued in that transaction (`integration_jobs`, status
`pending`, `max_attempts = JOB_MAX_ATTEMPTS`):

| job_type | idempotency_key | coalesce_key | subject | payload |
|---|---|---|---|---|
| `clickup.create_task` (one per task) | `clickup.create_task:{task_id}` | none | task | `project_id`, template `assignee_role` |
| `sheets.upsert_project` | `sheets.upsert_project:{project_id}:{sync_version}` | `sheets.upsert_project:{project_id}` | project | `sync_version` |
| `slack.notify` | `slack.notify:project_created:{project_id}` | none | project | `event: project_created` |

`store.jobs.enqueue` uses `INSERT … ON CONFLICT DO NOTHING`; it returns None
for a repeated key or while a job with the same `coalesce_key` is pending. For
the initial jobs every key is new, so a None aborts the transaction.

**Duplicate submissions.** The invitation lock serialises concurrent
submissions; the loser gets 409 `already_submitted`. `UNIQUE(invitation_id)`
on submissions and projects and `UNIQUE(project_id, template_task_key)` on
tasks are the database backstop (a violation is also mapped to 409).

**Progress.** Computed on read, never stored: completed ÷ total over **all**
tasks of the project (internal tasks count, since they are real work),
floored to a whole percent (100 only when every task is done); 0 tasks → 0 %.
Returned as `total_tasks`, `completed_tasks`, `open_tasks`, `percent`.

**Project view** fields: `id`, `name`, `status`, `description` (from the
submission), `start_date`, `due_date`, `created_at`, `progress`, `milestones`
(name, position, status, due date, client-visible tasks), `unassigned_tasks`,
`activity` (client-visible events of this project, newest first, max 50).
Tasks show title, description, status, due date, completed_at. The response
models are explicit (`extra="forbid"`): no token hashes, CRM or ClickUp IDs,
assignees, job data or internal activity can be serialised.

**Browser guidance for the future React portal.** Read the token from
`location.hash`, then remove it with `history.replaceState`; keep it in memory
or `sessionStorage`, never `localStorage`, cookies or query strings; send it
only as `Authorization: Bearer`; serve the portal with
`Referrer-Policy: no-referrer` and a strict Content-Security-Policy; render all
API text as text (React's default), never as HTML. The API is same-origin
(no CORS is configured).

### Integration worker (Task 4)

**Process.** `cd backend && .venv/bin/python -m app.jobs.worker`. Loads
settings, opens its own pool, builds the handler registry, then loops: run one
job; if none is due, wait `WORKER_POLL_SECONDS`. SIGINT/SIGTERM stop the loop
after the current job; the pool is always closed on exit. An exception in one
iteration is logged by class name and the loop continues (the job's lease
recovers it). Any number of worker processes may run.

**Claim** (one short transaction). Select one job of a *registered* type that
is either `pending`, due and with `attempts < max_attempts`, or `running`
with `locked_until < now()`; `FOR UPDATE SKIP LOCKED`; expired running jobs
first, then oldest. **Jobs sharing a `coalesce_key` never execute
concurrently** (Task 8, hardened in 8.1), across threads and processes:

1. The candidate query already skips a keyed job while another job with the
   same key is `running` (a pre-filter, valid as of the statement start).
2. After the candidate row is locked, the claim transaction takes
   `pg_advisory_xact_lock(hashtextextended('ltd-coalesce:' || key, 0))`.
   Claims of one key therefore run one at a time; the lock is
   transaction-scoped and released at commit or rollback.
3. A **separate statement**, run after the lock is granted (so under READ
   COMMITTED it sees everything the previous holder committed), checks for
   another `running` job with the key. If there is one, the candidate is
   skipped and the transaction changes nothing.

An expired running job blocks newer versions until it is recovered, and is
itself claimed first. Jobs without a key behave as before. Independently,
the partial unique index allows at most one *pending* job per key; the lock
does not rely on it (tests drop the index to prove that).

**Superseded retries.** If a keyed job fails with a retryable error while a
newer job of the same key is already pending, it is not rescheduled (that
would duplicate pending work and violate the index): it becomes `cancelled`
with `last_error_code = SUPERSEDED` (final state `superseded`), and the newer
job sends the current data. Manual requeue of such a job is refused
(`job_superseded`), including when the newer job appears concurrently. For an
expired lease: the open attempt is closed as `unknown` / `LEASE_EXPIRED`; if
no attempts remain the job becomes `dead` (`LEASE_EXPIRED`, plus an
`integration.job_dead` activity) and the next job is considered. Then:
`status = running`, `attempts = attempts + 1`, `locked_by = worker id`,
`locked_until = now() + WORKER_LEASE_SECONDS`, and a new
`integration_attempts` row (`attempt_number = attempts`). Commit.

**Execute.** The handler runs after the claim has committed, with no pooled
connection held (`db.assert_no_connection_held()` is called first). It
receives a validated `JobContext` (ids, type, idempotency key, subject,
payload, attempt number, `previous_outcome_unknown`). An optional
`payload_model` is checked first (invalid → permanent `INVALID_JOB_PAYLOAD`,
handler not called). An exception from the handler becomes outcome `unknown`
/ `HANDLER_EXCEPTION`, storing only the exception class name.

**Finalise** (one short transaction), fenced:

```sql
SELECT … FROM integration_jobs
WHERE id = :job AND status = 'running' AND locked_by = :worker AND attempts = :attempt
FOR UPDATE
```

No row → the lease was lost (another claim, possibly by the same worker
process, has a newer attempt number): the result is discarded and logged.
Otherwise the attempt row gets outcome, `finished_at`, HTTP status, sanitised
error code/message, external id and duration, and the job moves on:

| Outcome | Job becomes |
|---|---|
| `success` | handler `on_success(conn, ctx, result)` runs in this transaction, then `succeeded`, `completed_at`, lock and last error cleared |
| `retryable`, attempts left | `pending`, lock cleared, `next_run_at = now() + delay`, last error kept |
| `retryable`, none left | `dead` with the last error |
| `permanent` | `dead` (never retried automatically) |
| `unknown` | `dead`, `last_error_code = UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION` |

Every transition to `dead` adds an internal `integration.job_dead` activity
event linked to the job's project/client. If finalisation fails (database
error, callback error), everything in it rolls back: the job stays `running`
under its lease and is recovered later with that attempt marked `unknown`.
It is never marked `succeeded` by mistake.

**Retry delay.** `min(30 × 2^(attempt − 1), WORKER_MAX_RETRY_DELAY_SECONDS)`
→ 30, 60, 120, 240, 480, 900 s… A `retry_after_seconds` from the handler
(e.g. HTTP `Retry-After`) replaces it, bounded to [1, max].

**Manual retry** (`services.job_admin.retry_dead_job(job_id,
additional_attempts=…, reconciliation_confirmed=…)`): only `dead` jobs;
succeeded, pending, running and cancelled are refused. If the last attempt was
`unknown` (or the job died with `UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION` /
`LEASE_EXPIRED`), `reconciliation_confirmed=True` is required. The job
returns to `pending`, due now, with
`max_attempts = attempts + additional_attempts`. Attempts are not reset to
zero: attempt numbers are unique per job and are the fencing token, so they
keep counting. Attempt history is untouched; an `integration.job_requeued`
activity event records who, when and whether reconciliation was confirmed.
There is no HTTP route; the admin API (later task) will call this after
authenticating the administrator.

**Unregistered job types stay pending.** The worker only claims types with a
registered handler. `jobs/handlers.py` registers `clickup.create_task` (Task 5),
`slack.notify` (Task 7) and `sheets.upsert_project` (Task 8). On shutdown the worker calls each
handler's `close` (adapters close their HTTP clients). Claiming them without a handler would either fake a
success or burn their retries.

**Delivery guarantee.** At-least-once. A handler can run again for the same
job after a crash, a lease expiry, or a manual retry, and an external call can
succeed while its result is lost. Exactly-once external execution is not
claimed. Adapters must therefore (see `jobs/handlers.py`):
use the job's idempotency key and stored external ids to avoid duplicates;
reconcile (look the resource up) before creating when
`previous_outcome_unknown` is true; report `unknown`, never `retryable`, when
a request may have reached the provider without a definitive answer; and keep
their run time, including HTTP timeouts, well inside the lease
(`HTTP_TIMEOUT_SECONDS < WORKER_LEASE_SECONDS` is enforced at startup).

**Logging.** Job id, type, attempt, worker id, outcome, final state, HTTP
status, error code and duration. Never payloads, credentials, URLs or
exception messages. Stored error messages are ≤ 300 characters with URLs,
`Authorization`/`Bearer` values, `key=value` secrets, long opaque strings and
email addresses redacted; no tracebacks are stored.

**Configuration.** Existing: `WORKER_POLL_SECONDS`, `JOB_MAX_ATTEMPTS`,
`HTTP_TIMEOUT_SECONDS`. New: `WORKER_LEASE_SECONDS` (default 120, 10–3600),
`WORKER_MAX_RETRY_DELAY_SECONDS` (900, 30–86400), `WORKER_ID` (optional;
default `host:pid:random`).

### Administrator (Task 6)

**Authentication (production sprint, migration 0005).** Individual
administrator accounts (`admin_users`) replace the original shared
`ADMIN_PASSWORD_HASH`. `POST /api/admin/login {email, password, code}` needs
the password AND a TOTP code (never accepted twice) or a single-use recovery
code; all failures return the same 401 `invalid_credentials`. TOTP secrets are
AES-256-GCM encrypted with `ADMIN_MFA_ENCRYPTION_KEY`; missing key → 503
`admin_not_configured` (never open). Accounts are managed only on the server
(`python -m app.admin_users`). Success creates a 256-bit session token bound
to the administrator; only its SHA-256 goes into `admin_sessions` (expires in
12 h, or after 60 minutes idle; ends at once if the account is disabled).
`POST /api/admin/logout-all` revokes every session of that administrator.
Throttling: 5 failures per source and per account name, 50 overall, per 15
minutes. The raw token is
returned only as cookie `ltd_admin_session`: HttpOnly, SameSite=Strict,
Path=/api/admin, Max-Age 43200, Secure when `APP_ENV=production`. Every other
`/api/admin/*` route depends on `require_admin` (hash lookup, not revoked, not
expired; `last_seen_at` updated at most once a minute). `POST /api/admin/logout`
revokes the session and clears the cookie. 401s also clear the cookie.

**CSRF.** All non-GET admin requests (login included) need
`X-Requested-With: XMLHttpRequest`, a JSON body (else 415) of at most 64 KiB
(else 413), and, if an `Origin` header is sent, the origin of `APP_BASE_URL`
(else 403). No CORS middleware exists, so cross-origin scripts cannot send
the custom header.

**Login throttling** (migration 0004, `admin_login_attempts`): per source
(TCP peer address; `X-Forwarded-For` ignored) at most 5 failures since its last
success within 15 minutes, and at most 50 failures overall in 15 minutes →
429 `login_throttled` with `Retry-After`. Attempts from one source are
serialised with an advisory lock. While throttled, even the correct password
is refused.

**Routes** (all JSON, `Cache-Control: no-store`; list endpoints take `limit`
≤ 100 and `offset` and return `{items, total, limit, offset}`):

| Route | Purpose |
|---|---|
| `GET /api/admin/dashboard` | Aggregate counts (clients total/active/archived, pending invitations, projects active/completed, tasks open/done, jobs pending/running/dead), 5 active projects with progress, 5 newest dead jobs, 10 newest activity events |
| `GET/POST /api/admin/clients` | List (`q` searches name, company, email; `status`; `sort=created_desc|created_asc`), create |
| `GET/PATCH /api/admin/clients/{id}` | Detail (invitations with effective status and active-link expiry, projects with progress, activity), edit name/email/company/phone only |
| `POST /api/admin/clients/{id}/archive`, `/restore` | Status change; nothing is deleted |
| `POST /api/admin/invitations/{id}/regenerate-access-link` | New portal link, returned once |
| `GET /api/admin/projects`, `GET/PATCH /api/admin/projects/{id}` | List, detail (client, template, milestones, all tasks incl. internal, assignees, ClickUp id/URL and latest ClickUp job state, job summary, activity), status change |
| `PATCH /api/admin/projects/{id}/tasks/{task_id}` | Task `status` (`todo`/`in_progress`/`done`), `assignee_name`, `assignee_email` |
| `GET /api/admin/activity` | Filter by `client_id`, `project_id`, `event_prefix` |
| `GET /api/admin/integrations/status` | Per integration: implemented, credentials present, configuration valid (+ error code), ClickUp live verification (`verified_by_successful_job` only if a ClickUp attempt succeeded in this database) |
| `GET /api/admin/integration-jobs`, `/{id}` | Filter by `job_type`, `status`, `project_id`, `created_from`/`created_to`; detail with sanitised attempt history, reconciliation record, `requires_reconciliation` |
| `POST /api/admin/integration-jobs/{id}/retry` | `{additional_attempts 1–20, reconciliation_confirmed}` → `retry_dead_job` (409 `job_not_retryable` / `reconciliation_required`) |
| `POST /api/admin/integration-jobs/{id}/resolve-delivered` | Slack only: `{message_found_in_channel: true}` → job succeeded without resending (409 `job_not_resolvable_*`) |
| `GET /api/admin/webhook-events` | Event history (source, event id, status, error code, duplicates, deal id, service type, linked client/invitation via the invitation for that deal); no contact data |

Errors: 401 `admin_auth_required`, 403 CSRF codes, 404 `not_found`, 409
with a code (`email_taken`, `invitation_expired`, …), 422 validation details
without input values (applies to every route now).

**Client management.** Edits change only name/email/company/phone (email
normalised; `email_taken` on conflict); CRM identity and status cannot be
edited. Archive/restore never delete anything. **Policy for archived
clients:** a CRM deal matching an archived client is rejected with 409
`client_archived` and stored as a rejected event; it is not reactivated as a
side effect. After the administrator restores the client, the CRM's
redelivery of the same event is accepted (rejected identity conflicts are
re-evaluated).

**Access-link regeneration.** In one transaction: lock the invitation, revoke
all its active tokens, insert one new token hash, record
`invitation.access_link_regenerated` (kind, number revoked, expiry; never the
token). Pending, not expired → onboarding link expiring with the invitation
(an expired invitation is never reopened: 409 `invitation_expired`; no
extension operation exists). Submitted → project-access link for 180 days,
independent of the onboarding deadline; the submission and project are
untouched, and the token stays scoped to that invitation. Revoked → 409.

**Project and task updates.** One transaction each. A real status change
updates the task (`completed_at` set/cleared) or project, increments the
project's `sync_version`, queues `sheets.upsert_project:{id}:{version}` with
coalescing (no new row while one is pending), and records
`task.status_changed` / `project.status_changed` (client-visible when the task
is). Assignment changes record `task.assigned`. Unchanged values write
nothing. No ClickUp update job is created: local changes are not synchronised
to ClickUp, and the console says so.

**Admin activity events:** `client.created`, `client.updated` (field names
only), `client.archived`, `client.restored`, `invitation.access_link_regenerated`,
`project.status_changed`, `task.status_changed`, `task.assigned`,
`integration.job_requeued` (includes `reconciliation_confirmed`).

### Administrator console (frontend/)

React 19 + TypeScript + Vite 8, no UI framework and no router library (a
40-line history router). Pages: sign-in, dashboard (pipeline strip of real
counts, failed jobs, active projects, activity), clients (search, filter,
pagination, create), client detail (edit, archive/restore, invitations with
one-time link display and copy, projects, activity), projects, project detail
(task status and assignee editing, ClickUp links or sync state, "not synced
back to ClickUp" notice), integrations (status per integration, job filters,
job detail with attempt history, retry with the reconciliation checklist
required for unknown outcomes, CRM event history). Fonts are bundled
(`@fontsource`), so no external requests. All requests are same-origin with
`credentials: 'include'`; mutations send `X-Requested-With`; a 401 anywhere
returns to sign-in; nothing is stored in localStorage/sessionStorage; server
text is always rendered as text.

### Dependencies (pinned in `backend/pyproject.toml`)

fastapi 0.142.2, uvicorn 0.54.0, pydantic[email] 2.13.5,
pydantic-settings 2.15.0, psycopg[binary] 3.3.6, psycopg-pool 3.3.3,
httpx 0.28.1; tests: pytest 9.1.1.

## Known limitations

- Migrations are forward-only (no down migrations); fixes ship as new files.
- Pool size (1–5) and database timeouts are code constants, not settings.
- psycopg_pool logs libpq connection errors on reconnect; these include the
  host/socket and role name (never a password).
- The test suite emits Starlette's `StarletteDeprecationWarning` about using
  httpx with its TestClient; harmless today, to be revisited on upgrades.
- ClickUp, Slack and Google Sheets are registered, but **none has been
  exercised against its real API** (no credentials).
- Sheets: the worksheet is system-managed (header and column A must not be
  edited); at most 10,000 rows are searched; the ClickUp URL is the first
  task's and appears only at the next sync after ClickUp succeeds; a person's
  edits in a project row are overwritten; each sync reads the whole ID column
  (fine at agency scale, not for very large sheets); the service account is
  the only supported identity (no user OAuth).
- Slack: incoming webhooks only (no bot/OAuth); a delivered message cannot be
  looked up, so every unknown outcome needs a person to check the channel;
  HTTP 200 `ok` proves acceptance, not visibility; only `hooks.slack.com`
  `/services/…` URLs are accepted (Slack Workflow-builder webhooks and
  GovSlack hosts are refused).
- Admin: individual accounts with TOTP MFA, but no roles (every
  administrator has full access) and no self-service account management
  (server CLI only); sessions are not rotated during their 12 h life; login throttling sees a reverse proxy as a
  single source; expired pending invitations cannot be extended; the console
  is served by Vite in development/preview only (no production serving or
  deployment configuration); the dashboard's recent activity can be dominated
  by many similar job failures; webhook history links client/invitation by
  deal id, so events rejected before a deal existed show no client.
- ClickUp: personal token only (no OAuth); tasks are unassigned and use the
  List's default status; reconciliation scans at most 1000 recently created
  tasks (10 pages) and does not see archived tasks; date rendering for
  `due_date_time=false` is an assumption until verified live; the shared
  httpx client is not explicitly closed when the worker exits (process exit
  releases it); httpx logs request lines (method + URL with the List ID,
  never the token) at INFO.
- A ClickUp task created after the worker lost its lease is detected by
  marker on the next attempt; if a person removes or edits the marker in
  ClickUp, reconciliation cannot find it.
- A handler that overruns its lease cannot be interrupted (threads cannot be
  killed); another worker may then reclaim the job while it is still running.
  Fencing prevents the stale result from being recorded, but the external
  call may happen twice; adapters must stay inside the lease and be idempotent.
- One job per claim; no batching or per-type concurrency limits; polling, not
  LISTEN/NOTIFY (latency up to `WORKER_POLL_SECONDS`).
- Manual retry has no HTTP route until admin authentication exists.
- `webhook_events` does not link to the client/invitation it produced; the
  link is in `activity_events.data` (`event_id`, `invitation_id`).
- Currency is checked for format (3 letters), not against the ISO 4217 list.
- The onboarding URL is only returned once (in the webhook's 201 response);
  there is no way yet to re-issue a link. A secure, admin-triggered
  "regenerate link" (revoke old token, issue new one) belongs to the admin
  task; no other recovery path exists, by design.
- Portal access ends when the token expires (30 days after the deal), even
  after submission. Longer-lived project access needs the regeneration above.
- Template `assignee_role` has no task column; it travels in the ClickUp job
  payload only. Tasks are created unassigned.
- Project dates use the UTC date at submission time.
- Activity events written in one transaction share a timestamp, so their
  relative order in the portal timeline is not guaranteed.
- `last_used_at` on tokens is not maintained (portal GETs are read-only).

## Planned (not implemented)

- Live verification of the ClickUp adapter (needs credentials).
- Live verification of the Slack adapter (needs a sandbox webhook) and the Google Sheets adapter (needs a service account and test spreadsheet).
- Production deployment (configuration and runbooks exist: [deployment.md](deployment.md); no hosting account was available).
- (Done in the production sprint: client portal UI, admin MFA, hardening; see [production-readiness.md](production-readiness.md).)
