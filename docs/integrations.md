# Integrations

## CRM deal-won webhook (inbound)

**What this is:** a *generic*, signed, normalized webhook. Any CRM, or an
automation tool such as n8n placed in front of it, can call it when a deal is
won. Sample payloads labelled `gohighlevel` and `hubspot` show how data from
those CRMs looks **after** transformation into this contract. The application
does **not** connect to the GoHighLevel or HubSpot APIs, hold their
credentials, or receive their native webhook formats.

### Endpoint

```
POST /api/webhooks/crm/deal-won
Content-Type: application/json
X-LTD-Timestamp: <Unix time, seconds>
X-LTD-Signature: sha256=<hex HMAC>
```

- Body limit: 64 KiB (`413` if exceeded; also enforced without `Content-Length`).
- No external HTTP calls are made while handling the request.

### Signature

```
signature = HMAC_SHA256(CRM_WEBHOOK_SECRET, timestamp + "." + raw_request_body)
header    = "sha256=" + lowercase_hex(signature)
```

- Computed over the **exact bytes sent**. The server never re-serialises the
  JSON before checking, so re-formatting the body after signing breaks it.
- The timestamp must be within ±300 seconds of the server clock.
- Comparison uses `hmac.compare_digest`. Hex case is ignored.
- Failed authentication writes nothing to the database.

Example sender (bash + openssl), as used in the Task 2 verification:

```bash
TS=$(date +%s)
SIG=$(printf '%s.' "$TS" | cat - payload.json | openssl dgst -sha256 -hmac "$CRM_WEBHOOK_SECRET" -hex | sed 's/^.* //')
curl -X POST http://localhost:8010/api/webhooks/crm/deal-won \
  -H 'Content-Type: application/json' \
  -H "X-LTD-Timestamp: $TS" -H "X-LTD-Signature: sha256=$SIG" \
  --data-binary @payload.json
```

Python: `app.security.webhook_signature(secret, timestamp, body_bytes)`.

### Payload

See [examples/crm/](examples/crm/): `generic.json`, `gohighlevel-style.json`,
`hubspot-style.json` (all validated by the schema).

| Field | Required | Rules |
|---|---|---|
| `event_id` | yes | 1–200 chars, starts alphanumeric, then `A-Z a-z 0-9 . _ : -` |
| `event_type` | yes | exactly `deal.won` |
| `occurred_at` | yes | ISO 8601 **with** time zone; stored as UTC |
| `source` | yes | `generic`, `gohighlevel` or `hubspot` (trimmed, lower-cased) |
| `deal.external_id` | yes | 1–200 printable chars |
| `deal.name` | no | ≤ 300 chars |
| `deal.value` | no | 0 – 1,000,000,000, at most 2 decimals; stored as cents |
| `deal.currency` | if `value` given | 3 letters, upper-cased (`usd` → `USD`); format only, not an ISO list check |
| `deal.service_type` | yes | key of an **active** project template: `website_build`, `seo_retainer`, `marketing_automation_setup`. Unknown → 422; never defaulted |
| `contact.external_id` | one of these two | 1–200 printable chars |
| `contact.email` | one of these two | valid email; trimmed and lower-cased |
| `contact.name` | yes | 1–200 chars |
| `contact.company` | no | ≤ 300 chars |
| `contact.phone` | no | digits, spaces and `+ ( ) - .`, 3–40 chars |

Unknown fields are rejected (`extra` forbidden at every level).

### Responses

| Situation | HTTP | Body |
|---|---|---|
| New intake | 201 | `{"status":"created","client_id","invitation_id","onboarding_url","expires_at"}` |
| Same event delivered again (same normalized payload) | 200 | `{"status":"duplicate","event_id"}` |
| New `event_id`, deal already has an invitation | 200 | `{"status":"duplicate_deal","event_id"}` |
| `event_id` reused with a different payload | 409 | `{"detail","code":"event_id_reused","event_id"}` |
| Client identity conflict | 409 | `{"detail","code":"identity_mismatch" \| "crm_identity_conflict" \| "company_conflict" \| "client_archived","event_id"}` |
| Missing / malformed / wrong signature, bad timestamp | 401 | `{"detail","code":"missing_signature" \| "malformed_timestamp" \| "timestamp_out_of_window" \| "malformed_signature" \| "invalid_signature"}` |
| Wrong `Content-Type` | 415 | `{"detail","code":"unsupported_media_type"}` |
| Body too large | 413 | `{"detail","code":"payload_too_large"}` |
| Malformed JSON, invalid fields, unknown `service_type` | 422 | `{"detail":[{"loc","msg","type"}]}` (inputs are never echoed) |
| `CRM_WEBHOOK_SECRET` unset or shorter than 32 chars | 503 | `{"detail","code":"webhook_not_configured"}` |
| Concurrent conflict persisted after 3 attempts | 503 | `{"detail","code":"retry_later"}` |
| Database unavailable | 503 | `{"detail":"Service temporarily unavailable"}` |

`onboarding_url` is `APP_BASE_URL + "/portal#t=" + token`. The raw token
appears **only** in this 201 response; the database stores its SHA-256 hash.
Senders should forward the URL to the client and not log it. The token is in
the URL fragment, which browsers never send to servers. (The portal page
itself arrives in Task 3.)

### Processing and idempotency guarantees

Each delivery is handled in **one PostgreSQL transaction**: the event row,
client creation or update, invitation, token hash, activity records and the
event's final status commit together or not at all.

- **Event level:** `UNIQUE (source, external_event_id)` with
  `INSERT … ON CONFLICT DO NOTHING`. A replay with the same normalized payload
  returns `duplicate` and only increments `duplicate_count`. Payload
  comparison is on the normalized document, so email case, time-zone notation
  or `2500` vs `2500.00` do not count as differences. A different payload under
  the same `event_id` returns 409 and changes nothing.
- **Deal level:** `UNIQUE (crm_source, crm_deal_id)` on invitations. A deal
  resent under a new `event_id` returns `duplicate_deal`; the original
  invitation and its history are untouched, and nothing the duplicate request
  did besides recording its own event row survives (it runs in a savepoint
  that is rolled back).
- **Concurrency:** identical concurrent deliveries wait on the event's unique
  key; exactly one creates records (tested with 20 parallel requests).
  Concurrent deliveries of one deal under different event IDs produce one
  invitation (tested with 10). Concurrent new deals for one new contact produce
  one client (client insert uses `ON CONFLICT DO NOTHING` and re-resolves).
  A transaction that still loses a race (unique violation, deadlock) is
  retried whole, up to 3 times.
- **Crash safety:** an error anywhere rolls back everything, so the same event
  can simply be delivered again.

Stored event statuses: `processed`; `rejected` with `error_code`
`duplicate_deal` or an identity-conflict code. A delivery rejected for an
identity conflict is **re-evaluated** when the same event is delivered again,
so it succeeds once the conflicting data has been corrected. Requests that
fail authentication or validation are not stored.

### Client resolution

1. Match `(source, contact.external_id)` against `clients.crm_source/crm_external_id`.
2. Else match `contact.email` against `lower(clients.email)`.
3. Else create a client.

Names are never used for matching. Conflicts are rejected with 409, never merged:

| Code | Meaning |
|---|---|
| `identity_mismatch` | CRM ID and email match two different clients |
| `crm_identity_conflict` | email matches a client linked to a *different* contact ID in the same CRM |
| `company_conflict` | matched client has a different non-empty company (case and spacing ignored) |
| `client_archived` | the matched client is archived; restore it in the admin console, then the redelivered event is accepted |

For a matched client, existing non-empty fields are kept; only empty `email`,
`company` and `phone` are filled. A missing CRM identity is attached; a client
already linked to another CRM keeps that identity (one identity slot per
client). A returning client with a new deal gets a new invitation and token.

### Field mapping

| Contract field | `clients` / `onboarding_invitations` column |
|---|---|
| `source` | `clients.crm_source`, `onboarding_invitations.crm_source` |
| `contact.external_id` | `clients.crm_external_id` |
| `contact.name / email / company / phone` | `clients.name / email / company / phone` |
| `deal.external_id` | `onboarding_invitations.crm_deal_id` |
| `deal.name` | `onboarding_invitations.deal_name` |
| `deal.value` × 100 | `onboarding_invitations.deal_value_cents` |
| `deal.currency` | `onboarding_invitations.currency` |
| `deal.service_type` | `onboarding_invitations.service_type`, `template_id` |

Invitations start `pending` and expire after **30 days**
(`INVITATION_TTL_DAYS`); the token expires at the same moment.

Typical CRM sources for the normalized fields (indicative only; the
transformation is the sender's responsibility and is not verified against
provider APIs here): the deal/opportunity ID and name, its monetary value and
currency, a custom "service type" field or pipeline mapping, and the primary
associated contact's ID, name, email, company and phone.

### Activity recorded per successful intake

`crm.deal_received` (actor `crm`), then `client.created` or `client.matched`
(with `matched_by`), then `onboarding.invitation_created`. All are internal
(`client_visible = false`) and contain no tokens or contact details. Duplicate,
duplicate-deal and conflicting deliveries add no activity.

### Configuration

| Variable | Requirement |
|---|---|
| `CRM_WEBHOOK_SECRET` | ≥ 32 characters, in every environment; otherwise the endpoint returns 503. Generate: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `APP_BASE_URL` | Base of the onboarding URL (https in production) |

### Testing

```bash
cd backend
.venv/bin/python -m pytest -q tests/test_webhook_auth.py tests/test_crm_intake.py
```

Tests run against PostgreSQL `ltd_test` and make no network calls.

## Outbound integration jobs

Submitting onboarding (Task 3) writes these rows to `integration_jobs` in the
**same transaction** as the project, so they exist exactly when the project
does. The integration worker (`python -m app.jobs.worker`, see architecture.md)
executes them. The **ClickUp**, **Slack** and **Google Sheets** adapters are
registered (below). A job type without a registered adapter would stay
`pending`; it is never marked succeeded or failed for lack of an adapter.

| job_type | idempotency_key | coalesce_key | subject | payload |
|---|---|---|---|---|
| `clickup.create_task` (one per generated task) | `clickup.create_task:{task_id}` | none | `task` | `project_id`, template `assignee_role` |
| `sheets.upsert_project` | `sheets.upsert_project:{project_id}:{sync_version}` | `sheets.upsert_project:{project_id}` | `project` | `sync_version` |
| `slack.notify` | `slack.notify:project_created:{project_id}` | none | `project` | `event: project_created` |

- Keys are operation-specific and permanent; queuing the same key twice is a
  no-op (`ON CONFLICT DO NOTHING`).
- Sheets jobs coalesce: while one is **pending** for a project, a newer
  `sync_version` is not queued (the pending job reads current state when it
  runs). Running, succeeded or dead jobs never block a newer version.
- Jobs start `pending`, `attempts = 0`, `max_attempts = JOB_MAX_ATTEMPTS`,
  `next_run_at = now()`.
- Payloads carry identifiers only; the worker reads current data when it runs.

## ClickUp (`clickup.create_task`): implemented

**Status:** implemented and tested against a fake ClickUp API
(`httpx.MockTransport`, 63 tests including full worker runs). **Not yet
verified against the real ClickUp API**: no credentials were available; see
"Live test" below.

### Configuration

| Variable | Meaning |
|---|---|
| `CLICKUP_API_TOKEN` | Personal API token (ClickUp → Settings → Apps → API Token, starts with `pk_`). Sent as `Authorization: <token>` (no `Bearer`). Server-side only. |
| `CLICKUP_LIST_ID` | Numeric ID of the List tasks are created in (from the List's URL, `…/li/<id>`, or "Copy link"). |
| `CLICKUP_TIMEZONE` | IANA zone for date-only due dates, default `UTC`. |
| `HTTP_TIMEOUT_SECONDS` | Per-operation timeout (connect capped at 5 s); must be below `WORKER_LEASE_SECONDS`. |

A personal token acts as the person who created it: tasks show that user as
creator, it reaches every Workspace that user can see, and it stops working
if that user leaves. For customer-owned Workspaces, ClickUp's OAuth app flow
(a token per customer Workspace) is the better long-term option; it is not
implemented. Missing or invalid configuration does **not** stop the API or
the worker: each ClickUp job fails permanently with `CLICKUP_NOT_CONFIGURED`,
`CLICKUP_INVALID_LIST_ID`, `CLICKUP_INVALID_TOKEN_FORMAT` or
`CLICKUP_INVALID_TIMEZONE`, and can be requeued with `retry_dead_job` once
fixed.

### Request

`POST https://api.clickup.com/api/v2/list/{CLICKUP_LIST_ID}/task`

| ClickUp field | Source (local task is canonical) |
|---|---|
| `name` | task title (whitespace collapsed, truncated to 150 chars) + ` [LTD-TASK:<task uuid>]` |
| `description` | Project name, milestone, due date, suggested role "(unassigned)", the task's template description, and the marker again |
| `due_date` | 12:00 on the due date in `CLICKUP_TIMEZONE`, as Unix ms; omitted if no due date |
| `due_date_time` | `false` (date-only), sent only with `due_date` |

Not sent: status (the List's default applies), assignees (no user mapping
exists), client email/phone/name, onboarding answers, tokens, job payloads.
Noon in the agency zone keeps the same calendar date for viewers within ±11
hours of it and is unaffected by DST changes. **Assumption to verify live:**
that ClickUp shows a `due_date_time=false` date in the viewer's zone.

### Results

| Response | Outcome | Code |
|---|---|---|
| 2xx with valid `id` (and https `url`) | success → `tasks.clickup_task_id`, `tasks.clickup_url` | |
| 2xx without readable id/url | unknown | `CLICKUP_MALFORMED_RESPONSE` |
| 400 / 401 / 403 / 404 / other 4xx | permanent | `CLICKUP_BAD_REQUEST` / `CLICKUP_UNAUTHORIZED` / `CLICKUP_FORBIDDEN` / `CLICKUP_LIST_NOT_FOUND` / `CLICKUP_HTTP_<n>` |
| 429 | retryable; delay from `Retry-After`, else `X-RateLimit-Reset` − now, else backoff; capped at `WORKER_MAX_RETRY_DELAY_SECONDS` | `CLICKUP_RATE_LIMITED` |
| connect error, connect timeout, pool timeout (request not sent) | retryable | `CLICKUP_CONNECT_FAILED` |
| read/write timeout, protocol or read error, 408, 5xx (after POST) | **unknown** | `CLICKUP_CREATE_OUTCOME_UNKNOWN` |

Stored errors keep only ClickUp's short `ECODE` (e.g. `OAUTH_025`), never the
response body. No rate limit is hard-coded (limits vary by Workspace plan).

### Idempotency and reconciliation

1. If the local task already has `clickup_task_id`, nothing is sent; the job
   succeeds with that mapping.
2. If **any** earlier attempt of the job ended `unknown` (a lost response or
   an expired lease), the adapter first scans the List:
   `GET /list/{id}/task?page=N&include_closed=true&subtasks=true&include_markdown_description=true&date_created_gt=<task created − 10 min>`,
   100 tasks per page, pages from 0, at most 10 pages and within 60 % of the
   lease. A page is final when `last_page` is true. It looks for the exact
   marker in name, description, text content or markdown description.
   * one match → adopted (no POST), `integration.clickup_reconciled` activity;
   * several → unknown `CLICKUP_DUPLICATE_MARKER` (never picks one);
   * search incomplete (page limit or time budget) → unknown
     `CLICKUP_RECONCILIATION_INCOMPLETE`;
   * none, not confirmed → unknown `CLICKUP_RECONCILIATION_REQUIRED`;
   * none, confirmed → created, `integration.clickup_reconciled` activity.
   A failed search (GET timeout, 5xx) is retryable; because the unknown
   attempt stays in the job's history, the next attempt searches again.
3. Unknown results make the job `dead` (`UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION`).

**Manual reconciliation procedure.** Look in the List for the marker shown
in the job's task id (`[LTD-TASK:<subject_id>]`). Remove duplicates if any.
Then retry it from the admin console (Integrations → job → tick "I checked the
other system…" → Retry job), which calls `POST /api/admin/integration-jobs/{id}/retry`
with `reconciliation_confirmed: true` and so
`retry_dead_job(job_id, additional_attempts=…, reconciliation_confirmed=True, actor="admin")`.
The confirmation is stored durably in the job's payload
(`reconciliation.confirmed_after_attempt/confirmed_at/confirmed_by`) and in an
`integration.job_requeued` activity event, so it survives worker restarts.
It covers unknown attempts up to that point only; a later unknown needs a new
confirmation. The next attempt still searches first and adopts a match if one
appears.

### Live test

```bash
cd backend
CLICKUP_LIVE_TEST=1 .venv/bin/python -m app.integrations.clickup_live_check
```

Needs `CLICKUP_API_TOKEN` and `CLICKUP_LIST_ID` of a **dedicated test List**.
Runs on `ltd_test` only, and refuses while other ClickUp jobs are pending
there. It creates one synthetic task, checks the stored id/URL, re-runs the
worker (no new request) and confirms the reconciliation scan finds exactly
that task. The ClickUp task is left for inspection. **Status: not run;
blocked by missing credentials.** The script's steps were exercised against
the fake API only.

## Slack (`slack.notify`): implemented

**Status:** implemented and tested against a fake webhook endpoint
(`httpx.MockTransport`, 68 tests). **Not yet verified against real Slack**:
no webhook URL was configured; see "Live test".

### Setup

1. At api.slack.com/apps create an app ("From scratch") in the agency's
   workspace, open **Incoming Webhooks**, switch it on, and choose **Add New
   Webhook to Workspace**.
2. Pick the channel the team should be notified in (use a sandbox channel for
   testing). The webhook always posts there; the app never overrides it.
3. Copy the URL into `.env` as `SLACK_WEBHOOK_URL` (never into frontend
   variables, tickets, chat or Git). Rotate it in Slack if it is ever exposed.

The URL must be exactly `https://hooks.slack.com/services/T…/B…/<secret>`:
https only, host `hooks.slack.com`, no credentials, port other than 443,
query or fragment. Anything else fails the job with
`SLACK_INVALID_WEBHOOK_URL` and no request is made (this stops a mistyped or
tampered setting from turning the worker into a client for arbitrary hosts).
Unset → `SLACK_NOT_CONFIGURED`. Neither stops the API or other jobs.
Redirects are never followed.

### Message

Sent once per project, when onboarding creates it. Data is read from the
database (project, company, template, task count), never from the job payload.

| Element | Content |
|---|---|
| `text` (fallback / notification) | `New project ready: <project> (<company>)` |
| header | "New project ready" |
| fields (`plain_text`) | Project, Client (company only, omitted if none), Service, Tasks created, Project status, Due |
| context | "Onboarding is complete; the team can begin delivery." |
| button | "View project" → `APP_BASE_URL/admin/projects/<id>` |
| options | `unfurl_links: false`, `unfurl_media: false`; no channel, username or icon override |

Never included: contact names, emails, phones, onboarding answers, tokens,
CRM data. Dynamic text is single-line, ≤ 150 characters, with `&`, `<`, `>`
escaped and `@` before a letter broken by a zero-width space, so client text
cannot mention `@channel`/`@here`/people, create links or inject formatting.

### Results

| Response | Outcome | Code |
|---|---|---|
| 200 with body exactly `ok` | success (no message id exists; none is invented) | |
| 200 with anything else | **unknown** | `SLACK_UNEXPECTED_RESPONSE` |
| 4xx with a Slack error word (`invalid_payload`, `action_prohibited`, `channel_not_found`, `no_service`, `channel_is_archived`, …) | permanent | `SLACK_<WORD>` |
| other 4xx (including redirects) | permanent | `SLACK_HTTP_<n>` |
| 429 | retryable, delay from `Retry-After`, else backoff, capped at `WORKER_MAX_RETRY_DELAY_SECONDS` | `SLACK_RATE_LIMITED` |
| connect error (incl. DNS), connect timeout, pool timeout | retryable | `SLACK_CONNECT_FAILED` |
| read/write timeout, dropped connection, 408, 5xx | **unknown** | `SLACK_DELIVERY_UNKNOWN` |

Only Slack's short error word is stored, never the body. No workspace rate
limit is assumed.

### Unknown outcomes, retry and resolution

An unknown result makes the job `dead` (`UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION`)
and nothing is resent automatically: a webhook has no idempotency key and
cannot be searched, so a resend may duplicate the message. After an unknown
attempt (including a crashed worker whose lease expired) the adapter refuses
to post (`SLACK_DELIVERY_UNCONFIRMED`) until a person has checked.

In the admin console (Integrations → job), check the channel for the "New
project ready" message for that project, then:

* **Message is not there** → tick "I checked the channel and the message is
  not there" and **Retry job** (`POST /api/admin/integration-jobs/{id}/retry`
  with `reconciliation_confirmed: true`). The confirmation is stored in the
  job (`payload.reconciliation`) and the next attempt posts once.
* **Message is there** → tick "I found this project's … message" and **Mark
  as delivered** (`POST /api/admin/integration-jobs/{id}/resolve-delivered`
  with `{"message_found_in_channel": true}`). Nothing is sent; the job becomes
  `succeeded`, the unknown attempt stays in the history,
  `payload.resolution` records who/when, and an
  `integration.job_resolved_delivered` activity event is written. Only dead
  `slack.notify` jobs whose last attempt was unknown qualify (409 otherwise);
  ClickUp jobs reconcile by marker search instead and are refused.

The two confirmations are separate on purpose: "absent → retry" and
"present → resolve" can never be confused.

### Secret protection

The webhook URL is the credential. It is never returned by the API
(`/integrations/status` reports only booleans and an error code), stored in
jobs, attempts or activity, or included in error messages. httpx logs every
request URL at INFO; a logging filter on the `httpx` and `httpcore` loggers
replaces any Slack webhook URL, in full, with `<redacted-slack-webhook-url>`
before any handler sees it. Tests capture all loggers at DEBUG with a
synthetic secret and also confirm the secret *does* appear when the filter is
removed (so the test can detect a leak).

### Live test

```bash
cd backend
SLACK_LIVE_TEST=1 .venv/bin/python -m app.integrations.slack_live_check
```

Needs `SLACK_WEBHOOK_URL` for a **sandbox channel**. Runs on `ltd_test` only,
refuses while other Slack jobs are pending there, posts one synthetic message,
checks HTTP 200 + `ok` and the succeeded job, and re-runs the worker to show
nothing is resent. It never prints the URL. HTTP 200 does not prove the
message is visible; confirm in the channel. **Status: not run; blocked by
missing `SLACK_WEBHOOK_URL`.** The script's steps were exercised against the
fake endpoint only.

## Google Sheets (`sheets.upsert_project`): implemented

**Status:** implemented and tested against a fake Google token endpoint and
Sheets API (`httpx.MockTransport`, 78 tests; real RSA key and google-auth
signing). **Not yet verified against real Google**: no service account was
configured; see "Live test".

### Setup

1. In Google Cloud console create (or pick) a project and **enable the Google
   Sheets API** (APIs & Services → Library).
2. **IAM & Admin → Service accounts → Create service account** (no roles
   needed). Open it → **Keys → Add key → Create new key → JSON**. Store the
   file outside the repository (e.g. `~/.config/ltd/sheets-sa.json`,
   `chmod 600`) and set `GOOGLE_SERVICE_ACCOUNT_FILE` to its path. Never commit
   it, paste it into chat or put it in frontend variables.
3. Create the spreadsheet, add a worksheet named `Projects` (or set
   `GOOGLE_SHEETS_WORKSHEET`) and put this exact header in row 1 (A1:M1):

   `project_id | client_name | company_name | project_name | template_name | project_status | progress_percent | completed_tasks | total_tasks | start_date | due_date | clickup_url | updated_at`

4. **Share** the spreadsheet with the service account's email
   (`…@….iam.gserviceaccount.com`) as **Editor**.
5. Set `GOOGLE_SHEETS_SPREADSHEET_ID` (the `<ID>` in
   `https://docs.google.com/spreadsheets/d/<ID>/edit`).

Scope requested: `https://www.googleapis.com/auth/spreadsheets` only. Tokens
are obtained by google-auth (`service_account.Credentials`, JWT signed by the
library) through an httpx implementation of google-auth's transport
interface, cached in memory and refreshed when expired. The key file must be
a `service_account` key whose `token_uri` is `https://oauth2.googleapis.com/…`;
anything else is refused. Missing/invalid configuration affects only Sheets
jobs (`SHEETS_NOT_CONFIGURED`, `SHEETS_CREDENTIALS_FILE_NOT_FOUND`,
`SHEETS_INVALID_CREDENTIALS`, `SHEETS_INVALID_SPREADSHEET_ID`,
`SHEETS_INVALID_WORKSHEET_NAME`), never startup.

### The worksheet is system-managed

* Column A (`project_id`) is the row's identity; row numbers are never stored.
  Every sync reads A1:M1 and A2:A again and finds the project's row.
* The header must match exactly (surrounding spaces ignored); otherwise
  `SHEETS_SCHEMA_MISMATCH` and nothing is written. Worksheets are never
  created or renamed by the app (missing → `SHEETS_WORKSHEET_NOT_FOUND`).
* Sorting rows is safe. Do not edit column A or the header, and prefer
  **filter views** over moving rows while syncs run. Values written by people
  in a project's row are overwritten at the next sync.
* At most 10,000 project rows are searched; beyond that the search is
  incomplete and nothing is appended (`SHEETS_SCAN_LIMIT_REACHED`).

### Values

From PostgreSQL at the time of the sync (never from the job payload):
`project_id`, client name, company, project name, template name, project
status, progress percent / completed / total tasks (the same formula as the
portal and console: completed ÷ all tasks, floored; 0 tasks → 0), start and
due dates (ISO), the URL of the project's first ClickUp task if any (empty
until ClickUp has succeeded; it appears at the next sync), and `updated_at`
(latest change to the project or its tasks, UTC). No email, phone,
onboarding answers, tokens or CRM data. All writes use
`valueInputOption=RAW`, so text such as `=IMPORTXML(…)` stays text.

### Upsert

| Rows with this project_id | Action |
|---|---|
| 1 | `PUT values/'<sheet>'!A<row>:M<row>` with all 13 values |
| 0 (complete search) | `POST values/'<sheet>'!A:M:append` (`INSERT_ROWS`), unless an earlier append for this project is unresolved (below) |
| 2 or more | nothing written; permanent `SHEETS_DUPLICATE_PROJECT_ROWS`. Delete the extra rows, then retry |

### Ordering and concurrency

Each change queues `sheets.upsert_project:<project>:<sync_version>` with
coalesce key `sheets.upsert_project:<project>`. Only one such job can be
pending (later changes ride on it, since it reads current data), and the
worker never runs two jobs with the same coalesce key at once (advisory lock
per key in the claim transaction plus a fresh re-check; see architecture.md).
So version 2 waits for version 1 and then sends the latest state. If version
1 fails with a retryable error while version 2 is pending, version 1 is
marked superseded instead of being retried.

### Results

| Situation | Outcome | Code |
|---|---|---|
| read or update: 5xx, 408, timeout, dropped connection | retryable (repeatable; the row is located again) | `SHEETS_HTTP_<n>`, `SHEETS_TRANSPORT_ERROR` |
| **append**: 5xx, 408, read/write timeout, dropped connection, success without `updates.updatedRows = 1` for one A:M row | **unknown** | `SHEETS_APPEND_OUTCOME_UNKNOWN` |
| connect error / timeout, pool timeout | retryable | `SHEETS_CONNECT_FAILED` |
| token endpoint unreachable | retryable | `SHEETS_TOKEN_REFRESH_FAILED` |
| 429, or 403 with reason `rateLimitExceeded` / `userRateLimitExceeded` / `RATE_LIMIT_EXCEEDED` / `RESOURCE_EXHAUSTED` | retryable; `Retry-After` honoured, capped at `WORKER_MAX_RETRY_DELAY_SECONDS` | `SHEETS_RATE_LIMITED` |
| other 403 | permanent (share the sheet with the service account) | `SHEETS_PERMISSION_DENIED` |
| 400 "Unable to parse range" / other 400 / 401 / 404 | permanent | `SHEETS_WORKSHEET_NOT_FOUND` / `SHEETS_BAD_REQUEST` / `SHEETS_INVALID_CREDENTIALS` / `SHEETS_NOT_FOUND` |

Google error bodies are read only to pick the code; they are never stored.

### Unknown appends and reconciliation

An append whose result is lost may have added the row. After any unresolved
`unknown` attempt in **any** Sheets job of the project (resolved = a later
successful sync), the next sync still searches first:

* the row is found → it is updated ("adopted"); no append;
* several rows → `SHEETS_DUPLICATE_PROJECT_ROWS`;
* none → **no automatic append**: unknown `SHEETS_RECONCILIATION_REQUIRED`.

Then, in the admin console (Integrations → job), check column A of the sheet
for the project ID, remove duplicates, tick "I checked the sheet…" and
**Retry job**. The confirmation is stored durably on the job
(`payload.reconciliation.confirmed_at`, microsecond precision) and must be
later than the unknown attempt. The retry searches again and appends only if
the row is still absent and the search was complete. A job whose newer
version is already queued is not requeued (409 `job_superseded`): the newer
job sends the latest data.

### Live test

```bash
cd backend
GOOGLE_SHEETS_LIVE_TEST=1 .venv/bin/python -m app.integrations.sheets_live_check
```

Needs `GOOGLE_SERVICE_ACCOUNT_FILE` and `GOOGLE_SHEETS_TEST_SPREADSHEET_ID` (a
separate spreadsheet with the same header; refused if equal to the reporting
one), runs on `ltd_test`, refuses while other Sheets jobs are pending, then:
append → find once → re-sync updates the same row → change the project →
row reflects it → exactly one row → delete the synthetic row after
re-locating it → confirm it is gone. If cleanup cannot be verified it prints
the project ID and worksheet for manual removal. **Status (2026-11-10):
not run.** A service-account key is configured, but the test spreadsheet ID
equals the reporting spreadsheet ID (the check correctly refuses), and a
read-only access check returned `SHEETS_NOT_FOUND` (the spreadsheet is not
shared with the service account, or the ID is wrong). Its steps were
exercised against the fake API only. See
[integration-verification.md](integration-verification.md).

### Adapter contract (for future adapters)

An adapter is a `Handler(job_type, execute, on_success=None,
payload_model=None)`. `execute(ctx)` runs without a database connection and
returns an `IntegrationResult`:

| Situation | Result |
|---|---|
| Done (2xx) | `success(external_id=…)` |
| Not done, may work later: connect error, timeout before sending, 408, 429, 5xx | `retryable(code, msg, http_status=…, retry_after_seconds=…)` |
| Will not work without a change: 401/403/404/422, missing configuration | `permanent(code, msg)` |
| May have been done: sent, then timed out or connection dropped | `unknown(code, msg)` |

`app.jobs.retry.classify_http_status` maps statuses. `on_success(conn, ctx,
result)` stores external ids (e.g. `tasks.clickup_task_id`) in the same
transaction that marks the job succeeded. When `ctx.previous_outcome_unknown`
is true the adapter must look the resource up before creating it (ClickUp:
search the list for the task's marker; Sheets: upsert by project id; Slack
webhooks cannot be checked, so a duplicate message is the accepted risk).
Delivery is at-least-once; exactly-once is never claimed.

The n8n demonstration workflow (a simulated CRM calling the webhook) is
described in [n8n-demo.md](n8n-demo.md). It calls the webhook through a local
relay (`backend/app/demo_relay.py`) that forwards the signed bytes unchanged
and returns only status and IDs, because the `201` response carries the
client's invitation link. The relay is a local demo tool, not part of the
deployed API.

## Planned (not implemented)

ClickUp OAuth, assignee mapping, two-way sync, native CRM integrations, and
live verification of the ClickUp, Slack and Google Sheets adapters
(all three await credentials; none has called a real API yet).
