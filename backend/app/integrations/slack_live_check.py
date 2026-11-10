"""Controlled live check against a DEDICATED Slack sandbox channel.

    cd backend
    SLACK_LIVE_TEST=1 .venv/bin/python -m app.integrations.slack_live_check

Needs SLACK_WEBHOOK_URL (environment or .env) for an incoming webhook that
posts to a sandbox channel, and runs on TEST_DATABASE_URL, which must be
``ltd_test``. Refuses while any other slack.notify job is pending or running
there, so it can never post notifications for leftover test data.

Posts ONE synthetic "New project ready" message, then verifies:
1. HTTP 200 with body exactly "ok" (recorded on the attempt);
2. the worker marked the job succeeded;
3. running the worker again sends nothing.
HTTP 200 proves Slack accepted the message, not that it is visible: confirm
in the channel yourself. The webhook URL is never printed.
"""

import os
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import httpx

from app import db
from app.config import Settings
from app.integrations.slack import SlackAdapter
from app.jobs import runner
from app.jobs.registry import HandlerRegistry

REQUIRED_DB = "ltd_test"


class LiveCheckError(RuntimeError):
    pass


def live_check(settings: Settings, *, transport: httpx.BaseTransport | None = None,
               out: Callable[[str], None] = print) -> dict:
    adapter = SlackAdapter(settings, transport=transport)
    problem = adapter.configuration_error()
    if problem is not None:
        raise LiveCheckError(f"configuration: {problem}")
    db.configure_pool(settings.database_url.get_secret_value())
    try:
        with db.transaction() as conn:
            name = conn.execute("SELECT current_database() AS d").fetchone()["d"]
            if name != REQUIRED_DB:
                raise LiveCheckError(f"refusing to run against database {name!r}; use {REQUIRED_DB}")
            others = conn.execute("SELECT count(*) AS n FROM integration_jobs WHERE job_type = 'slack.notify' "
                                  "AND status IN ('pending', 'running')").fetchone()["n"]
            if others:
                raise LiveCheckError(f"{others} other Slack job(s) pending in {REQUIRED_DB}; clear them first")
            stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
            client = conn.execute("INSERT INTO clients (name, company) VALUES ('LTD Live Test', 'LTD Sandbox Co') "
                                  "RETURNING id").fetchone()["id"]
            project = conn.execute(
                "INSERT INTO projects (client_id, name, start_date, due_date) VALUES (%s, %s, %s, %s) RETURNING id",
                (client, f"[LTD live test] {stamp}", date.today(), date.today() + timedelta(days=30))).fetchone()["id"]
            conn.execute("INSERT INTO tasks (project_id, title) VALUES (%s, 'Synthetic task')", (project,))
            job = conn.execute(
                "INSERT INTO integration_jobs (job_type, idempotency_key, subject_type, subject_id, payload, max_attempts) "
                "VALUES ('slack.notify', %s, 'project', %s, '{\"event\": \"project_created\"}'::jsonb, 1) RETURNING id",
                (f"slack.notify:project_created:{project}", project)).fetchone()["id"]
        out(f"synthetic project {project}, job {job}")

        registry = HandlerRegistry()
        registry.register(adapter.handler())
        report = runner.run_one(registry, worker_id="live-check", lease_seconds=settings.worker_lease_seconds)
        with db.connection() as conn:
            row = conn.execute("SELECT status, last_error_code FROM integration_jobs WHERE id = %s", (job,)).fetchone()
            attempt = conn.execute("SELECT outcome, http_status, error_code FROM integration_attempts "
                                   "WHERE job_id = %s ORDER BY attempt_number DESC LIMIT 1", (job,)).fetchone()
        if report is None or attempt is None or attempt["http_status"] != 200 or attempt["outcome"] != "success":
            raise LiveCheckError(f"1. delivery failed: {attempt['outcome'] if attempt else None} "
                                 f"{attempt['error_code'] if attempt else None} (HTTP {attempt['http_status'] if attempt else None})")
        out("1. Slack answered HTTP 200 with body 'ok'")
        if row["status"] != "succeeded":
            raise LiveCheckError(f"2. job is {row['status']} {row['last_error_code']}")
        out("2. job marked succeeded")
        if runner.run_one(registry, worker_id="live-check", lease_seconds=settings.worker_lease_seconds) is not None:
            raise LiveCheckError("3. job was executed again")
        out("3. re-running the worker sent nothing")
        out("Now confirm the message is visible in the sandbox channel (HTTP 200 does not prove that).")
        return {"project_id": str(project), "job_id": str(job)}
    finally:
        adapter.close()
        db.close_pool()


def main() -> int:
    if os.environ.get("SLACK_LIVE_TEST") != "1":
        print("Refusing: set SLACK_LIVE_TEST=1 to post a real message to the Slack sandbox channel.", file=sys.stderr)
        return 2
    from dotenv import dotenv_values

    from app.config import REPO_ROOT
    test_url = os.environ.get("TEST_DATABASE_URL") or dotenv_values(REPO_ROOT / ".env").get("TEST_DATABASE_URL")
    if not test_url:
        print("Refusing: TEST_DATABASE_URL is not set.", file=sys.stderr)
        return 2
    try:
        live_check(Settings(database_url=test_url))
    except LiveCheckError as exc:
        print(f"LIVE CHECK FAILED: {exc}", file=sys.stderr)
        return 1
    print("LIVE CHECK PASSED (HTTP delivery); channel visibility needs manual confirmation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
