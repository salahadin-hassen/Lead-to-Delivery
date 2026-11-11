# Security

The security model as implemented (2026-11-10), with its limits.

## Threat model (summary)

| Asset | Threat | Control |
|---|---|---|
| Admin console (all client data, retries) | Password guessing, phishing, stolen session | Individual accounts, scrypt passwords, mandatory TOTP (or single-use recovery code), throttling per source/account/global, HttpOnly SameSite=Strict session cookie, 12 h / 60 min idle expiry, revocation |
| Client portal | Link leakage, guessing, cross-client access | 256-bit token, stored only as SHA-256, expiring and revocable, scoped to one invitation; token never in a server-visible URL |
| CRM webhook | Forged or replayed deals | HMAC-SHA256 over the exact raw body + timestamp (±5 min), constant-time compare, idempotency on `(source, event_id)` with payload-mismatch rejection |
| Integration credentials | Disclosure via API, logs, errors | `SecretStr` settings, booleans only in status endpoints, error redaction in job attempts, no request bodies in logs |
| Browser | XSS, clickjacking, referrer leaks | React text rendering (no raw HTML), strict CSP, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, no third-party scripts or analytics |

## Administrator authentication

- **Accounts:** `admin_users`, one per person, created only on the server with
  `python -m app.admin_users` (passwords via `getpass`, at least 12
  characters, never as arguments). No default credentials.
- **MFA (mandatory for every account):** TOTP, SHA-1/6 digits/30 s, ±1 step;
  the highest accepted step is stored, so a code is never accepted twice.
  Enrolment requires a correct code before anything is saved. Secrets are
  AES-256-GCM encrypted with `ADMIN_MFA_ENCRYPTION_KEY` (held outside the
  database). 10 recovery codes per account, ~74 bits each, single use, stored
  as SHA-256 only; each use is logged with the number left.
- **Login:** one request with email, password and code. Every failure is the
  same 401; unknown emails still cost one scrypt check (no account
  enumeration by timing).
- **Throttling:** 5 failures per source address and 5 per account name (since
  the last success), and 50 in total, per 15 minutes → 429 with
  `Retry-After`. Stored in PostgreSQL (survives restarts, shared across API
  processes).
- **Sessions:** 256-bit token, SHA-256 in `admin_sessions`, bound to the
  administrator; new token at every login; 12 h maximum, 60 min idle; ended at
  once when the account is disabled; `POST /api/admin/logout-all` and
  `app.admin_users revoke-sessions` revoke all of an administrator's sessions.
- **Cookie:** `ltd_admin_session`, HttpOnly, SameSite=Strict, Path=/api/admin,
  Secure in production.
- **CSRF:** non-GET admin requests need `X-Requested-With: XMLHttpRequest`
  (forces a CORS preflight, which fails: no CORS is configured), a JSON body,
  and a matching `Origin` when present; plus SameSite=Strict.
- **Recovery:** `reset-password` and `reset-mfa` revoke the account's sessions;
  `new-recovery-codes` invalidates old codes; `disable`/`enable`.

Limits: no roles (every administrator has full access), no self-service
account management in the UI, no WebAuthn/passkeys.

## Client portal

- Link `APP_BASE_URL/portal#t=<token>`. The fragment is never sent to the
  server or in a `Referer`. The page reads it once, removes it from the
  address bar, keeps it in memory only (no localStorage/sessionStorage/
  cookies), and sends it only as `Authorization: Bearer` with
  `credentials: 'omit'`, `referrerPolicy: 'no-referrer'`, `cache: 'no-store'`.
  Reloading needs the link again (deliberate).
- Server side: token format checked, only its hash is queried, expiry and
  revocation enforced, every read scoped to the invitation and client;
  another project's ID returns 404; responses are explicit allow-listed
  models (no internal fields); `Cache-Control: no-store`.
- Admin endpoints never accept a portal token (verified by test and on
  staging).

## Transport, origin and headers

- HTTPS terminated at the proxy; HSTS in production.
- One origin for app and API (`FRONTEND_DIST_DIR`); no CORS anywhere.
- `ALLOWED_HOSTS` (production default: the `APP_BASE_URL` host; wildcards
  refused). Health probes are exempt.
- Headers on every response: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, COOP/CORP
  same-origin, `Permissions-Policy`, CSP `default-src 'none'` for API
  responses and a self-only CSP for the app (no inline scripts or styles).
- Request bodies: 1 MiB global cap; webhook 64 KiB, portal 32 KiB, admin
  64 KiB. Validation errors never echo input.
- Production does not serve `/docs`, `/redoc` or `/openapi.json`.
- Proxy: run uvicorn with `--forwarded-allow-ips` set to the proxy only; the
  application never reads `X-Forwarded-For` itself.

## Secrets

- Required in production: `ADMIN_MFA_ENCRYPTION_KEY`, `CRM_WEBHOOK_SECRET`
  (32+ chars), database credentials. Optional: `CLICKUP_API_TOKEN`,
  `SLACK_WEBHOOK_URL` (the URL is the secret), Google service-account key file.
- Never in Git (`.gitignore`, secret scans before every commit), never logged,
  never returned by status endpoints.
- Rotation: see [operations-runbook.md](operations-runbook.md#secret-rotation).
- Demo secrets (n8n relay demo, staging QA) must never be used in production.

## Verification evidence (2026-11-10)

Backend tests cover all of the above (see [production-readiness.md](production-readiness.md)
for counts). On staging, in Google Chrome: token only in the Authorization
header, never in a URL or referrer; no web storage; no third-party requests;
no console errors under the strict CSP; admin sign-in with TOTP and with a
recovery code (second use refused); logs contained no secret, token, link or
email address.

## Known risks / not done

- No external penetration test has been performed. Dependency audit
  2026-11-11: `pip-audit` on the installed backend environment and
  `npm audit` on the frontend found no known vulnerabilities. All Python
  dependencies, transitive included, are pinned with hashes in
  `backend/requirements.lock` / `requirements-dev.lock`
  ([build-and-release.md](build-and-release.md)).
- Login throttling's per-source limit depends on correct proxy trust on the
  hosting platform (preflight D1).
- `ADMIN_MFA_ENCRYPTION_KEY` loss forces every administrator to re-enrol MFA.
- A compromised server (env + database) compromises everything; there is no
  HSM/KMS integration.
- Login throttling is per process group sharing one database; there is no
  edge rate limiting (add it at the proxy/platform if exposed widely).
