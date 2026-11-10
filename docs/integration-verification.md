# Integration verification

Status of each external integration against its **real** API, as of
2026-11-10. A mocked test is never reported as real verification.

| Integration | Result | Evidence | What is needed |
|---|---|---|---|
| ClickUp (task creation) | **BLOCKED: CREDENTIAL MISSING** | `CLICKUP_API_TOKEN` and `CLICKUP_LIST_ID` are empty. Mock-tested only (fake ClickUp API in the suite). | A personal token for a dedicated test List, then `CLICKUP_LIVE_TEST=1 python -m app.integrations.clickup_live_check` |
| Slack (incoming webhook) | **BLOCKED: CREDENTIAL MISSING** | `SLACK_WEBHOOK_URL` is empty. Mock-tested only. | A webhook for a private sandbox channel, then `SLACK_LIVE_TEST=1 python -m app.integrations.slack_live_check` (sends one labelled synthetic message, which cannot be retracted) |
| Google Sheets (project rows) | **FAILED: REAL API CHECK FAILED** | A service-account key is configured (outside the repo, mode 600) and loads. The live check refused to run because `GOOGLE_SHEETS_TEST_SPREADSHEET_ID` equals the reporting `GOOGLE_SHEETS_SPREADSHEET_ID` (by design). A read-only access check of the configured spreadsheet returned `SHEETS_NOT_FOUND`: it is not shared with the service account, or the ID is wrong. No row was written. | Create a separate test spreadsheet with the header from the README, share both spreadsheets with the service account's email as Editor, set `GOOGLE_SHEETS_TEST_SPREADSHEET_ID`, then `GOOGLE_SHEETS_LIVE_TEST=1 python -m app.integrations.sheets_live_check` |
| Generic CRM webhook | **VERIFIED (local staging)** | Signed `deal.won` → 201 with client, invitation and link; identical replay → 200 duplicate (no second client or invitation); wrong secret → 401; same event ID with a different payload → 409 `event_id_reused`; archived client → 409 `client_archived`; restored → 201. | — |
| n8n demo (simulated CRM) | **LOCAL DEMO ONLY** | Verified in Task 9.1 against an isolated n8n; relay endpoint fixed and checked before any request. Not a GoHighLevel/HubSpot integration. | — |

Each adapter is still recorded as "mock tested only" for its behaviour
(retries, unknown outcomes, reconciliation, rate limits): those paths have
not been exercised against the real services.

## Effect on deployment

Unconfigured integrations do not block the core workflow: their jobs fail
permanently with `*_NOT_CONFIGURED`, stay visible in the admin console and
can be retried after configuration. Before enabling an integration in
production, run its live check against a dedicated test target and record the
result here.

## Safety rules for live checks

- Only dedicated test targets (test List, sandbox channel, test spreadsheet);
  never a customer channel or the reporting spreadsheet.
- Run only against `ltd_test`, with no other test run in progress.
- Synthetic, clearly labelled records only; clean up only the exact record
  created.
