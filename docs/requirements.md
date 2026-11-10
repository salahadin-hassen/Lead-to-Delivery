# Requirements: Agency Lead-to-Delivery Automation Hub

This summarises the approved business requirements. Implementation status is
tracked in [architecture.md](architecture.md); this file states what the
product must do, not what exists today.

## Customers and proposition

- **Target customers:** small agencies and B2B service businesses (web, SEO,
  marketing automation) that win clients in a CRM and deliver work as projects.
- **Proposition:** *"I connect your CRM, project-management software and
  business tools so winning a new client automatically initiates onboarding,
  project setup, notifications and delivery tracking."*

## Core workflow

1. A deal is marked **won** in the CRM.
2. A **normalized, signed CRM webhook** delivers the deal and contact.
3. The application validates the event, **rejects duplicates**, and creates
   (or reuses) the client.
4. The client receives a **secure onboarding link**.
5. The client submits project requirements.
6. The application creates the **project, milestones and initial tasks** from
   a template.
7. Integration jobs create **ClickUp** tasks, update **Google Sheets** and send
   **Slack** notifications.
8. Project progress is tracked by the agency and visible to the client.
9. Failed integrations are **recorded, visible and safely retryable**.

## MVP requirements

### Administration (single administrator)
- Password-protected admin area (one password, server-side sessions, CSRF
  protection, login throttling).
- Dashboard of real counts: clients, pending onboarding, projects, tasks,
  integration jobs, recent activity.
- Manage clients (create, view, edit, archive, restore). Archived clients
  keep their history; new CRM deals for them are rejected until restored.
- View onboarding invitations; regenerate a client's link (revoke the old
  token, issue a new one, shown once). Submitted projects get a 180-day
  project-access link. This is the only way to recover a lost link.
- View projects; change project and task status and task assignees. Changes
  stay local (no sync back to ClickUp).
- View integration status, jobs, attempt history and failures; retry failed
  jobs, with an explicit reconciliation confirmation when the outcome of the
  last attempt is unknown.
- View CRM webhook event history.

### Client portal
- Access only through a high-entropy, expiring, revocable token link
  (`/portal#t=<token>`; the token is sent as `Authorization: Bearer`).
- Access is scoped to **one invitation**: a token never shows the client's
  other invitations or projects, and never another client's data.
- Submit onboarding information once. The form collects project name and
  description, business goals, requirements and scope, target audience,
  desired delivery date, communication preferences, notes, and a short
  service-specific section (website build, SEO retainer, marketing automation).
- Submitting creates the project, milestones and tasks from the invitation's
  template and queues the ClickUp, Sheets and Slack jobs, all in one transaction.
- See the project's progress, milestones, client-visible tasks and
  client-visible activity, and nothing internal.

### Integrations
- **ClickUp:** create one task per generated project task, without duplicates.
- **Google Sheets:** one row per project in a system-managed worksheet,
  upserted by project ID (13 fixed columns), written as RAW values, kept
  current as status and task progress change; duplicate rows and uncertain
  appends are never resolved automatically.
- **Slack:** incoming-webhook notifications for key events.
- **Generic CRM webhook:** HMAC-signed, normalized `deal.won` payload. Example
  payloads may imitate GoHighLevel or HubSpot shapes; this is **not** a native
  integration with those products.
- **n8n:** a demonstration workflow that normalizes sample CRM payloads, signs
  them and calls the webhook.

### Reliability
- PostgreSQL is the source of truth, the transactional outbox and the job queue
  (no Redis/Celery).
- Duplicate CRM events and duplicate deals never create duplicate clients,
  invitations or projects.
- External API calls never run inside a business database transaction, and
  never inside a user-facing request.
- Every attempt is recorded; failures are classified (retryable vs permanent),
  retried with backoff, and can be retried manually after the cause is fixed.
- Operation-specific idempotency keys protect external writes.

### Security
- Client-level authorization on every portal query; IDs are never authorization.
- External API credentials are server-side only and never returned or logged.
- Webhooks are authenticated (HMAC + timestamp window).
- Production refuses to start with missing security settings; no development
  fallback that disables authentication.

## Deferred (not in the MVP)

- Inbound ClickUp webhooks / two-way sync.
- Native CRM connectors (OAuth apps for GoHighLevel, HubSpot, etc.).
- Sending onboarding emails (the admin copies the link; n8n could send it later).
- File uploads during onboarding.
- Multiple admin users and roles.
- Mapping ClickUp assignees to application users.
- Template management UI/API (templates are seeded by migration).
- Adding tasks by hand, extending an expired invitation, multiple administrators.
- Hosting and deployment automation.
