# Operations runbook

For whoever runs a deployed instance. Commands run from `backend/` on the
server with the production environment loaded (`LTD_DISABLE_DOTENV=1`).

## Health and monitoring

| Check | Meaning when failing | Action |
|---|---|---|
| `GET /api/health` → 200 | 503: database unreachable or schema behind the code | Check the database; run migrations (release step) |
| `GET /api/health/worker` → 200 | 503 `no_worker`: no worker heartbeat in 90 s | Restart the worker service; check its log |
| Admin → Integrations: failed jobs | Integration errors (`*_NOT_CONFIGURED`, `*_UNAUTHORIZED`, …) | Fix the cause, then retry from the job page |

Point the platform's health check at `/api/health` and an external monitor
(uptime checker) at both endpoints, alerting after 2–3 consecutive failures.
Logs go to stdout/stderr (platform log stream or journald). They contain
event names, IDs and error codes, never request bodies, tokens, secrets or
email addresses.

## Worker

- Start/stop: `systemctl start|stop ltd-worker` (or the platform's worker
  service). SIGTERM finishes the current job, then exits.
- Several workers may run; each job is claimed once and results are fenced.
- A job whose last attempt has an **unknown** outcome (timeout after sending)
  needs a person to check the external system (e.g. the Slack channel, the
  ClickUp List) before retrying: the retry form asks for that confirmation.
- Retry after fixing a cause: Admin → Integrations → job → Retry.

## Administrators

```bash
.venv/bin/python -m app.admin_users list
.venv/bin/python -m app.admin_users create --email … --name "…"
.venv/bin/python -m app.admin_users reset-password --email …      # forgotten password
.venv/bin/python -m app.admin_users reset-mfa --email …           # lost phone (or use a recovery code first)
.venv/bin/python -m app.admin_users new-recovery-codes --email …
.venv/bin/python -m app.admin_users revoke-sessions --email …     # suspected stolen session
.venv/bin/python -m app.admin_users disable --email …             # someone leaves
```

## Clients

- Lost or expired client link: Admin → client → invitation → "Regenerate
  link" (old link stops working at once; the new one is shown once).
- Archived clients: new CRM deals for them are rejected (409
  `client_archived`) until restored.

## Backup and restoration

**Policy.** Use the managed provider's automated daily backups with
point-in-time recovery where available (retain at least 7 days), plus a
logical dump before every release:

```bash
pg_dump "$MIGRATION_DATABASE_URL" -Fc --no-acl -f ltd-$(date +%F-%H%M).dump   # store encrypted, off-server
```

Backups contain client personal data and encrypted TOTP secrets. Keep
`ADMIN_MFA_ENCRYPTION_KEY` separately (secret manager), never with the dumps;
a restore needs the same key or every administrator must `reset-mfa`.

**Restore (tested 2026-11-10 on staging: 98 KB dump, restored in under 1 s;
row counts and schema version 6 identical):**

```bash
createdb -O ltd_owner ltd_restore                              # as the DB admin
pg_restore -d "postgresql://ltd_owner@HOST/ltd_restore" --no-owner --exit-on-error ltd-….dump
DATABASE_URL="postgresql://ltd_owner@HOST/ltd_restore" .venv/bin/python -m app.migrations --status
# re-run deploy/db-app-grants.sql + deploy/db-app-check.sql (psql -1) as the owner, then point
# DATABASE_URL at the restored database and restart API and worker
```

Restoring makes integration jobs that were in flight look pending again;
they are idempotent by design, but check Slack/Sheets/ClickUp for duplicates
afterwards.

## Migrations and rollback

- Forward-only. A failed migration rolls back completely and blocks the
  release; fix it with a new migration file, never by editing an applied one
  (checksums are enforced).
- Rolling back **code** is safe when the older code works with the newer
  schema (all migrations so far only add tables/columns). If not, restore the
  pre-release dump.
- Migration 0005 revokes all sessions issued under the old shared password;
  create administrator accounts before deploying it.

## Secret rotation

| Secret | How | Effect |
|---|---|---|
| `CRM_WEBHOOK_SECRET` | New value in the CRM/automation and the API at the same time (brief rejected window; the CRM should retry) | Old signatures stop verifying |
| `ADMIN_MFA_ENCRYPTION_KEY` | No in-place re-encryption: set the new key, then every administrator runs `reset-mfa` | Logins fail until re-enrolled |
| Database passwords | Rotate in the provider; update `DATABASE_URL` / `MIGRATION_DATABASE_URL`; restart | — |
| `CLICKUP_API_TOKEN`, `SLACK_WEBHOOK_URL`, Google key | Replace, restart the worker, retry failed jobs | — |
| Admin password / MFA | `app.admin_users reset-password` / `reset-mfa` | Sessions revoked |

## Incident response

1. **Contain:** `disable` affected admin accounts and `revoke-sessions`;
   regenerate affected client links; rotate exposed secrets (table above).
   To stop all external writes, stop the worker (jobs queue up safely).
2. **Preserve:** keep logs and take a dump before changing data.
3. **Assess:** `admin_login_attempts`, `admin_sessions`, `activity_events`,
   `webhook_events` and `integration_attempts` show who did what and when.
4. **Recover:** fix, restart the worker, retry failed jobs.
5. **Notify** clients if their data was exposed, per the agency's obligations.

## Data retention

Kept indefinitely unless removed by the agency: clients, projects,
submissions, activity, webhook events, job attempts. Prunable at any time
without affecting behaviour: `admin_login_attempts` older than 15 minutes,
expired/revoked `admin_sessions`, `worker_heartbeats` (workers prune rows
older than 7 days). No automatic deletion of client data exists yet.

## Troubleshooting

| Symptom | Cause |
|---|---|
| API exits at start with "Insecure production configuration" | A required setting is missing; the message names it (never its value) |
| `400 invalid_host` | Request host not in `ALLOWED_HOSTS` / `APP_BASE_URL` host |
| `503 admin_not_configured` at login | `ADMIN_MFA_ENCRYPTION_KEY` missing or not the key the secrets were encrypted with |
| `429 login_throttled` | Too many failures; wait for `Retry-After` |
| Portal "This link has expired / is no longer active" | Regenerate the link in the admin console |
| Admin actions 403 `origin_not_allowed` | Console opened from an origin other than `APP_BASE_URL` |
