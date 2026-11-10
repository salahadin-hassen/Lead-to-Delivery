# n8n CRM deal-won demo

`n8n/crm-deal-won-demo.json` is an n8n workflow that acts as a **simulated CRM**.
It takes a synthetic "deal won" example, turns it into the backend's webhook
contract, signs it, sends it to the local API **through a loopback relay**, then
sends the identical event a second time to show that the backend recognises the
replay and creates nothing new.

```
n8n ──raw body + X-LTD-Timestamp/X-LTD-Signature──► relay :8011 ──same bytes/headers──► API :8010
n8n ◄──── status and IDs only (no invitation link) ── relay ◄──── full response ──────── API
```

The relay exists because the API's `201` response contains the client's
invitation link with its bearer token, and n8n shows every node's output in the
editor. See section 7.

**What this is not:**

- It is **not** a GoHighLevel or HubSpot integration. The `gohighlevel` and
  `hubspot` modes use synthetic payloads written in the *style* of those CRMs
  (`docs/examples/crm/`). No CRM account, OAuth app or CRM API is involved, and
  nothing here has been tested against a real CRM.
- It does not replace the backend's own validation. The backend still checks
  the signature, timestamp and every field.
- It does not exercise ClickUp, Slack or Google Sheets. Those adapters are
  still tested only against fakes; live verification against their real APIs is
  still outstanding.

Tested with n8n **2.41.7** (Node.js 24) on Linux, without Docker.

---

## 1. What the workflow does

```
Run Demo ─► Select CRM Example ─► Check Relay Endpoint ─► Generate Demo ID ─► Normalize CRM Payload ─► Build Exact Raw Body
   ─► Create Webhook Timestamp ─► Sign Webhook ─► Send Deal-Won Event ─► Validate First Response
   ─► Create Replay Timestamp ─► Sign Replay ─► Replay Deal-Won Event ─► Validate Replay
   ─► Present Safe Demo Result
```

| Node | Purpose |
|---|---|
| Run Demo | Manual trigger. The workflow is never activated. |
| Select CRM Example | The only node you edit: `crm_format`. |
| Check Relay Endpoint | Before anything is sent, checks that both HTTP nodes call exactly `http://127.0.0.1:8011/api/webhooks/crm/deal-won`; otherwise stops the run. |
| Generate Demo ID | A random UUID for this run (Crypto node). |
| Normalize CRM Payload | Maps the chosen example field by field onto the contract, with explicit validation. |
| Build Exact Raw Body | Serializes the payload to a JSON string **once**. |
| Create Webhook Timestamp | Current Unix time in seconds. |
| Sign Webhook / Sign Replay | HMAC-SHA256 with the secret from a Crypto credential. |
| Send Deal-Won Event / Replay Deal-Won Event | POST the raw string with the two signature headers to the relay (fixed URL). |
| Validate First Response | Stops if the response has any field the relay never returns; requires `201 created`; otherwise stops with an explanation and skips the replay. |
| Create Replay Timestamp | A new timestamp, strictly later than the first. Uses the same body string. |
| Validate Replay | Same field check; requires `200 duplicate` for the same event ID. |
| Present Safe Demo Result | The only output: IDs and status codes. |
| Relay Unreachable / Replay Relay Unreachable | Error branches of the two HTTP nodes: "relay not reachable". |

## 2. The three demo modes

Set `crm_format` in **Select CRM Example**:

| `crm_format` | Example used | `source` sent | Service |
|---|---|---|---|
| `generic` (default) | `docs/examples/crm/generic.json` | `generic` | `website_build` |
| `gohighlevel` | `docs/examples/crm/gohighlevel-style.json` | `gohighlevel` | `seo_retainer` |
| `hubspot` | `docs/examples/crm/hubspot-style.json` | `hubspot` | `marketing_automation_setup` |

The examples are embedded in the Normalize node as exact copies of those files
(a test checks they match). All names, emails, phone numbers and companies are
fictitious.

## 3. Normalization

Each run builds a new payload field by field. Nothing from the example is
passed through unchecked:

- `event_id` = `n8n-demo-<gen|ghl|hs>-<demo UUID>` and `deal.external_id` =
  `<example deal id>-<demo UUID>`. They are new on every run, so a second run is
  a new deal, not a replay of an old one.
- `occurred_at` is the current time.
- Contact `external_id`, name and email come from the example unchanged, so
  repeated runs of the same mode are recognised as the **same client**.
- Checks (the workflow stops with `Normalization failed: …` if one fails):
  `crm_format` must be one of the three, the example's `source` must match the
  mode, `event_type` must be `deal.won`, `value` must be a non-negative number
  with at most 2 decimals, `currency` must be 3 letters, `service_type` must be
  one of the three active templates, `contact.name` is required, contact needs
  an `external_id` or an email, and the email and phone must look valid.
  (The relay endpoint is not a setting; see section 7.)

## 4. Raw body and signature

The backend signs `timestamp + "." + raw body bytes`, so n8n must send exactly
the bytes it signed:

1. **Build Exact Raw Body** calls `JSON.stringify` once and stores the result
   as the string `body`.
2. **Sign Webhook** (Crypto node, action *Hmac*, type *SHA256*, encoding
   *hex*) signs `{{ $json.timestamp + '.' + $json.body }}`.
3. **Send Deal-Won Event** sends `{{ $json.body }}` as a **raw** body with
   content type `application/json`, and the headers
   `X-LTD-Timestamp: <timestamp>` and `X-LTD-Signature: sha256=<hex>`.

Nothing parses or re-serializes the body between signing and sending, and the
relay forwards the bytes unchanged (section 7). The replay reuses the same `body` string from **Build Exact Raw Body**, with a new
timestamp and a new signature.

Two n8n details matter here (verified in 2.41.7):

- The HTTP node's raw body option sends the string as is. A JSON body option
  would re-serialize an object, so it is not used.
- The response format is left on **autodetect**. With a raw request body, an
  explicit "JSON" response format leaves the response as an unparsed stream,
  and the validators would see no status. The live test caught this.

## 5. Secret handling

The secret is stored **only** in an n8n **Crypto** credential named
`LTD CRM webhook HMAC (demo)`. Its *Hmac Secret* field must equal the backend's
`CRM_WEBHOOK_SECRET`. n8n encrypts credentials with the instance's own key.

The secret is **not** in the workflow JSON, any Code node, any expression, a
Set node, static data or this document, and the workflow never uses `$env`.
The instance also sets `N8N_BLOCK_ENV_ACCESS_IN_NODE=true`. No unrestricted Code
node modules are needed, because signing is done by the Crypto node.

Use a **dedicated development secret** (at least 32 characters) for this demo,
never a production one. To create one without it landing in shell history,
write it straight to a private file and paste it from there:

```bash
umask 077; python3 -c "import secrets;print(secrets.token_urlsafe(36))" > ~/ltd-dev-webhook-secret
```

## 6. Idempotency demonstration

| Request | Expected | Meaning |
|---|---|---|
| First | `201` `{"status":"created", …}` | Client, onboarding invitation and access token created. |
| Replay | `200` `{"status":"duplicate","event_id":…}` | Same `(source, event_id)` already processed: nothing created, `duplicate_count` +1. |

If the first response is not `201 created`, the replay is not sent. A `201` on
the replay is reported as an idempotency failure.

## 7. Output safety and the relay

### Why a relay

The API's `201` response contains `onboarding_url`, a working invitation link
that carries the client's bearer token (`…#t=<token>`). n8n shows **every node's
output** in the editor while a workflow runs, and `n8n execute` prints every
node's data. Removing the field in a later node does not help: the HTTP node
has already received it, and the editor has already shown it. So the HTTP nodes
must never receive that response at all.

### What the relay does

`backend/app/demo_relay.py` (Python standard library only) sits between n8n
and the API:

- **Request, unchanged.** It reads the body as bytes and forwards exactly those
  bytes, plus `Content-Type`, `X-LTD-Timestamp` and `X-LTD-Signature` copied
  verbatim. It never parses or re-serializes the body, so the signature n8n
  computed is the one the API checks. Every other header (cookies,
  `Authorization`, …) is dropped. It holds no secret and checks nothing itself:
  the API still verifies the HMAC, the timestamp window, every field and
  idempotency.
- **Response, reduced.** It reads the API response into memory, keeps only
  these fields and discards the rest:

  | API response | What n8n receives |
  |---|---|
  | `201 created` | `{"status":"created","client_id":…,"invitation_id":…}` |
  | `200 duplicate` / `duplicate_deal` | `{"status":"duplicate","event_id":…}` |
  | any error | `{"status":"error","code":…}`, plus `"field"` (e.g. `body.deal.currency`) for a `422` |
  | API down, redirect, non-JSON | `502` `{"status":"error","code":"upstream_unreachable" \| "unexpected_response","relay":true}` |

  IDs, codes and field paths are checked against strict patterns. Upstream
  text is never copied. A last check refuses any response containing a field
  outside that list, `#t=`, `onboarding`, `://` or `token`, and returns
  `unsafe_response` instead. Response headers are only `Content-Type`,
  `Content-Length`, `Cache-Control: no-store`, `Date` and `Server`.
- **Never logs bodies.** One line per request: method, path, status, upstream
  status and duration. The API response is never written anywhere.
- **Local only.** It binds only to a loopback address, forwards only to a
  loopback `http://host:port` upstream, accepts only
  `POST /api/webhooks/crm/deal-won` (anything else gets `404`), limits bodies to
  64 KiB like the API, and never follows redirects. It refuses to start with a
  non-loopback `--host` or `--upstream`. It is a demo tool: it never talks to
  production and is not part of the deployed API.

### The endpoint is fixed and checked before anything is sent

The relay endpoint is **not configurable**. Both HTTP nodes have the literal
URL `http://127.0.0.1:8011/api/webhooks/crm/deal-won` (a plain value, not an
expression), and there is no `relay_url` setting.

Because anyone can still edit an HTTP node in the editor, **Check Relay
Endpoint** runs right after **Select CRM Example**, before any HTTP request.
It reads both HTTP nodes' own settings (`$('…').params`) and stops the run with
`Relay endpoint check failed: … No request was sent.` unless each one has:

- the URL **exactly** `http://127.0.0.1:8011/api/webhooks/crm/deal-won` (so
  the API on 8010, `localhost`, `https`, other hosts, ports or paths, query
  strings, credentials and expressions are all rejected);
- method `POST`, no query parameters, no proxy, redirects not followed.

Both nodes are checked up front, so a wrong replay URL also stops the run
before the first request. The error never repeats the rejected URL.

**Validate First Response** and **Validate Replay** keep a further check: they
stop with `Unsafe response: N field(s) outside the relay allowlist` if a
response has any field the relay never returns.

### Final output

**Present Safe Demo Result** shows:

```json
{
  "result": "success",
  "crm_format": "generic",
  "event_id": "n8n-demo-gen-…",
  "deal_id": "deal_123-…",
  "first_request": { "status": 201, "result": "created", "client_id": "…", "invitation_id": "…" },
  "replay": { "status": 200, "result": "duplicate" },
  "message": "The deal-won event was accepted once and the identical replay was recognised as a duplicate. The client invitation link was not exposed."
}
```

Error messages contain only the HTTP status, the error code and the first
failing field.

### How to check it yourself

After a manual run, open **Send Deal-Won Event** and **Replay Deal-Won Event**
in the editor. Their output `body` must contain only `status`, `client_id`,
`invitation_id` (first) and `status`, `event_id` (replay). No node may show a
URL, `#t=` or a token.

What was verified on n8n 2.41.7 (all three modes, manual and command-line runs):
every push message the editor received (47 per manual run, including each
node's output) and the full `n8n execute` output contained no `#t=`, no
`onboarding`, no webhook secret and no string whose SHA-256 matches a stored
access-token hash. A copy of the workflow with both HTTP nodes pointed at the
API stopped at **Check Relay Endpoint**: the editor received output only for
**Run Demo** and **Select CRM Example**, and neither the relay nor the API
received a request (relay log and `webhook_events` unchanged). The same held
for the API on the replay node only, a different port, `localhost` and an
expression URL, run from the command line.

## 8. Execution retention (dedicated instance)

The instance that runs this demo keeps **no execution data**. With the relay, the
HTTP nodes no longer receive the invitation link, but nothing from the demo
needs to be kept, so this stays as a second layer.

`scripts/start-n8n.sh` sets, for this instance only:

| Setting | Value |
|---|---|
| `EXECUTIONS_DATA_SAVE_ON_SUCCESS` | `none` |
| `EXECUTIONS_DATA_SAVE_ON_ERROR` | `none` |
| `EXECUTIONS_DATA_SAVE_ON_PROGRESS` | `false` |
| `EXECUTIONS_DATA_SAVE_MANUAL_EXECUTIONS` | `false` |

The workflow repeats them in its own settings (`saveDataSuccessExecution: none`,
`saveDataErrorExecution: none`, `saveManualExecutions: false`,
`saveExecutionProgress: false`), so it also keeps nothing if imported elsewhere.

What n8n 2.41.7 actually does (observed): every execution first gets a
placeholder row. At the end n8n soft-deletes the row and leaves its data empty
(`[]`). Soft-deleted rows are invisible in the UI and API, and n8n's pruning
removes them later. The event log (`n8nEventLog.log`) records node names and
timings, not data. After the live runs, a byte-level search of every file in
`var/n8n` (database, event logs, config) found no secret, no `onboarding_url`
and no live token. (A static editor JavaScript file in `var/n8n/.cache` contains
the characters `#t=`; it is n8n's own code, not a token.)

Check the running instance yourself: `scripts/start-n8n.sh config` shows the
environment it starts with, and, while signed in to the editor,
`http://127.0.0.1:5690/rest/settings` shows what the server uses. It should
report
`saveDataSuccessExecution: none`, `saveDataErrorExecution: none`,
`saveManualExecutions: false`, `saveExecutionProgress: false`.

## 9. Setup

Requirements: n8n 2.x on `PATH` (`npm install -g n8n`), the backend set up as in
the README, PostgreSQL `ltd_test`.

**Use a dedicated n8n instance.** `scripts/start-n8n.sh` gives it its own data
folder `var/n8n/` (gitignored, mode 700) and its own encryption key (generated
by n8n on first use in `var/n8n/.n8n/config`, mode 600, kept between runs).
It listens on `127.0.0.1:5690` with its task-runner broker on `5691`, away from
n8n's defaults (5678/5679) and other local instances. Override the ports with
`LTD_N8N_PORT` / `LTD_N8N_BROKER_PORT`. Never point it at another instance's
folder.

```bash
scripts/start-n8n.sh config   # shows folder, ports and retention settings
scripts/start-n8n.sh import   # imports n8n/crm-deal-won-demo.json (instance must be stopped)
scripts/start-n8n.sh start    # http://127.0.0.1:5690
```

Then, in the editor:

1. On first open, create the local owner account (stays in `var/n8n`).
2. Open the workflow **LTD - CRM deal-won demo (HMAC + idempotent replay)**.
3. Open **Sign Webhook**, choose or create the credential
   **LTD CRM webhook HMAC (demo)** (type *Crypto*) and paste your development
   secret into *Hmac Secret*. Select the same credential in **Sign Replay**.
4. Do not change the URL of the two HTTP nodes. The relay must run on
   `127.0.0.1:8011`; any other URL makes **Check Relay Endpoint** stop the run.

## 10. Running the demo

Start the API against **`ltd_test`** with the same secret. Read the secret from
the file, so it never appears in a command line:

```bash
cd backend
LTD_DISABLE_DOTENV=1 DATABASE_URL=postgresql:///ltd_test \
CRM_WEBHOOK_SECRET="$(cat ~/ltd-dev-webhook-secret)" \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8010
```

(`LTD_DISABLE_DOTENV=1` makes the API ignore `.env`, so it cannot pick up the
`ltd` database by mistake.)

In a second terminal, start the relay (no secret needed):

```bash
cd backend
.venv/bin/python -m app.demo_relay --port 8011 --upstream http://127.0.0.1:8010
```

Stop either with Ctrl+C. The relay prints one line per request, never a body.

In n8n: set `crm_format`, click **Execute workflow**, then open
**Present Safe Demo Result**. Repeat with each mode. Running a mode again
creates a new deal for the same client.

From the command line (instance stopped). `n8n execute` prints every node's
data; with the relay that contains no invitation link, but the filter below
keeps the output short:

```bash
scripts/start-n8n.sh n8n execute --id=ltdCrmDealWonDemo --rawOutput 2>&1 | python3 -c '
import json,re,sys
raw=sys.stdin.read(); m=re.search(r"^\{", raw, re.M)
d=json.JSONDecoder().raw_decode(raw[m.start():])[0]["data"]["resultData"] if m else None
print(json.dumps(d["runData"]["Present Safe Demo Result"][0]["data"]["main"][0][0]["json"] if d and not d.get("error")
      else {"error": (d or {}).get("error", {}).get("message", "see n8n output")}, indent=1))'
```

## 11. Checking the database

Read-only, on `ltd_test`:

```sql
SELECT source, external_event_id, status, duplicate_count
FROM webhook_events WHERE external_event_id LIKE 'n8n-demo-%' ORDER BY created_at;
-- each successful run (editor or command line): status processed, duplicate_count 1

SELECT c.name, count(i.id) AS invitations
FROM clients c JOIN onboarding_invitations i ON i.client_id = c.id GROUP BY c.name;
-- one client per example identity, one invitation per run
```

Never run the demo against `ltd`. To clear demo data, run the backend test
suite (it truncates `ltd_test` only).

## 12. Troubleshooting

| Message | Cause | Fix |
|---|---|---|
| `Relay not reachable at …: ECONNREFUSED` | Relay not running | Start the relay on port 8011 |
| `First request failed: HTTP 502 (upstream_unreachable)` | Relay running, API not | Start uvicorn on port 8010 |
| `Relay endpoint check failed: … No request was sent.` | An HTTP node's URL, method, query, proxy or redirect setting was changed | Re-import `n8n/crm-deal-won-demo.json` (`scripts/start-n8n.sh import`) |
| `Unsafe response: N field(s) outside the relay allowlist` | Something other than the relay answered on port 8011 (for example the API started on 8011) | Run the API on 8010 and the relay on 8011; treat any link shown as exposed (run the test suite to clear `ltd_test`) |
| `HTTP 401 (invalid_signature): signature rejected` | Credential secret ≠ `CRM_WEBHOOK_SECRET` | Paste the same secret into the credential |
| `HTTP 401 (timestamp_out_of_window)` | Clock off by more than 5 minutes | Fix the system clock |
| `HTTP 503 (webhook_not_configured)` | API has no secret, or it is shorter than 32 characters | Set `CRM_WEBHOOK_SECRET` |
| `HTTP 422 at body.…` | Payload rejected by validation | Check the example; the field is named |
| `HTTP 409 (…)` | Event ID reused with different data, or a client identity conflict | Run again (new IDs), check the example |
| `HTTP 200: … already processed earlier` | First request was a duplicate | Should not happen; IDs are new each run |
| `Replay failed: … NEW event` | Backend idempotency broken | Investigate the backend |
| `Normalization failed: …` | Invalid mode or example | Read the field named in the message |
| n8n: `Task Broker's port … is already in use` | Another n8n is using the broker port | Set `LTD_N8N_BROKER_PORT` |
| Credential missing in Sign nodes | Imported workflow, no credential yet | Section 9, step 3 |

## 13. Tests

`backend/tests/test_n8n_workflow.py` (needs Node.js; tests skip without it):

- the export: node chain, node versions, error branches, retention settings,
  raw body and headers, no secrets/`$env`/tokens/literal signatures, and the
  embedded examples match `docs/examples/crm`;
- the workflow's own Code-node JavaScript, run with Node.js through
  `tests/n8n_code_harness.mjs`: normalization of all three modes (validated with
  the backend's `DealWonEvent` schema), unique IDs, every validation error,
  body serialized once and reused for the replay, response classification;
- the Crypto node's HMAC (Node `createHmac`) equals the backend's
  `webhook_signature`, including non-ASCII bodies;
- the full chain through the **real relay** to the real API over HTTP on
  `ltd_test`: all three modes `201 → 200 duplicate`, a second run creates a new
  event for the same client, a wrong secret gives `401` and writes nothing, and
  **every HTTP node output** (not just the final one) contains only relay fields
  and no URL or token, while the API's own response did contain it;
- the validators stop on a response that bypassed the relay;
- **Check Relay Endpoint**, run on the exported workflow: every path from the
  trigger to an HTTP node goes through it; it passes the export unchanged; and
  it rejects 27 changed configurations (API URL on either or both nodes,
  `localhost`, IPv6 loopback, `https`, other ports, hosts and paths, trailing
  slash, query, fragment, credentials, expressions, `GET`, query parameters,
  proxy, redirects, a renamed node);
- with the real relay and API running and counting requests, a changed URL
  (the API, another relay path, a stand-in server) or a changed replay node
  alone stops the run with **no request** to the relay, the API or the target
  and no rows written; a negative control skips the check and shows the same
  change would deliver the invitation link to the HTTP node.

`backend/tests/test_demo_relay.py` tests the relay itself: byte-exact
forwarding (non-ASCII, escapes, odd spacing) with only the three headers,
reduction of every response type, leaky error bodies and headers, redirects,
API down, wrong path or method, oversized bodies, loopback-only binding and
upstream, the CLI, its log lines, and the full path to the real API (valid
signature `201`, replay `200`, wrong secret, changed bytes, stale timestamp,
missing signature and a re-serialized body all rejected with no rows written).
A negative control swaps in a relay that leaks the URL and checks that the
safety assertions fail.

These tests do not run n8n itself; the live run (section 10) does.

## 14. Limitations

- Simulated CRM only. No native GoHighLevel/HubSpot integration and no real
  CRM account, OAuth or API.
- The endpoint check verifies the HTTP nodes' settings, not who is listening on
  port 8011. If the API itself were started on 8011, the HTTP node would
  receive its full response; the validators would then stop the run. Run the API
  on 8010 as documented.
- Someone who edits the workflow can also remove the check itself. Re-import the
  file to restore it.
- The relay is a local demo tool, not a production component.
- The demo creates real records in the database it points at. Use `ltd_test`.
- Workflow-level retention settings are a second layer. They do not replace a
  dedicated instance configured to keep nothing.
- ClickUp, Slack and Google Sheets are not involved and remain unverified
  against their real APIs.
