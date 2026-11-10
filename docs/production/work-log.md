# Production-readiness sprint: work log

Internal log of the 2026-11-10 sprint. Facts observed, not plans.

## Phase 0: ownership and recovery point (2026-11-10, ~21:00 EAT)

- Repository root and Git root: `~/projects/business-automation/Lead-to-Delivery`,
  branch `main`, **no commits**. Remote `origin` =
  `github.com/salahadin-hassen/Lead-to-Delivery`; `git ls-remote` returned no
  refs (remote empty). HTTPS push has no credentials; SSH key authenticates as
  `salahadin-hassen`.
- Processes: one Antigravity hub (`agy --hub --add-dir=<this repo>`), cwd =
  this repo, only child a Figma MCP server; its conversations were last
  written 2026-11-09 22:17, so it was idle. No pytest, uvicorn, worker, relay
  or n8n processes were running.
- Databases (local PostgreSQL): `ltd`, `ltd_test` (both at schema versions
  1–4), plus `lead_automation` and `lead_automation_test` (another project;
  not touched).
- Configuration names in `.env` (values not recorded): `APP_ENV`,
  `APP_BASE_URL`, `DATABASE_URL` (→ `ltd`), `TEST_DATABASE_URL` (→
  `ltd_test`), `ADMIN_PASSWORD_HASH` (empty), `CRM_WEBHOOK_SECRET` (empty),
  `CLICKUP_API_TOKEN` / `CLICKUP_LIST_ID` (empty), `SLACK_WEBHOOK_URL`
  (empty), `GOOGLE_SERVICE_ACCOUNT_FILE` (set; file outside the repo, mode
  600), `GOOGLE_SHEETS_SPREADSHEET_ID`, `GOOGLE_SHEETS_WORKSHEET`,
  `GOOGLE_SHEETS_TEST_SPREADSHEET_ID`, `GOOGLE_SHEETS_TEST_WORKSHEET` (set),
  worker tuning values.
- Cloud tooling: no provider CLI or configuration (Render, Fly, Railway, Neon,
  Vercel, Netlify, Heroku, DigitalOcean, GCP, AWS, Azure, Supabase) on this
  machine.
- Recovery point: file inventory (146 files), SHA-256 manifest and a source
  tarball excluding `.env`, `var/`, dependencies and build output, kept
  outside the repository. The first Git commit is the durable recovery point.

### Unexplained artefacts

| Path | Finding | Decision |
|---|---|---|
| `candidate.id` | Empty file, created 2026-11-08 14:44. Not created by any recorded Claude Code or Antigravity action. | Left in place; excluded from Git (`.git/info/exclude`). |
| `scripts/start-relay.sh` | Empty file, created 2026-11-09 21:18 by the Antigravity session (its plan names this script). | Left in place; excluded from Git. |
| `backend/app/main.py` (mtime 2026-11-09 19:57) | Antigravity added the exception text to the database-error log line and reverted it 26 steps later. Content equals the original. | Kept as is. |
| `var/n8n/` | Local isolated n8n instance (database, encryption key, event logs). | Ignored by Git. |

Scratchpad demo files (an n8n owner password and the demo webhook secret)
were in a session-temporary directory that no longer exists. The demo secret
was used only with `ltd_test` and the isolated n8n instance; it must never be
used anywhere else, and the n8n credential holding it should be replaced
before the instance is used again.

## Phase 1: secrets audit and first commit

- Scanner: `detect-secrets` 1.5.0 over every file proposed for commit, plus
  pattern checks (Slack webhook URLs, ClickUp tokens, private keys, onboarding
  `#t=` links, cloud keys). All findings were synthetic values in tests
  (`T0SYNTH01`, `xoxb-123`, dummy passwords) or placeholders in
  `.env.example`.
- No non-empty `.env` value appears in any committed file.
- `.gitignore` extended with logs, SQLite/database files, dumps and test
  report folders.
- First commit `29c9e18` pushed over SSH to the empty remote (`main`).
- Baseline re-run before changes: backend 732 passed, frontend 12 passed,
  type check and build passed.

## Phases 3–8 (summary; details in commit messages)

- Task 9.1 fail-closed relay: confirmed present (fixed endpoint + endpoint
  check node, covered by the suite).
- Client portal (`66ebca5`, `f786572`), admin accounts + MFA (`12de804`),
  hardening + worker heartbeat (`4fb7cab`), audit attribution, deployment
  files and docs (final commit).
- Staging: `CREATE DATABASE` was not permitted for the local role, so QA ran
  on a private PostgreSQL 16 instance (initdb in a temporary directory, Unix
  socket only, roles `ltd_owner`/`ltd_app`). Nothing touched `ltd`.
- Google Sheets live check refused to run (test spreadsheet ID equals the
  reporting one); a read-only access check returned `SHEETS_NOT_FOUND`.
- One intermittent frontend test failure was traced to a race in the test
  (dashboard heading rendered before its request), fixed, then 15 clean runs.
