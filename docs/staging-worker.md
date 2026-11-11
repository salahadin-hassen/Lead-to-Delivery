# Staging worker (manual GitHub Actions run)

The $0 staging ([free-staging-options.md](free-staging-options.md)) has no
background-worker service: Render Free offers none, and a continuously
polling worker would keep Neon Free awake and exhaust its 100 CU-hours. Jobs
are therefore processed by a **manually started, time-boxed** run of the
same worker, in GitHub Actions:
[`.github/workflows/staging-worker.yml`](../.github/workflows/staging-worker.yml).

Status (2026-11-11): the workflow exists in the repository; it has **never
run** against a database. The first real run happens after the owner
configures Neon and the GitHub environment below.

## What a run does

1. **Guard job** (no secrets, no environment): fails unless the run is on
   `refs/heads/main` and the `minutes` input is a whole number from 1 to 15
   (default 10).
2. **Worker job** (environment `staging-worker`; starts only after the
   environment's approval):
   1. checks out the commit and **builds the committed `Dockerfile`** (no
      registry, no push) — the same image the staging API runs;
   2. prepares worker-only settings: `APP_ENV=production`, `APP_BASE_URL`
      from the environment variable, and **throwaway, masked**
      `ADMIN_MFA_ENCRYPTION_KEY` / `CRM_WEBHOOK_SECRET` values (the worker
      never reads them; production start-up validation requires them; the
      API's real values are never given to the worker);
   3. **preflight**: connects with `STAGING_LTD_APP_DATABASE_URL` and refuses
      to continue unless the role is `ltd_app`, the role cannot create
      objects in `public`, the connection is TLS-encrypted,
      [`deploy/db-app-check.sql`](../deploy/db-app-check.sql) passes (the
      exact privilege policy, the same check the setup helper and CI run;
      mounted read-only from the checkout), and the schema version equals
      what this commit expects; it never prints the URL or driver connection
      messages;
   4. runs `python -m app.jobs.worker` in a container for the requested
      minutes (secrets passed to Docker **by name**, never on a command
      line); if the worker exits early, the run fails and shows the last log
      lines;
   5. `docker stop --time 120` → SIGTERM: the worker stops claiming, finishes
      the current job (outbound HTTP timeout 15 s), records a final
      heartbeat and exits; the run requires exit code 0 and the
      `worker stopped` log line;
   6. always (also on failure or cancellation): stops the container with a
      60 s grace period and removes it and the image.
3. **One run at a time** (`concurrency: staging-worker`, never cancels a
   running worker; a second request waits). Read-only token, no other
   secrets, actions pinned to commit SHAs, no deployment.

If the runner itself dies mid-job, the job's 120 s lease expires and the next
worker run records that attempt as `unknown`, which the admin console
requires to be reconciled before a retry — never a blind duplicate.

With integrations unset (as required), jobs end `dead` with
`*_NOT_CONFIGURED`; the run demonstrates claiming, attempts, retries,
finalisation and graceful stop, not ClickUp/Slack/Sheets.

## One-time GitHub setup (owner, before adding any secret)

The workflow naming an environment is **not** protection by itself; the
rules below must be configured by hand.

1. Confirm the repository is **public** (Settings → General → Danger Zone
   shows "Change visibility … public"). On GitHub Free, environment
   secrets, required reviewers and deployment-branch rules are available
   **only for public repositories**. If the repository is ever made
   private on Free, the environment protection and its secrets stop
   working; delete the environment's secrets first, or upgrade (a cost
   decision outside this plan).
2. Settings → Environments → **New environment** → `staging-worker`.
3. **Deployment branches and tags** → *Selected branches and tags* → add
   branch rule `main` only.
4. **Required reviewers** → add yourself (up to six people; one approval is
   enough). If you are the only reviewer, leave **Prevent self-review**
   **unchecked**, otherwise nobody can approve your own run.
5. Save the protection rules, **then** add, in this environment only (never
   repository-wide secrets):
   - secret `STAGING_LTD_APP_DATABASE_URL` = the **`ltd_app`** role's
     **direct** Neon URL (host without `-pooler`) ending in
     `?sslmode=require`;
   - variable (not secret) `STAGING_APP_BASE_URL` =
     `https://<service>.onrender.com`.
   Never add the Neon owner credentials, the API's MFA key or webhook
   secret, or any integration credential.

Pull requests from forks cannot read environment secrets, and
`workflow_dispatch` requires write access to the repository.

## Running it

GitHub → Actions → **Staging worker (manual)** → Run workflow → branch
`main`, minutes (1–15) → approve the pending `staging-worker` deployment.
Neon wakes for the duration of the run and suspends 5 minutes after it.

## Stopping it early

Actions → the run → **Cancel workflow**. The cleanup step stops the
container with SIGTERM (60 s grace) and removes it. A job cut off by the
cancellation is recovered through its lease on the next run.

## Verifying a run

The run log shows `preflight ok: role ltd_app, TLS, exact privilege policy, schema N`,
`worker started`, the worker's own log lines, `time limit reached: sending
SIGTERM`, and `worker stopped cleanly`. In the admin console the processed
jobs show new attempts. Secrets never appear (GitHub masks them; the
application does not log them).

## Removing it

Settings → Environments → delete `staging-worker` (removes its secrets).
Optionally disable the workflow (Actions → workflow → ⋯ → Disable workflow).
