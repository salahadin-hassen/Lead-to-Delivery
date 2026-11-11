# Production readiness

**Decision (2026-11-10): NO-GO for production. Local/staging release
candidate only.**

The application is functionally complete for a single agency and passed
local staging QA, but it has **not been deployed** (no hosting account was
available), **no integration has been verified against its real API**, and
backup/monitoring exist only as procedures. It must not be described as
production-ready until the open items below are closed.

Scope: one agency per deployment (own application and database). Not a
multi-tenant SaaS. The n8n workflow is a simulated CRM demo, not a native
GoHighLevel/HubSpot integration.

## Checklist

### Product
- [x] Client invitation and onboarding experience (`/portal#t=…`), form generated from the API schema
- [x] Client project dashboard (progress, milestones, tasks, client-visible updates)
- [x] Admin console (clients, invitations/link regeneration, projects/tasks, integration jobs, CRM events)
- [x] No fake metrics or mocked data shown as real (all figures come from the API)

### Security
- [x] No shared administrator password (migration 0005)
- [x] Individual administrator identity
- [x] Mandatory TOTP MFA with single-use recovery codes
- [x] Session expiry/idle timeout/revocation; CSRF controls; SameSite=Strict HttpOnly cookie
- [x] Client invitation isolation (tests + staging: other project → 404, revoked/expired → 401)
- [x] HMAC webhook and replay behaviour (tests + staging)
- [x] Secrets absent from Git (secret scan of every commit) and from logs (staging log scan)
- [x] HTTPS-only config, host allowlist, no CORS, trusted-proxy guidance, security headers
- [x] Fail-closed n8n relay configuration (Task 9.1)
- [ ] External penetration test — **not done** (dependency audit run 2026-11-11: no known vulnerabilities)

### Reliability
- [x] Backend regression passes (see Evidence)
- [x] Frontend tests and build pass
- [x] Worker concurrency and recovery verified (suite: leases, fencing, SKIP LOCKED)
- [x] Retry and unknown-outcome reconciliation verified (suite; mock adapters)
- [x] Health, readiness and worker liveness endpoints
- [x] Backup and restoration procedure demonstrated (local staging)
- [ ] Backups and monitoring configured on real infrastructure — **not done**

### Integrations
- [ ] ClickUp real API — **BLOCKED: credential missing**
- [ ] Slack real API — **BLOCKED: credential missing**
- [ ] Google Sheets real API — **FAILED: spreadsheet not accessible; no separate test spreadsheet**
- [x] Generic CRM webhook works (staging)
- [x] No mocked test reported as live success ([integration-verification.md](integration-verification.md))

Unconfigured integrations can be left disabled for a first launch: their jobs
fail visibly with `*_NOT_CONFIGURED` and can be retried later.

### Deployment
- [x] Production-like configuration ([deployment.md](deployment.md), `deploy/`)
- [ ] Managed, isolated PostgreSQL — **not provisioned** (roles/grants verified on a private staging instance)
- [x] Separate API and worker processes (design and staging)
- [ ] Production secrets provisioned — **not done**
- [ ] HTTPS and domain routing — **not done** (domain not confirmed)
- [x] Migrations applied safely (staging, as schema owner)
- [ ] Logs and monitoring on the platform — **not done**
- [x] No dependency on local machine services
- [ ] Exact deployed version — **none deployed**

### Repository
- [x] Clean Git baseline, pushed to `github.com/salahadin-hassen/Lead-to-Delivery`
- [x] No secrets in committed files
- [x] Changes committed and pushed
- [x] Documentation matches behaviour (this sprint's docs written from the code)

## Evidence (2026-11-10)

| Check | Result |
|---|---|
| Backend suite (`pytest`, PostgreSQL `ltd_test`) | 780 passed (baseline before the sprint: 732) |
| Frontend unit/component tests (`vitest`) | 27 passed (baseline: 12); 15 consecutive clean runs after fixing one test race |
| Type check + production build (`npm run build`) | passed |
| Real browser (Google Chrome, staging) | 22/22 checks: portal invitation, validation, submission, dashboard, reload, mobile layout, token handling, no third-party requests, no console errors under strict CSP, admin TOTP login, task edit, sign-out |
| Staging API scenarios | identity resolution, conflict, archive/restore, isolation, expired and revoked links, double submission, admin recovery code (single use), job retry, logout |
| Production-mode instance | refuses start without MFA key; 400 unknown host; docs 404; HSTS + CSP; no CORS; 403 cross-origin admin POST |
| Least-privilege DB role | app role cannot create/drop tables |
| Backup/restore | dump restored; all row counts and schema version identical |
| Log scan (API, worker) | no secrets, tokens, links or email addresses |

## Remaining blockers (to reach GO)

1. A hosting account/platform for the API, the worker and managed PostgreSQL
   (and approval for any cost).
2. The production domain, confirmed, with DNS pointed at the platform.
3. Production secrets generated and stored in the platform's secret manager;
   first administrator created with MFA.
4. Platform backups (daily + point-in-time) and uptime monitoring/alerts on
   `/api/health` and `/api/health/worker`.
5. Deployment gates from [deployment-preflight.md](deployment-preflight.md)
   §6 (P1): platform proxy range (`FORWARDED_ALLOW_IPS`), direct database
   URL, migrations with separate owner credentials, restore drill, worker
   stop timeout; and a first green CI run including the image build
   ([build-and-release.md](build-and-release.md)).
6. Decide per integration: verify against real test targets (credentials and
   test spreadsheet/List/channel needed) or launch with it disabled.
7. Post-deploy verification against the deployed URL (deployment.md §6),
   recording the deployed commit SHA.

## Known limitations

- Administrators have no roles; client/task edits are recorded as "admin"
  activity without the individual's identity (job retries and reconciliation
  confirmations do record the administrator's email).
- No onboarding email sending (the admin copies the link); no file uploads.
- ClickUp: personal token, no OAuth. Slack: incoming webhooks only.
- One-way sync to ClickUp/Sheets; no inbound webhooks.
- See [architecture.md](architecture.md#known-limitations) for the full list.
