# Infrastructure comparison

Prepared 2026-11-11 for one agency deployment of Lead-to-Delivery: one public
API (FastAPI, also serving the built React app), one always-on background
worker, managed PostgreSQL 16, HTTPS with a custom domain, automated backups,
logs/monitoring and secret management. Prices are from the providers' own
pages on 2026-11-11 (sources at the end); re-check before purchase.

## What this application needs from a platform (from the code)

| Requirement | Why | Consequence |
|---|---|---|
| Always-on worker | `python -m app.jobs.worker` polls every `WORKER_POLL_SECONDS` (2 s) | No platform that sleeps services; a serverless database that scales to zero will **never** scale to zero (it is polled every 2 s) |
| Direct (session) PostgreSQL connections | Pool sets session `statement_timeout`; psycopg prepares statements; migrations take a **session** advisory lock | Use the provider's direct/unpooled URL, not a transaction-mode pooler |
| ≈ 16 connections | API: uvicorn workers × pool max 5; worker: 5; release: 1 | Any plan with ≥ 25 connections |
| Node 22+ at build time | `npm ci && npm run build` produces `frontend/dist`, served by the API | The API build must have Node and Python, or the frontend is built elsewhere |
| Trusted proxy configuration | Login throttling keys on the client address | The platform must expose a stable private proxy range to trust (see preflight) |
| Separate migration credentials | Least privilege: `ltd_owner` migrates, `ltd_app` runs | Ideally migrations run outside the service's environment |

## Options

### 1. Render (web service + background worker + Render Postgres) — **recommended**

| Item | Plan | Price |
|---|---|---|
| Workspace | Hobby (1 seat, 5 GB bandwidth) | $0 |
| API web service | 0.5 CPU / 512 MB (`0.5c-512mb`) | $7 |
| Worker | 0.5 CPU / 512 MB background worker | $7 |
| PostgreSQL | `0.1c-256mb` (100 connections) | $6 |
| DB storage | ~1–2 GB at $0.30/GB | ~$0.30–0.60 |
| **Total** | | **≈ $20–21/month** |
| Headroom option | Postgres `0.5c-1g` ($19) | ≈ $33–34/month |
| Longer PITR | Pro workspace (+$25) → 7-day PITR | ≈ $46–59/month |

- Backups: continuous PITR on paid databases — **3 days on Hobby**, 7 days on
  Pro; restore creates a new database; on-demand logical exports kept 7 days.
  Free databases have no backups and expire after 30 days.
- Worker: native background-worker service type (paid only).
- Releases: pre-deploy command (paid) runs on a separate instance before the
  new version starts; failure aborts the deploy and keeps the old version.
  Zero-downtime deploys; SIGTERM with 30 s default (max 300 s) shutdown delay.
- Free tier: web services spin down after 15 min; no workers; Postgres
  expires after 30 days → **unsuitable**.
- Risks: Node availability in the Python build is unconfirmed; the
  pre-deploy command shares the service's environment (owner credentials);
  Render's proxy appends to `X-Forwarded-For` (client-forgeable left side).
- Lock-in: low (plain PostgreSQL + `pg_dump`; standard processes).

### 2. Render compute + Neon PostgreSQL

| Item | Price |
|---|---|
| API + worker on Render | $14 |
| Neon Launch: 0.25 CU always awake (the worker polls) ≈ 182.5 CU-h × $0.106 | ≈ $19.35 |
| Neon storage ~1 GB × $0.35 | ≈ $0.35 |
| **Total** | **≈ $34/month** |

- **Neon Free is not viable:** 100 CU-hours/month; a 0.25 CU compute kept awake
  by the worker uses that in ≈ 17 days, after which Neon suspends compute
  until the next period — a guaranteed mid-month outage.
- Backups: 7-day history (PITR) on Launch, snapshots extra.
- Connection pooling is PgBouncer-style; this app must use the direct URL.
- Cross-provider traffic over the public internet (TLS required, more latency
  unless regions match).
- Advantage: branching, longer PITR than Render Hobby, independent DB vendor.

### 3. DigitalOcean App Platform + Managed PostgreSQL

| Item | Price |
|---|---|
| API web component 1 vCPU / 512 MiB | $5 |
| Worker component 1 vCPU / 512 MiB | $5 |
| Managed PostgreSQL 1 GiB / 1 vCPU / 10 GiB | $15.15 |
| **Total** | **≈ $25/month** |

- Backups: daily full backups + 7-day point-in-time recovery, included.
- Strong value (1 GiB database, 7-day PITR) and a real managed database.
- App Platform has pre-deploy jobs and worker components; free tier is static
  sites only.
- Unknowns: Node+Python in one build, proxy/client-IP behaviour (App Platform
  documents its own connecting-IP header, which uvicorn does not read).
- Close second; preferred if a 7-day backup window matters more than reusing
  an existing Render account.

### 4. Railway (Hobby)

| Item | Price |
|---|---|
| Hobby subscription (includes $5 usage) | $5 |
| Usage: API + worker + Postgres container (~1–1.5 GB RAM total at ~$10/GB-month, low CPU) | ≈ $10–18 |
| **Total** | **≈ $15–23/month (usage-based, variable)** |

- Railway's default PostgreSQL is a template container on a volume ("a single
  node that Railway maintains"); HA is an optional conversion; backups are
  volume backups (daily kept 6 days, weekly 27, monthly 89); PITR is not the
  default. Less "managed" than options 1–3.
- Free trial: $5 one-time credit for 30 days; Free plan ($1/month credit,
  0.5 GB per service, no custom domains) — not viable.
- Cost is the least predictable of the four.

## Recommendation

**Render: one web service (API + built frontend), one background worker,
Render Postgres in the same region, Hobby workspace — ≈ $20–21/month**,
upgrading the database to `0.5c-1g` ($19) when the 256 MB instance becomes a
constraint.

Why:
1. All three components on one private network, one dashboard, one bill; the
   database is managed with continuous PITR.
2. Native background-worker service type matching `app.jobs.worker`; zero-
   downtime deploys and a configurable shutdown delay suit the worker's
   finish-the-current-job shutdown.
3. Cheapest option that is genuinely managed and has automated backups;
   Neon would cost more for this always-on workload, and Railway's database is
   less managed.
4. A previous Render-based runbook exists in another project, which suggests
   an account may already exist (to be confirmed; Lead-to-Delivery must not
   reuse that project's services, databases or secrets).

Accepted trade-offs: 3-day PITR on Hobby (mitigated by a logical export
before every release and a weekly off-platform `pg_dump`), and three platform
behaviours that must be verified before go-live (see
[deployment-preflight.md](deployment-preflight.md)): Node in the build, the
trusted proxy range, and creating the restricted `ltd_app` database user.

If a 7-day backup window is a hard requirement without paying for Render Pro,
choose option 3 (DigitalOcean, ≈ $25/month).

## Sources (retrieved 2026-11-11)

- Render pricing: https://render.com/pricing (machine-readable: https://render.com/pricing.md)
- Render free instances: https://render.com/docs/free
- Render Postgres backups/PITR: https://render.com/docs/postgresql-backups
- Render deploys, pre-deploy, shutdown: https://render.com/docs/deploys
- Render Postgres credentials: https://render.com/docs/postgresql-credentials
- Render monorepo support: https://render.com/docs/monorepo-support
- Render client IP behaviour (community, unofficial): https://community.render.com/t/accessing-client-ips-in-a-node-express-app/36282
- Neon pricing: https://neon.com/pricing
- Railway pricing: https://railway.com/pricing
- Railway PostgreSQL: https://docs.railway.com/databases/postgresql
- Railway backups: https://docs.railway.com/reference/backups
- DigitalOcean App Platform pricing: https://www.digitalocean.com/pricing/app-platform
- DigitalOcean Managed Databases pricing: https://www.digitalocean.com/pricing/managed-databases
- DigitalOcean PostgreSQL backups: https://docs.digitalocean.com/products/databases/postgresql/details/features/
