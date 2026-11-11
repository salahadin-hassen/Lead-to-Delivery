# Free staging ($0/month)

Budget constraint: **$0 recurring**, absolute. No payment method on any
account, no expiring trials or credits, nothing that can upgrade or bill
automatically. Researched 2026-11-11 from the providers' own pages (sources
at the end). Nothing has been created or deployed. The paid Render design
(≈ $20.30/month) is **not approved** and is kept only as a reference in
[render-staging.md](render-staging.md) and
`deploy/render/render.paid-staging.yaml`.

## Verdict

No verified option gives persistent, always-available hosting of the API, a
continuously running worker and a durable managed PostgreSQL with real
backups at $0.

**Selected: intermittent $0 staging — Render Free (Docker API) + Neon Free
(PostgreSQL, direct connection, two roles) + a manually started, time-boxed
worker in GitHub Actions** ([staging-worker.md](staging-worker.md)).

What it is: **publicly reachable, application-authenticated staging using
synthetic data only.** It is **not** private: the Render Free web service and
the Neon endpoint are reachable from the internet; access is controlled by
the application (admin MFA sessions, invitation tokens, webhook HMAC) and by
database credentials over TLS. An obscure URL is not access control. It is
**not** production hosting.

## 1. Security: what an unauthenticated internet user can do

Enumerated from the code (every router in `backend/app/routes/` and
`backend/app/web.py`), production mode:

| Route | Guard | Unauthenticated user can |
|---|---|---|
| `GET /`, `/admin`, `/admin/*`, `/portal`, `/portal/*`, `/assets/*` | none (static) | download the built React app (code only, no data) |
| `GET /docs`, `/redoc`, `/openapi.json` | disabled in production | nothing (404) |
| `GET /api/health` | none | read `{"status":"ok"\|"unavailable"}`; **wakes the instance and the database** |
| `GET /api/health/worker` | none | read `ok` / `no_worker`; wakes both |
| `POST /api/webhooks/crm/deal-won` | HMAC-SHA256 over the exact body + 5-min timestamp window, checked **before** parsing or any database work; 64 KiB cap | get 401/415/413; create nothing |
| `GET/POST /api/portal/*` (`me`, `onboarding`, `project`, `projects/{id}`) | `Authorization: Bearer` 256-bit invitation token (format check, then hash lookup, expiry, revocation, invitation scope) | get 401; each well-formed guess costs one database lookup |
| `POST /api/admin/login` | CSRF header + JSON; email + password + TOTP/recovery code; throttling | try credentials (generic 401; throttled after 5 failures) |
| `POST /api/admin/logout` | CSRF header | revoke nothing (no session) |
| Every other `/api/admin/*` (dashboard, clients, invitations, projects, tasks, activity, webhook events, integration jobs/status, logout-all, me) | admin session (MFA login) + CSRF on writes | get 401 |

**No client record, onboarding submission, project detail or admin data is
returned without a valid admin session or invitation token** (also covered
by the backend tests: every admin endpoint 401 without a session; portal
tokens cannot reach admin endpoints; cross-invitation project access 404).

**Shared login-throttling bucket.** Render's proxy is not a trusted proxy
(`FORWARDED_ALLOW_IPS=127.0.0.1`, [render-staging.md](render-staging.md)
§4), so every request appears to come from the proxy's address. Anyone can
make five failed logins and block admin sign-in for **everyone** for up to
15 minutes (per-account and global limits still apply). This is a denial of
service, not a bypass: nothing is exposed.

**Availability and quota abuse.** Any request (health checks, invalid tokens,
bad signatures, login attempts) wakes the Render instance and usually the
database. Sustained traffic can exhaust Render's 750 instance hours or
Neon's 100 CU-hours; the services are then **suspended until next month** —
no charge, since no payment method exists.

**Assessment:** sufficient for **synthetic-data-only** staging: no data is
reachable without authentication, and the worst outcomes are lockouts and
quota exhaustion. Not sufficient for real client data or a public pilot
(proxy trust still unresolved).

**Optional extra layer:** Cloudflare Access (Zero Trust free plan) in front
of the service would require a domain on Cloudflare and DNS changes, and
whether it can be enabled without a payment method was not verified. Not
required for synthetic-data staging; not added.

## 2. What the application needs (checked in the code)

| Property | Value |
|---|---|
| API memory | ≈ 50 MB RSS for one uvicorn process (measured locally, no database connected; a live pool adds a little) — set `WEB_CONCURRENCY=1` on the 0.1-CPU free instance |
| Worker memory | ≈ 36 MB RSS (same conditions) |
| Connections | pool min 1 / max 5 per process → API ≤ 5, worker ≤ 5 (Neon direct limit 105) |
| Database features | session `statement_timeout`, session advisory lock (migrations), transaction advisory locks, prepared statements → **direct (non-pooler) connection required** |
| Worker | polls every 2 s and heartbeats every 15 s while running, so it must not run continuously on Neon Free; stops gracefully on SIGTERM; a killed job is recovered by its 120 s lease (`unknown`, reconciliation before retry) |
| Bounded worker runs | no code change: the existing SIGTERM handling + `docker stop --time 120` in the workflow |
| CPU architecture | amd64 (CI-built); arm64 also supported by both base images and all 30 locked runtime packages |
| Runtime privileges | `SELECT/INSERT/UPDATE/DELETE` on tables only (no sequences, no DDL, no extensions) |

## 3. Provider limits (official pages, 2026-11-11)

**Render Free** ([Free](https://render.com/docs/free), [compute plans](https://render.com/docs/compute-plans), [regions](https://render.com/docs/regions), [pricing](https://render.com/pricing.md)):
- Web services only — **no free background worker**. Docker supported; 0.1 CPU / 512 MB.
- **Publicly reachable** over HTTPS on `*.onrender.com`; free web services
  cannot receive private-network traffic.
- 750 free instance hours **per workspace per calendar month**; when used up,
  all free web services are suspended until next month.
- Spins down after **15 minutes** without inbound traffic; spin-up **about
  one minute**; may be restarted at any time.
- **Ephemeral filesystem** (lost on redeploy, restart or spin-down) — the
  app keeps nothing on local disk.
- Workspace includes 5 GB outbound bandwidth and 500 build-pipeline minutes
  per month (Hobby). Beyond that Render bills **only if a payment method
  exists**; **without one, free services are suspended (bandwidth) or new
  builds disabled (build minutes) for the rest of the month**.
- Free services can be suspended for uncommonly high **service-initiated**
  traffic (outbound traffic, which includes database traffic to Neon).
- No shell/SSH, one instance, no persistent disk, no one-off jobs; outbound
  SMTP ports blocked. Free Render Postgres (30-day expiry, no backups) is
  not used.
- Region fixed at creation.

**Neon Free** ([plans](https://neon.com/docs/introduction/plans), [scale to zero](https://neon.com/docs/introduction/scale-to-zero), [pooling](https://neon.com/docs/connect/connection-pooling), [roles](https://neon.com/docs/manage/roles), [IP Allow](https://neon.com/docs/introduction/ip-allow), [regions](https://neon.com/docs/introduction/regions), [restore](https://neon.com/docs/introduction/branch-restore)):
- **100 CU-hours per project per month** (≈ 400 h at 0.25 CU); **1 GB**
  storage per project; **5 GB** public network transfer per month.
- **Six-hour instant-restore history**, capped at 1 GB of change history;
  **one manual snapshot**. Restore overwrites the branch in place and keeps
  a backup branch. *Not a backup strategy for real data.*
- When the CU-hours or transfer are used up, compute is **suspended** until
  the next period (no charge); storage over the cap blocks writes.
- **Scale to zero after 5 minutes** of inactivity (fixed on Free); resumes in
  a few hundred milliseconds.
- **Direct connection required** (host without `-pooler`); the pooled
  endpoint is transaction-mode and breaks session settings and session
  advisory locks. 112 direct connections at 0.25 CU (105 usable).
- **IP Allow is a Scale-plan feature.** A Free project **cannot** be limited
  to the operator's IP: the endpoint is reachable from the internet, guarded
  only by role credentials and TLS. Consequences: use long random
  passwords; never put the owner credentials anywhere but the operator's
  machine; rotate the owner password after setup; treat the data as
  disposable.
- The owner role (member of `neon_superuser`, not a true superuser) can
  `CREATE ROLE`; SQL-created roles get only explicit grants; passwords need
  ≥ 60 bits of entropy.
- **Passwords:** Neon documents that a password set in SQL "must be supplied
  in plain text … Hashed passwords are not supported", so psql's
  `\password` (which sends a SCRAM verifier) is not a Neon path. Roles made
  in the Console or API, and by `neon roles create` **without** `--access`,
  are administrators in `neon_superuser`. `neon roles create --access
  read_write` makes a scoped role outside `neon_superuser` and prints a
  control-plane-generated password once; `neon roles reset-password`
  rotates it the same way. ([Manage roles](https://neon.com/docs/manage/roles),
  [CLI roles](https://neon.com/docs/reference/cli-roles); not yet exercised
  against a real Neon project.)
- Regions include `aws-eu-central-1` (Frankfurt); region fixed at creation.
- A card appears only for optional prepaid "AI Gateway" credits — never buy
  them.

**Others** (not selected): Supabase Free (500 MB, paused after 7 idle days,
**no backups or PITR**, IPv6 direct / IPv4 session pooler); Oracle Always Free
(A1 2 OCPU / 12 GB; **card + authorization hold**; idle reclamation; Ethiopian
eligibility unconfirmed); Cloudflare Quick Tunnels (testing only, random URL)
for a local machine; GitHub Actions alone (nothing persists).

## 4. Comparison

| | A. Render Free + Neon Free + Actions worker | B. Oracle Always Free | C. Render Free + Supabase Free | D1. Local + Quick Tunnel | D2. Actions only |
|---|---|---|---|---|---|
| Class | **Intermittent $0** | Persistent $0 *if* eligible and not reclaimed | Intermittent $0 | Local/private $0 | Temporary demo |
| Card | No | **Yes** + hold | No (Render); Supabase not stated | No | No |
| Separate worker | Manual, time-boxed | Continuous | Manual | Continuous | During run |
| Database | Neon, durable, 6 h restore | self-managed | durable, weekly pause, **no backups** | local | none |
| Direct DB | Yes (TLS) | Yes | session pooler | Yes | Yes |
| Network | **public** app + public DB endpoint | public VM | public | tunnel while running | n/a |
| Sleep | API ~1 min cold start; DB resumes in ms | none | API + weekly DB pause | machine on/off | n/a |
| Burden | low–medium | high | low–medium | low | low |
| Failure modes | quota suspension, lockouts, cold starts | reclamation, capacity, card | pauses, no backups | machine off | nothing persists |

## 5. Setup outline (manual, by the owner)

Region: **Frankfurt for both** — Render `frankfurt` and Neon
`aws-eu-central-1` — **if each dashboard offers it for the free plan at
creation** (neither provider's documentation says whether every region is
available on its free plan). If either does not, choose another matching
pair (e.g. Render `ohio` + Neon `aws-us-east-2`, or Render `virginia` + Neon
`aws-us-east-1`). Both regions are fixed once created; never create
resources just to look.

1. **Accounts:** Render and Neon accounts **without a payment method**;
   check both billing pages before and after setup.
2. **Neon project** (Free, chosen region), database `ltd`. Copy the owner
   connection string (direct, `sslmode=require`) into the password manager
   only.
3. **Application role, created by Neon's control plane** (operator's own
   terminal, logged in with `neon auth`; never in CI, never redirected to a
   file or pasted into chat), **before** any migration:

   ```bash
   neon roles create --name ltd_app --access read_write --database ltd --schema public \
     --project-id <project-id> --branch <branch-id>
   ```

   - `--access read_write` is **mandatory**: the default (`admin`), the
     Console and the API create a `neon_superuser` member, which the helper
     refuses.
   - `--schema public` is explicit (also the CLI default); project and
     branch IDs are always given, so the role lands on the intended branch.
   - Neon generates the password and prints it once: save it **directly
     into the password manager**, then clear the terminal scrollback. It
     never passes through SQL you write, a command line or an environment
     variable.
   - **Subject to live verification:** CLI availability on the Free plan
     (the docs state no plan restriction) and the privileges the CLI
     actually grants (only "SELECT; read_write adds INSERT, UPDATE, DELETE"
     is documented). The helper inspects the role before changing or
     granting anything, so a role that differs from the documentation is
     narrowed or refused, never trusted.
4. **Narrow, migrate, prove** with
   [`scripts/neon-staging-setup.sh`](../scripts/neon-staging-setup.sh) (works
   from any directory; owner URL typed at a hidden prompt, held only in a
   temporary 0600 passfile; refuses localhost, non-Neon and pooled hosts,
   any database but `ltd`, and URLs without `sslmode=require`; shows the
   target host and role and asks for `yes`):
   `scripts/neon-staging-setup.sh roles`, then `migrate`, then `verify` (with
   the `ltd_app` URL). `roles` narrows `ltd_app` in one transaction —
   `CONNECTION LIMIT 20`, PUBLIC loses `TEMP`/`CREATE`, then
   [`deploy/db-app-grants.sql`](../deploy/db-app-grants.sql) removes every
   privilege the CLI granted and grants exactly the policy — and
   [`deploy/db-app-check.sql`](../deploy/db-app-check.sql) proves the exact
   matrix (no role membership such as `neon_superuser`, no
   superuser/createrole/createdb/replication/bypassrls, owns nothing, exact
   table/sequence/schema/database/default privileges). If the role has
   rights the owner cannot remove (superuser, replication, bypassrls or any
   membership), the helper changes nothing and says to `neon roles delete
   ltd_app` and recreate it. `migrate` repeats the narrowing, applies the
   migrations, re-applies the grants and re-runs the check; `verify` (with
   the `ltd_app` URL) runs the check plus DDL-refusal and DML probes.
   **Rotation:** `neon roles reset-password ltd_app --project-id <project-id>
   --branch <branch-id>` (drops open connections), update the two places
   that hold the `ltd_app` URL (step 6, step 8), then `verify`.

   Regression tests (disposable local PostgreSQL with Neon's control plane
   **simulated** — not a Neon test): `scripts/tests/neon-setup/run.sh`
   (needs PostgreSQL 16+ server binaries, `psql`, `openssl`,
   `backend/.venv`; set `PG_BIN`/`PORT` if needed). It builds a throwaway
   TLS cluster on its own port and socket, runs 37 cases (URL refusals,
   refusal of `neon_superuser`/BYPASSRLS/REPLICATION roles with nothing
   changed, narrowing of a broad role, idempotence, drift detection by
   `verify`, the worker workflow's preflight and `admin`, rotation, API and
   worker as `ltd_app`, no password in the statement log) and deletes it.
5. **Rotate the owner password** in Neon after setup (and after every later
   migration session) so the credential used on the operator machine does
   not stay valid; store the new one only in the password manager.
6. **Render Free web service** (dashboard, no Blueprint): Docker,
   `./Dockerfile`, branch `main`, instance type **Free**, health check
   `/api/health`, auto-deploy off, region as above. Variables:
   `APP_ENV=production`, `APP_BASE_URL=https://<service>.onrender.com`,
   `DATABASE_URL` = `ltd_app` **direct** Neon URL with `sslmode=require`,
   `ADMIN_MFA_ENCRYPTION_KEY` and `CRM_WEBHOOK_SECRET` (new staging-only
   values), `WEB_CONCURRENCY=1`, `FORWARDED_ALLOW_IPS=127.0.0.1`. No
   integration variables.
7. **First administrator** from the operator machine:
   `scripts/neon-staging-setup.sh admin you@example.com "Your Name"`
   (`ltd_app` URL, then the API's MFA key at a hidden prompt read inside the
   admin tool; refuses unless `ltd_app` passes the exact policy check).
8. **Worker:** configure the GitHub environment and run the manual workflow
   as described in [staging-worker.md](staging-worker.md). It receives only
   the `ltd_app` URL.
9. **Synthetic data only** (render-staging.md step 11).

Secret placement summary:

| Secret | Operator machine (password manager) | Render (API) | GitHub `staging-worker` environment |
|---|---|---|---|
| Neon owner URL/password | yes, only here | **never** | **never** |
| `ltd_app` URL | yes | `DATABASE_URL` | `STAGING_LTD_APP_DATABASE_URL` |
| `ADMIN_MFA_ENCRYPTION_KEY` | yes (needed for the admin tool) | yes | no (worker generates throwaway values per run) |
| `CRM_WEBHOOK_SECRET` | yes (to sign synthetic webhooks) | yes | no (throwaway per run) |
| Admin password, TOTP, recovery codes | yes (codes offline) | no | no |

Do not attach an uptime monitor: it would keep both services awake and burn
the free hours.

## 6. Financial safety controls

| Provider | Card | Can usage exceed free? | Automatic charge? | Hard stop | Remove / confirm |
|---|---|---|---|---|---|
| Render | none | yes (hours, bandwidth, build minutes) | only with a payment method | **no payment method** → suspension / builds disabled | delete the service; Billing shows no payment method and only Free instances |
| Neon | none | yes (CU-hours, transfer, storage) | no on Free | Free-plan limits suspend compute / block writes; never upgrade or buy credits | delete the project; Billing shows Free |
| GitHub Actions | none | not for public-repo standard runners | no without a payment method | blocked beyond the included amount | disable/delete the workflow; Billing shows $0 |

A budget alert is not a spending cap; nothing here relies on one. Check the
three billing pages before setup, after setup, and monthly.

## 7. Deleting the staging resources

Render: service → Settings → Delete Web Service. Neon: project → Settings →
Delete project (all data and history). GitHub: Settings → Environments →
delete `staging-worker` (removes its secrets); optionally disable the
workflow. Then confirm each billing page shows nothing active.

## 8. What this staging can and cannot validate

**Can:** the production image on a real platform over HTTPS; production
configuration (fail-closed settings, Secure cookie, HSTS, CSP, host
allowlist, no docs); admin sign-in with MFA; the client portal end to end;
the signed webhook and replay; migrations with separated owner/app roles on a
managed PostgreSQL over direct TLS connections; job creation, on-demand job
processing, retries/finalisation and graceful worker stop; logs free of
secrets.

**Cannot:** a continuously running worker and `/api/health/worker` (reads
`no_worker` except during a run); worker behaviour under platform deploys;
always-on availability; production capacity (0.1 CPU); private networking
(app ↔ database is over the internet); restore beyond 6 hours or a realistic
backup drill; monitoring/alerting; **proxy trust** (unresolved); real
integrations; network-level access restriction.

## 9. Blockers

- **P0:** none for synthetic-data staging, provided the owner never adds a
  payment method and the stop conditions in the setup hold.
- **P1 (before any public pilot or real data):** proxy trust
  (`CF-Connecting-IP` support); network-level protection of the database
  (paid IP Allow or another provider); real backups; monitoring.
- **P2:** worker validation needs throwaway MFA/webhook values; manual
  per-job retry; quota exhaustion by third-party traffic.
- **Unverified until provisioning:** free-plan region availability (both
  providers); Render Free building this Dockerfile within its allowance;
  the Neon CLI on the Free plan and the privileges `neon roles create
  --access read_write` really grants (first real `roles` + `verify` run;
  both fail closed); the first real workflow run.
